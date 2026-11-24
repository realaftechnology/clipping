#!/usr/bin/env python3
"""Hosted copy of the AI editor dashboard (Railway), so Natalie and Matt can open it anywhere.

It holds no pipeline: the Mac running flywheel.py pushes a snapshot of the dashboard data and the clips waiting for
review every minute (dashboard/server.py --sync), and pulls back any Approve/Reject clicked here.

Env:
  DASHBOARD_PASSWORD  password people type to open the page
  INGEST_TOKEN        shared secret the Mac uses to push data and pull actions
  DATA_DIR            persistent folder (Railway volume), default ./data
  PORT                set by the host
Standard library only.
"""
import hashlib, hmac, json, os, re, threading, time, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get('DATA_DIR', os.path.join(HERE, 'data'))
VIDEOS = os.path.join(DATA, 'videos')
PASSWORD = os.environ.get('DASHBOARD_PASSWORD', '')
TOKEN = os.environ.get('INGEST_TOKEN', '')
COOKIE = 'aied'
LOCK = threading.Lock()
os.makedirs(VIDEOS, exist_ok=True)


def session_value():
    return hmac.new(PASSWORD.encode(), b'ai-editor-dashboard-session', hashlib.sha256).hexdigest()


def read_json(name, default):
    try:
        return json.load(open(os.path.join(DATA, name)))
    except (OSError, ValueError):
        return default


def write_json(name, obj):
    tmp = os.path.join(DATA, name + '.tmp')
    json.dump(obj, open(tmp, 'w'))
    os.replace(tmp, os.path.join(DATA, name))


