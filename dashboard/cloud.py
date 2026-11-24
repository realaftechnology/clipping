#!/usr/bin/env python3
"""Hosted AI editor dashboard (Railway). Works with the editing Mac switched off.

- Analytics are its own: every 15 minutes it reads published posts, post stats and follower counts straight from
  Zernio (ZERNIO_API_KEY) into DATA_DIR/cloud.db. Post stats refresh hourly in a post's first week, then every 6 h.
- The editing pipeline (brand set settings, review queue, schedule, activity) comes from the Mac running
  `dashboard/server.py --sync-only`, pushed every minute while it is on. The last copy stays on the page when it's off.
- Approve/Reject clicked here is queued; the Mac applies it on its next sync.

Env: DASHBOARD_PASSWORD, INGEST_TOKEN, ZERNIO_API_KEY, DATA_DIR (Railway volume), TZ (e.g. America/New_York), PORT.
Standard library only. Run from the repo root: python3 dashboard/cloud.py
"""
import datetime as dt, hashlib, hmac, json, os, re, sqlite3, sys, threading, time, traceback, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
DATA = os.environ.get('DATA_DIR', os.path.join(HERE, 'data'))
VIDEOS = os.path.join(DATA, 'videos')
PASSWORD = os.environ.get('DASHBOARD_PASSWORD', '')
TOKEN = os.environ.get('INGEST_TOKEN', '')
COOKIE = 'aied'
LOCK = threading.Lock()
os.makedirs(VIDEOS, exist_ok=True)
if hasattr(time, 'tzset'):
    time.tzset()


# ---------- storage ----------
def db():
    c = sqlite3.connect(os.path.join(DATA, 'cloud.db'), timeout=30)
    c.row_factory = sqlite3.Row
    c.executescript('''
    CREATE TABLE IF NOT EXISTS posts(remote_id TEXT, platform TEXT, account TEXT, username TEXT, url TEXT,
      published_at REAL, content TEXT, PRIMARY KEY(remote_id, platform));
    CREATE TABLE IF NOT EXISTS stats(remote_id TEXT, platform TEXT, taken_at REAL, views INTEGER, likes INTEGER,
      comments INTEGER, shares INTEGER, saves INTEGER, PRIMARY KEY(remote_id, platform));
    CREATE TABLE IF NOT EXISTS followers(account TEXT, platform TEXT, username TEXT, day TEXT, followers INTEGER,
      taken_at REAL, PRIMARY KEY(account, day));
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
    ''')
    return c


def read_json(name, default):
    try:
        return json.load(open(os.path.join(DATA, name)))
    except (OSError, ValueError):
        return default


def write_json(name, obj):
    tmp = os.path.join(DATA, name + '.tmp')
    json.dump(obj, open(tmp, 'w'))
    os.replace(tmp, os.path.join(DATA, name))


def ts(iso):
    return dt.datetime.fromisoformat(iso.replace('Z', '+00:00')).timestamp() if iso else None


# ---------- analytics straight from Zernio ----------
def refresh():
    from backends import zernio as z
    c = db()
    now = time.time()
    page = 1
    while True:  # every published post, per platform
        _, j = z._call('GET', '/posts', query={'status': 'published', 'limit': 100, 'page': page})
        for p in j.get('posts', []):
            for x in p.get('platforms', []):
                if x.get('status') != 'published':
                    continue
                acc = x.get('accountId') or {}
                c.execute('INSERT OR REPLACE INTO posts VALUES(?,?,?,?,?,?,?)', (
                    p['_id'], x['platform'], acc.get('_id') if isinstance(acc, dict) else acc,
                    acc.get('username') if isinstance(acc, dict) else None, x.get('platformPostUrl'),
                    ts(x.get('publishedAt') or p.get('publishedAt')), p.get('content')))
        pg = j.get('pagination') or {}
        if page >= (pg.get('pages') or 1):
            break
        page += 1
    c.commit()
    due = {}
    for r in c.execute('SELECT p.remote_id, p.published_at, MIN(COALESCE(s.taken_at,0)) seen FROM posts p LEFT JOIN stats s '
                       'ON s.remote_id=p.remote_id AND s.platform=p.platform WHERE p.published_at>? GROUP BY p.remote_id',
                       (now - 30 * 86400,)):
        every = 3600 if now - r['published_at'] < 7 * 86400 else 6 * 3600
        if now - r['seen'] >= every:
            due[r['remote_id']] = True
    for rid in due:
        try:
            got = z.post_stats(rid)
        except Exception as ex:
            print(f'stats {rid}: {ex}', flush=True)
            continue
        for pf, m in got.items():
            c.execute('INSERT OR REPLACE INTO stats VALUES(?,?,?,?,?,?,?,?)', (rid, pf, now, m.get('views'), m.get('likes'),
                      m.get('comments'), m.get('shares'), m.get('saves')))
            if m.get('url'):
                c.execute('UPDATE posts SET url=? WHERE remote_id=? AND platform=? AND url IS NULL', (m['url'], rid, pf))
    day = dt.date.today().isoformat()
    last = c.execute("SELECT v FROM meta WHERE k='followers_at'").fetchone()
    if not last or now - float(last['v']) > 6 * 3600:
        for a in z.account_info():
            c.execute('INSERT OR REPLACE INTO followers VALUES(?,?,?,?,?,?)', (a['id'], a['platform'], a['username'], day, a['followers'], now))
        c.execute("INSERT OR REPLACE INTO meta VALUES('followers_at', ?)", (str(now),))
    c.execute("INSERT OR REPLACE INTO meta VALUES('refreshed_at', ?)", (str(now),))
    c.commit()


def refresher(every=900):
    while True:
        try:
            if os.environ.get('ZERNIO_API_KEY'):
                refresh()
        except Exception as ex:
            print(f'refresh failed: {ex}\n{traceback.format_exc()}', flush=True)
        time.sleep(every)


# ---------- the page's data: pipeline from the Mac + analytics from here ----------
def overview():
    snap = read_json('snapshot.json', None)
    if not snap:
        return None
    c = db()
    acc2set = {a['id']: b['id'] for b in snap['brand_sets'] for a in b['accounts'] if a.get('id')}
    hooks = {p.get('remote_id'): p['hook'] for p in snap.get('posts', []) if p.get('remote_id')}
    removed = {(p.get('remote_id'), x['platform']) for p in snap.get('posts', []) for x in p['platforms']
               if x.get('status') in ('removed', 'remove_by_hand')}
    rows = c.execute('SELECT p.*, s.views, s.likes, s.comments, s.shares, s.saves, s.taken_at FROM posts p LEFT JOIN stats s '
                     'ON s.remote_id=p.remote_id AND s.platform=p.platform ORDER BY p.published_at DESC').fetchall()
    week_ago = (dt.date.today() - dt.timedelta(days=7)).isoformat()
    fol = {}
    for r in c.execute('SELECT account, day, followers FROM followers ORDER BY day'):
        fol.setdefault(r['account'], []).append((r['day'], r['followers']))

    for b in snap['brand_sets']:
        tot = {'posts': set(), 'views': 0, 'likes': 0, 'comments': 0, 'shares': 0, 'saves': 0}
        for r in rows:
            if acc2set.get(r['account']) == b['id'] and (r['remote_id'], r['platform']) not in removed:
                tot['posts'].add(r['remote_id'])
                for k in ('views', 'likes', 'comments', 'shares', 'saves'):
                    tot[k] += r[k] or 0
        tot['posts'] = len(tot['posts'])
        b['totals'] = tot
        for a in b['accounts']:
            h = fol.get(a.get('id'), [])
            if h:
                then = next((f for d, f in reversed(h) if d <= week_ago), h[0][1])
                a['followers'], a['growth_7d'] = h[-1][1], (h[-1][1] or 0) - (then or 0)

    posts, daily = {}, {}
    for r in rows:
        bs = acc2set.get(r['account'], 'other')
        g = posts.setdefault(r['remote_id'], {'remote_id': r['remote_id'], 'brand_set': bs, 'posted_at': r['published_at'],
                                              'hook': hooks.get(r['remote_id']) or (r['content'] or '').split('\n')[0][:90], 'platforms': []})
        is_removed = (r['remote_id'], r['platform']) in removed
        g['platforms'].append({'platform': r['platform'], 'status': 'removed' if is_removed else 'posted', 'url': r['url'],
                               **{k: r[k] for k in ('views', 'likes', 'comments', 'shares', 'saves')}})
        if r['published_at'] and r['published_at'] > time.time() - 30 * 86400 and not is_removed:
            day = time.strftime('%Y-%m-%d', time.localtime(r['published_at']))
            d = daily.setdefault((day, bs), {'day': day, 'brand_set': bs, 'views': 0, 'posts': set()})
            d['views'] += r['views'] or 0
            d['posts'].add(r['remote_id'])
    for d in daily.values():
        d['posts'] = len(d['posts'])
    # posts that are still publishing / failed exist only in the Mac's copy
    for p in snap.get('posts', []):
        if p.get('remote_id') not in posts and any(x.get('status') in ('publishing', 'failed') for x in p['platforms']):
            posts[p.get('remote_id') or f"e{p['edit_id']}"] = p
    out = dict(snap)
    out['posts'] = sorted(posts.values(), key=lambda p: -(p['posted_at'] or 0))[:50]
    out['daily'] = sorted(daily.values(), key=lambda d: d['day'])
    st = c.execute('SELECT MAX(taken_at) FROM stats').fetchone()[0]
    out['stats_at'] = st
    out['received'] = read_json('meta.json', {}).get('received')
    pending = {a['edit_id']: a['action'] for a in read_json('actions.json', [])}
    for r in out.get('review', []):
        r['pending'] = pending.get(r['edit_id'])
        r['has_video'] = os.path.exists(os.path.join(VIDEOS, f"{r['edit_id']}.mp4"))
    return out