LOGIN = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Editor Network</title><style>
:root{color-scheme:light;--bg:#f4f4f2;--s:#fcfcfb;--b:#e2e1dc;--t:#0b0b0b;--t2:#52514e;--a:#0b0b0b;--ai:#fcfcfb}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#111110;--s:#1a1a19;--b:#2e2e2b;--t:#f0efec;--t2:#c3c2b7;--a:#f0efec;--ai:#1a1a19}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#111110;--s:#1a1a19;--b:#2e2e2b;--t:#f0efec;--t2:#c3c2b7;--a:#f0efec;--ai:#1a1a19}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--t);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;padding:16px;box-sizing:border-box}
form{background:var(--s);border:1px solid var(--b);border-radius:12px;padding:24px;width:100%;max-width:340px}
h1{font-size:19px;margin:0 0 4px}p{color:var(--t2);margin:0 0 16px;font-size:13px}
input{width:100%;box-sizing:border-box;font:inherit;padding:10px 12px;border-radius:8px;border:1px solid var(--b);background:var(--bg);color:var(--t)}
button{margin-top:12px;width:100%;font:inherit;padding:10px;border-radius:8px;border:0;background:var(--a);color:var(--ai);cursor:pointer}
.err{color:#d03b3b;font-size:13px;margin-top:10px}</style></head><body>
<form method="post" action="/login"><h1>AI Editor Network</h1><p>Real AF short-form dashboard</p>
<input type="password" name="password" placeholder="Password" autofocus autocomplete="current-password">
<button>Open dashboard</button>__ERR__</form></body></html>'''


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype='application/json', headers=None):
        b = body if isinstance(body, bytes) else (body.encode() if isinstance(body, str) else json.dumps(body).encode())
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def signed_in(self):
        if not PASSWORD:
            return False
        for part in self.headers.get('Cookie', '').split(';'):
            k, _, v = part.strip().partition('=')
            if k == COOKIE and hmac.compare_digest(v, session_value()):
                return True
        return False

    def machine(self):
        auth = self.headers.get('Authorization', '')
        return bool(TOKEN) and hmac.compare_digest(auth, f'Bearer {TOKEN}')

    def body(self, limit=5 << 20):
        n = int(self.headers.get('Content-Length') or 0)
        if n > limit:
            raise ValueError('too large')
        return self.rfile.read(n)

    # ---------- people ----------
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == '/healthz':
            return self.send(200, {'ok': True})
        if path == '/api/actions':
            if not self.machine():
                return self.send(401, {'error': 'unauthorised'})
            return self.send(200, read_json('actions.json', []))
        if not self.signed_in():
            if path.startswith('/api/') or path.startswith('/video/'):
                return self.send(401, {'error': 'sign in'})
            return self.send(200, LOGIN.replace('__ERR__', ''), 'text/html; charset=utf-8')
        if path in ('/', '/index.html'):
            return self.send(200, open(os.path.join(HERE, 'index.html'), 'rb').read(), 'text/html; charset=utf-8')
        if path == '/api/overview':
            snap = read_json('snapshot.json', None)
            if not snap:
                return self.send(503, {'error': 'No data from the editing Mac yet'})
            pending = {a['edit_id']: a['action'] for a in read_json('actions.json', [])}
            for r in snap.get('review', []):
                r['pending'] = pending.get(r['edit_id'])
                r['has_video'] = os.path.exists(os.path.join(VIDEOS, f"{r['edit_id']}.mp4"))
            snap['received'] = read_json('meta.json', {}).get('received')
            return self.send(200, snap)
        m = re.match(r'^/video/(\d+)$', path)
        if m:
            return self.video(int(m.group(1)))
        if path == '/logout':
            return self.send(302, b'', 'text/plain', {'Location': '/', 'Set-Cookie': f'{COOKIE}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax'})
        self.send(404, {'error': 'not found'})

    def video(self, eid):
        p = os.path.join(VIDEOS, f'{eid}.mp4')
        if not os.path.exists(p):
            return self.send(404, {'error': 'no video'})
        size = os.path.getsize(p)
        start, end = 0, size - 1
        rng = re.match(r'bytes=(\d*)-(\d*)', self.headers.get('Range', ''))
        if rng:
            start = int(rng.group(1) or 0)
            end = min(int(rng.group(2)) if rng.group(2) else size - 1, size - 1)
        self.send_response(206 if rng else 200)
        self.send_header('Content-Type', 'video/mp4')
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(end - start + 1))
        if rng:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.end_headers()
        with open(p, 'rb') as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == '/login':
            form = urllib.parse.parse_qs(self.body(4096).decode(errors='replace'))
            if PASSWORD and hmac.compare_digest(form.get('password', [''])[0], PASSWORD):
                return self.send(302, b'', 'text/plain', {'Location': '/', 'Set-Cookie':
                                 f'{COOKIE}={session_value()}; Max-Age=2592000; Path=/; HttpOnly; Secure; SameSite=Lax'})
            time.sleep(1)
            return self.send(401, LOGIN.replace('__ERR__', '<div class="err">Wrong password</div>'), 'text/html; charset=utf-8')
        m = re.match(r'^/api/edit/(\d+)/(approve|reject)$', path)
        if m:
            if not self.signed_in():
                return self.send(401, {'error': 'sign in'})
            eid, action = int(m.group(1)), m.group(2)
            snap = read_json('snapshot.json', {})
            if not any(r['edit_id'] == eid for r in snap.get('review', [])):
                return self.send(409, {'error': f'edit {eid} is not waiting for review'})
            with LOCK:
                acts = [a for a in read_json('actions.json', []) if a['edit_id'] != eid]
                acts.append({'edit_id': eid, 'action': action, 'at': time.time()})
                write_json('actions.json', acts)
            return self.send(200, {'ok': True, 'edit_id': eid, 'queued': True,
                                   'status': 'approved' if action == 'approve' else 'rejected'})
        # ---------- the editing Mac ----------
        if not self.machine():
            return self.send(401, {'error': 'unauthorised'})
        if path == '/api/ingest':
            snap = json.loads(self.body())
            keep = {str(r['edit_id']) for r in snap.get('review', [])}
            for f in os.listdir(VIDEOS):  # drop videos no longer under review
                if f.split('.')[0] not in keep:
                    os.remove(os.path.join(VIDEOS, f))
            write_json('snapshot.json', snap)
            write_json('meta.json', {'received': time.time()})
            have = sorted(int(f.split('.')[0]) for f in os.listdir(VIDEOS) if f.endswith('.mp4'))
            return self.send(200, {'ok': True, 'have_videos': have})
        if path == '/api/actions/ack':
            done = set(json.loads(self.body()).get('edit_ids', []))
            with LOCK:
                write_json('actions.json', [a for a in read_json('actions.json', []) if a['edit_id'] not in done])
            return self.send(200, {'ok': True})
        self.send(404, {'error': 'not found'})

    def do_PUT(self):
        m = re.match(r'^/api/video/(\d+)$', urllib.parse.urlparse(self.path).path)
        if not m or not self.machine():
            return self.send(401 if m else 404, {'error': 'no'})
        n = int(self.headers.get('Content-Length') or 0)
        if n > 300 << 20:
            return self.send(413, {'error': 'too large'})
        tmp = os.path.join(VIDEOS, f'{m.group(1)}.part')
        with open(tmp, 'wb') as f:
            left = n
            while left > 0:
                chunk = self.rfile.read(min(1 << 20, left))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        if left:
            os.remove(tmp)
            return self.send(400, {'error': 'incomplete upload'})
        os.replace(tmp, os.path.join(VIDEOS, f'{m.group(1)}.mp4'))
        self.send(200, {'ok': True})


if __name__ == '__main__':
    if not PASSWORD or not TOKEN:
        print('warning: DASHBOARD_PASSWORD and INGEST_TOKEN must both be set; sign-in and syncing are off until they are')
    port = int(os.environ.get('PORT', 8080))
    print(f'AI editor dashboard (hosted) on :{port}, data in {DATA}')
    ThreadingHTTPServer(('0.0.0.0', port), H).serve_forever()