# ---------- web ----------
def session_value():
    return hmac.new(PASSWORD.encode(), b'ai-editor-dashboard-session', hashlib.sha256).hexdigest()


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
        return bool(TOKEN) and hmac.compare_digest(self.headers.get('Authorization', ''), f'Bearer {TOKEN}')

    def body(self, limit=5 << 20):
        n = int(self.headers.get('Content-Length') or 0)
        if n > limit:
            raise ValueError('too large')
        return self.rfile.read(n)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == '/healthz':
            return self.send(200, {'ok': True})
        if path == '/api/actions':
            return self.send(200, read_json('actions.json', [])) if self.machine() else self.send(401, {'error': 'unauthorised'})
        if not self.signed_in():
            if path.startswith('/api/') or path.startswith('/video/'):
                return self.send(401, {'error': 'sign in'})
            return self.send(200, LOGIN.replace('__ERR__', ''), 'text/html; charset=utf-8')
        if path in ('/', '/index.html'):
            return self.send(200, open(os.path.join(HERE, 'index.html'), 'rb').read(), 'text/html; charset=utf-8')
        if path == '/api/overview':
            try:
                out = overview()
            except Exception as ex:
                return self.send(500, {'error': f'Dashboard error: {ex}'})
            return self.send(200, out) if out else self.send(503, {'error': 'Waiting for the first sync from the editing Mac'})
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
            reason = (json.loads(self.body(8192) or b'{}').get('reason') or '').strip()[:1000]
            if action == 'reject' and not reason:
                return self.send(400, {'error': 'Say why it was rejected'})
            if not any(r['edit_id'] == eid for r in read_json('snapshot.json', {}).get('review', [])):
                return self.send(409, {'error': f'edit {eid} is not waiting for review'})
            with LOCK:
                acts = [a for a in read_json('actions.json', []) if a['edit_id'] != eid]
                acts.append({'edit_id': eid, 'action': action, 'reason': reason, 'at': time.time()})
                write_json('actions.json', acts)
            return self.send(200, {'ok': True, 'edit_id': eid, 'queued': True})
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
    if not (PASSWORD and TOKEN and os.environ.get('ZERNIO_API_KEY')):
        print('warning: set DASHBOARD_PASSWORD, INGEST_TOKEN and ZERNIO_API_KEY', flush=True)
    threading.Thread(target=refresher, daemon=True).start()
    port = int(os.environ.get('PORT', 8080))
    print(f'AI editor dashboard (hosted) on :{port}, data in {DATA}', flush=True)
    ThreadingHTTPServer(('0.0.0.0', port), H).serve_forever()
