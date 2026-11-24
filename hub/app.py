#!/usr/bin/env python3
"""Central hub for the Real AF AI editor network (hosted on Railway, reachable only through Cloudflare Access).

One place to see and talk to every AI editor, whatever machine it runs on. Matt, 2026-11-24: "there needs to be a
central hub where you can communicate with all the editors in there, regardless of what computer that they live on"
and "even if somebody knows what they're doing, they have no way in unless they are specifically approved".

  people   -> Cloudflare Access (Google sign-in, allowlist) -> front-door Worker -> Cloudflare Tunnel -> this app
  workers  -> same route with their own service token + hub token, connecting out only (worker.py on each machine)

This app has no public address. It re-checks the signed identity on every request (auth.py) and enforces roles
here: viewer < reviewer < director < admin; only Matt (HUB_OWNER_EMAIL) manages people, workers and roles.
Security model: Projects/Real AF/Context/Thoughts/2026-11-24 Central Hub - Security Architecture.md

Run: python3 hub/app.py   (PORT, DATA_DIR, plus the variables in auth.py, storage.py and ZERNIO_API_KEY)
"""
import json, os, re, secrets, sys, threading, time, traceback, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
if hasattr(time, 'tzset'):
    time.tzset()

import auth, clean, clips, overview, storage, store, zernio_hub  # noqa: E402
import directives as dv  # noqa: E402

PORT = int(os.environ.get('PORT', 8080))
ALERT_WEBHOOK = os.environ.get('ALERT_WEBHOOK', '')  # e.g. an ntfy.sh topic URL for Matt's phone
MAX_BODY = 8 << 20
DOCS = ('content_profile', 'performance_playbook', 'output_spec')
RATE = {'director': {'chat': 30, 'directives': 20}}  # per hour, for anyone below admin (Grok Bot)
WAITERS = {}
WLOCK = threading.Lock()


def now():
    return time.time()


def notify(worker_id):
    with WLOCK:
        ev = WAITERS.get(worker_id)
    if ev:
        ev.set()


def notify_editor(editor):
    r = store.db().execute('SELECT worker_id FROM editors WHERE id=?', (editor,)).fetchone()
    if r:
        notify(r['worker_id'])


def send_alert(text, level='warn'):
    store.alert(text, level)
    if ALERT_WEBHOOK:
        def go():
            try:
                urllib.request.urlopen(urllib.request.Request(ALERT_WEBHOOK, data=text.encode(), method='POST',
                                                              headers={'Title': 'AI editor hub', 'Tags': 'lock'}), timeout=10).read()
            except Exception as ex:
                print(f'alert webhook failed: {ex}', flush=True)
        threading.Thread(target=go, daemon=True).start()


def editor_row(eid):
    return store.db().execute("SELECT * FROM editors WHERE id=? AND status='approved'", (eid,)).fetchone()


def item_editor(it):
    """Which editor a clip belongs to, from its own worker's latest report ('__sourcing' for raw moments)."""
    if it['kind'] == 'sourced':
        return '__sourcing'
    row = store.db().execute('SELECT snap FROM snapshots WHERE worker_id=?', (it['worker_id'],)).fetchone()
    snap = json.loads(row['snap']) if row else {}
    for r in snap.get('review', []):
        if r['edit_id'] == it['local_id']:
            return r['brand_set']
    for bs in snap.get('brand_sets', []):
        if any(x['edit_id'] == it['local_id'] for x in bs.get('scheduled', [])):
            return bs['id']
    return None


def chat_payload(eid, after=0):
    c = store.db()
    msgs = [dict(r) for r in c.execute('SELECT * FROM chat WHERE editor=? AND id>? ORDER BY id DESC LIMIT 100', (eid, after))][::-1]
    ds = [dict(r) for r in c.execute('SELECT id, action, params, summary, status, requested_by, decided_by, message_id, created, applied_at, '
                                     'result, undone_at FROM directives WHERE editor=? ORDER BY id DESC LIMIT 100', (eid,))]
    for d in ds:
        d['params'] = json.loads(d['params'] or '{}')
    job = c.execute("SELECT status, created FROM chat_jobs WHERE editor=? AND status IN ('pending','sent') ORDER BY id DESC LIMIT 1", (eid,)).fetchone()
    return {'messages': msgs, 'directives': ds, 'thinking': dict(job) if job else None}


def recent(table, col, who_id, secs=3600):
    return store.db().execute(f'SELECT COUNT(*) FROM {table} WHERE {col}=? AND {"at" if table == "chat" else "created"}>?',
                              (who_id, now() - secs)).fetchone()[0]


# ---------- what workers get ----------
def pending_for(worker_id):
    """Everything waiting for this worker. Items sent over 10 minutes ago without an answer are sent again."""
    c = store.db()
    stale = now() - 600
    eds = [r['id'] for r in c.execute("SELECT id FROM editors WHERE worker_id=? AND status='approved'", (worker_id,))]
    q = ','.join('?' * len(eds)) or "''"
    acts = [dict(r) for r in c.execute('SELECT id, kind, local_id, editor, action, reason, by_email FROM review_actions '
                                        'WHERE worker_id=? AND done_at IS NULL AND (sent_at IS NULL OR sent_at<?)', (worker_id, stale))]
    ds = [dict(r) for r in c.execute(f"SELECT id, editor, action, params, requested_by FROM directives WHERE editor IN ({q}) AND action!='add_rule' "
                                     "AND status='approved' AND (sent_at IS NULL OR sent_at<?)", (*eds, stale))]
    undos = [dict(r) for r in c.execute(f"SELECT id, editor, action, undo FROM directives WHERE editor IN ({q}) AND status='applied' "
                                        "AND undo_requested=1 AND action!='add_rule'", eds)]
    jobs = []
    for j in c.execute("SELECT * FROM chat_jobs WHERE worker_id=? AND (status='pending' OR (status='sent' AND sent_at<?))", (worker_id, stale)).fetchall():
        hist = [{'role': m['role'], 'author': m['author'], 'text': m['text'], 'at': m['at']}
                for m in c.execute('SELECT * FROM chat WHERE editor=? AND id<=? ORDER BY id DESC LIMIT 30', (j['editor'], j['message_id']))][::-1]
        u = c.execute('SELECT name FROM users WHERE email=?', (j['by_email'],)).fetchone()
        jobs.append({'job_id': j['id'], 'editor': j['editor'], 'history': hist,
                     'requester': {'name': (u['name'] if u else None) or j['by_email'], 'role': j['by_role'], 'owner': j['by_email'] == auth.OWNER}})
    for d in ds:
        d['params'] = json.loads(d['params'])
    for u in undos:
        u['undo'] = json.loads(u['undo'] or '{}')
    return {'review_actions': acts, 'directives': ds, 'undos': undos, 'chat_jobs': jobs}


def mark_sent(p):
    c = store.db()
    t = now()
    for a in p['review_actions']:
        c.execute('UPDATE review_actions SET sent_at=? WHERE id=?', (t, a['id']))
    for d in p['directives']:
        c.execute('UPDATE directives SET sent_at=? WHERE id=?', (t, d['id']))
    for j in p['chat_jobs']:
        c.execute("UPDATE chat_jobs SET status='sent', sent_at=? WHERE id=?", (t, j['job_id']))


def current_role(email):
    if email == auth.OWNER:
        return 'admin'
    r = store.db().execute('SELECT role FROM users WHERE email=? AND active=1', (email,)).fetchone()
    return r['role'] if r else 'none'


def take_chat_reply(worker, a):
    """A worker's chat agent answered: store the reply and turn its proposed actions into checked directives."""
    c = store.db()
    j = c.execute('SELECT * FROM chat_jobs WHERE id=? AND worker_id=?', (a.get('job_id'), worker.id)).fetchone()
    if not j or j['status'] == 'done':
        return
    if a.get('error'):
        c.execute("UPDATE chat_jobs SET status='done', done_at=?, error=? WHERE id=?", (now(), str(a['error'])[:500], j['id']))
        c.execute('INSERT INTO chat(editor,role,author,text,at) VALUES(?,?,?,?,?)',
                  (j['editor'], 'system', 'hub', f"The editor couldn't answer: {str(a['error'])[:200]}", now()))
        return
    cur = c.execute('INSERT INTO chat(editor,role,author,text,at) VALUES(?,?,?,?,?)',
                    (j['editor'], 'assistant', j['editor'], str(a.get('reply') or '')[:6000], now()))
    mid, made = cur.lastrowid, []
    role = current_role(j['by_email'])  # a person demoted or removed since asking gets no more than they have now
    for act in (a.get('actions') or [])[:5] if isinstance(a.get('actions'), list) else []:
        act = act if isinstance(act, dict) else {}
        name, params = act.get('action'), act.get('params') if isinstance(act.get('params'), dict) else {}
        try:
            p = dv.validate(name, params, role)
            limit = RATE.get(j['by_role'], {}).get('directives')
            if limit and recent('directives', 'requested_by', j['by_email']) >= limit:
                raise dv.Invalid(f'rate limit: at most {limit} directives an hour')
            st = 'proposed' if dv.needs_approval(name, role, p, owner=j['by_email'] == auth.OWNER) else 'approved'
            summary = dv.describe(name, p)
        except dv.Invalid as ex:
            p, st, summary = params, 'refused', f'{name}: {ex}'
        d = c.execute('INSERT INTO directives(editor,action,params,summary,status,requested_by,requested_role,message_id,created) '
                      'VALUES(?,?,?,?,?,?,?,?,?)', (j['editor'], str(name)[:40], json.dumps(p)[:4000], summary[:500], st, j['by_email'],
                                                    role, mid, now())).lastrowid
        store.audit(j['by_email'], 'user', f'directive.{st}', j['editor'], {'directive': d, 'action': name, 'params': p, 'via': 'chat'})
        made.append(d)
        if st == 'proposed':
            send_alert(f"{j['editor']}: a change waits for your approval: {summary}", 'info')
    c.execute('UPDATE chat SET directives=? WHERE id=?', (json.dumps(made), mid))
    c.execute("UPDATE chat_jobs SET status='done', done_at=? WHERE id=?", (now(), j['id']))
    if made:
        notify(worker.id)


def take_acks(worker, acks):
    c = store.db()
    for a in acks.get('review', []):
        r = c.execute('SELECT * FROM review_actions WHERE id=? AND worker_id=?', (a.get('id'), worker.id)).fetchone()
        if r and not r['done_at']:
            c.execute('UPDATE review_actions SET done_at=?, result=? WHERE id=?', (now(), 'ok' if a.get('ok') else str(a.get('error'))[:300], r['id']))
            store.audit(worker.id, 'worker', 'review.applied', f"{r['kind']}:{r['local_id']}", {'ok': a.get('ok'), 'error': a.get('error')})
    for a in acks.get('directives', []):
        d = c.execute("SELECT * FROM directives WHERE id=? AND status='approved'", (a.get('id'),)).fetchone()
        if not d or d['editor'] not in worker.editors:
            continue
        st = 'applied' if a.get('ok') else 'failed'
        c.execute('UPDATE directives SET status=?, applied_at=?, result=?, undo=? WHERE id=?',
                  (st, now(), str(a.get('result') or a.get('error') or '')[:500], json.dumps(a.get('undo') or {}), d['id']))
        c.execute('INSERT INTO chat(editor,role,author,text,at) VALUES(?,?,?,?,?)',
                  (d['editor'], 'system', 'hub', f"{'Done' if st == 'applied' else 'Failed'}: {a.get('result') or a.get('error') or d['summary']}", now()))
        store.audit(worker.id, 'worker', f'directive.{st}', d['editor'], {'directive': d['id'], 'result': a.get('result') or a.get('error')})
    for a in acks.get('undos', []):
        d = c.execute("SELECT * FROM directives WHERE id=? AND status='applied' AND undo_requested=1", (a.get('id'),)).fetchone()
        if not d or d['editor'] not in worker.editors:
            continue
        if a.get('ok'):
            c.execute("UPDATE directives SET status='undone', undone_at=? WHERE id=?", (now(), d['id']))
        else:
            c.execute('UPDATE directives SET undo_requested=0 WHERE id=?', (d['id'],))
        c.execute('INSERT INTO chat(editor,role,author,text,at) VALUES(?,?,?,?,?)',
                  (d['editor'], 'system', 'hub', f"{'Undone' if a.get('ok') else 'Undo failed'}: {a.get('result') or a.get('error') or d['summary']}", now()))
        store.audit(worker.id, 'worker', 'directive.undone' if a.get('ok') else 'directive.undo_failed', d['editor'], {'directive': d['id']})
    for a in acks.get('chat', []):
        take_chat_reply(worker, a)


def learning(worker, push):
    """Network-wide learning across machines: the content profile and performance playbook, every learned rule
    and every review reason reach every editor (Matt: "I want all new lessons to be applied to all agents")."""
    c = store.db()
    out = {'docs': {}, 'rules': [], 'feedback': [], 'conflicts': []}
    lead = store.meta('lead_worker')
    for name, d in clean.dct(push.get('docs')).items():
        d = clean.dct(d)
        if name not in DOCS or not isinstance(d.get('text'), str):
            continue
        if name == 'output_spec' and worker.id != lead:
            continue  # Matt's written spec comes only from the lead machine (his Mac), never from other workers
        cur = c.execute('SELECT version, text FROM docs WHERE name=?', (name,)).fetchone()
        if cur is None or cur['version'] == d.get('base'):
            v = (cur['version'] if cur else 0) + 1
            text = d['text'][:400000]
            c.execute('INSERT OR REPLACE INTO docs VALUES(?,?,?,?,?)', (name, v, text, worker.id, now()))
            c.execute('INSERT INTO docs_history(name,version,text,by,at) VALUES(?,?,?,?,?)', (name, v, text, worker.id, now()))
            store.audit(worker.id, 'worker', 'doc.update', name, {'version': v, 'chars': len(text), 'was': len(cur['text']) if cur else 0})
        else:
            out['conflicts'].append(name)
    known = push.get('known') or {}
    for r in c.execute('SELECT * FROM docs'):
        if known.get(r['name']) != r['version']:
            out['docs'][r['name']] = {'version': r['version'], 'text': r['text']}
    new_today = c.execute('SELECT COUNT(*) FROM net_rules WHERE origin=? AND at>?', (worker.id, now() - 86400)).fetchone()[0]
    for r in clean.lst(push.get('rules'), 500):
        r = clean.dct(r)
        uid = f"{worker.id}-{clean.i(r.get('local_id'), 0)}"
        ed = r.get('editor')
        if ed is not None and ed not in worker.editors:
            continue
        old = c.execute('SELECT rule, active FROM net_rules WHERE uid=?', (uid,)).fetchone()
        rule, active = clean.s(r.get('rule'), 1000), 1 if r.get('active') else 0
        if old and old['rule'] == rule and old['active'] == active:
            continue
        if not old:
            if new_today >= 40:  # a machine learning more than this in a day is broken or compromised: stop and tell Matt
                if not store.meta(f'rules_capped_{worker.id}'):
                    send_alert(f'Worker {worker.label()} sent more than 40 new network rules in a day; the rest are held back.')
                    store.set_meta(f'rules_capped_{worker.id}', True)
                break
            new_today += 1
        c.execute('INSERT OR REPLACE INTO net_rules VALUES(?,?,?,?,?,?,?,?,?)', (uid, clean.s(r.get('kind'), 10), rule, clean.s(r.get('reason'), 1000),
                  ed, active, worker.id, clean.t(r.get('at')) or now(), now()))
        store.audit(worker.id, 'worker', 'rule.learned' if not old else 'rule.changed', uid, {'rule': rule[:300], 'editor': ed, 'active': active})
    eds = worker.editors or []
    out['rules'] = [dict(r) for r in c.execute('SELECT * FROM net_rules WHERE origin!=?', (worker.id,))
                    if r['editor'] is None or r['editor'] in eds]
    out['feedback_upto'] = None
    for f in clean.lst(push.get('feedback'), 500):
        f = clean.dct(f)
        if f.get('editor') not in eds:
            break  # not approved here yet: the worker sends it again later
        out['feedback_upto'] = clean.i(f.get('local_id'), 0)
        c.execute('INSERT OR IGNORE INTO net_feedback VALUES(?,?,?,?,?,?,?,?,?)', (f"{worker.id}-{out['feedback_upto']}", f['editor'],
                  'reject' if f.get('action') == 'reject' else 'approve', clean.s(f.get('reason'), 1000), clean.s(f.get('hook'), 300),
                  clean.s(f.get('note'), 1500), clean.s(f.get('transcript'), 600), clean.t(f.get('at')) or now(), worker.id))
    since = clean.t(push.get('feedback_since')) or 0
    out['feedback'] = [dict(r) for r in c.execute('SELECT * FROM net_feedback WHERE origin!=? AND at>? ORDER BY at LIMIT 300', (worker.id, since))]
    return out


def worker_sync(worker, body, ip):
    c = store.db()
    info = body.get('info') or {}
    c.execute('UPDATE workers SET last_seen=?, last_ip=?, info=? WHERE id=?', (now(), ip, json.dumps(info)[:2000], worker.id))
    # editors this machine says it runs: new ones wait for Matt to approve them (and lock their accounts)
    new = []
    for eid, e in list(clean.dct(body.get('editors')).items())[:50]:
        if not clean.eid(eid) or not isinstance(e, dict):
            continue
        r = c.execute('SELECT * FROM editors WHERE id=?', (eid,)).fetchone()
        settings = clean.dct(e.get('editor'))
        blob = lambda v: json.dumps(v)[:50000]
        if r is None:
            if len(new) >= 5 or c.execute("SELECT COUNT(*) FROM editors WHERE worker_id=? AND status='pending'", (worker.id,)).fetchone()[0] >= 20:
                continue
            accts = {k: v for k, v in clean.dct(settings.get('accounts')).items() if k in clean.PLATFORMS and isinstance(v, str) and clean.REMOTE.match(v)}
            c.execute("INSERT INTO editors(id,worker_id,name,accounts,status,settings,style,notes,reported_at) VALUES(?,?,?,?,?,?,?,?,?)",
                      (eid, worker.id, clean.s(settings.get('name'), 60), json.dumps(accts), 'pending',
                       blob(settings), blob(clean.dct(e.get('style'))), blob(clean.lst(e.get('notes'), 200)), now()))
            new.append(eid)
        elif r['worker_id'] == worker.id:
            if r['status'] == 'approved' and json.loads(r['accounts'] or '{}') != (settings.get('accounts') or {}):
                if not store.meta(f'acct_warned_{eid}'):
                    send_alert(f"{eid} on {worker.label()} now lists different accounts than the ones you approved; posting stays limited to the approved ones.")
                    store.set_meta(f'acct_warned_{eid}', True)
            c.execute('UPDATE editors SET settings=?, style=?, notes=?, reported_at=? WHERE id=?',
                      (blob(settings), blob(clean.dct(e.get('style'))), blob(clean.lst(e.get('notes'), 200)), now(), eid))
    if new:
        send_alert(f"Worker {worker.label()} reported new editor(s) {', '.join(new)}; approve them in the hub's Access panel before they can post.")
    if body.get('snapshot') is not None:
        c.execute('INSERT OR REPLACE INTO snapshots VALUES(?,?,?)', (worker.id, json.dumps(clean.snapshot(body['snapshot'])), now()))
    take_acks(worker, body.get('acks') or {})
    out = pending_for(worker.id)
    mark_sent(out)
    out['learning'] = learning(worker, body.get('learning') or {})
    # clips the page needs that the hub doesn't have yet: one signed upload link each (a few per sync)
    have, want = clips.have(), []
    for x in clean.lst(body.get('clips'), 200):
        x = clean.dct(x)
        if x.get('kind') not in ('edit', 'sourced'):
            continue
        x['id'] = clean.i(x.get('id'), 0)
        g = store.gid(worker.id, x['kind'], x['id'])
        if g not in have and len(want) < 4:
            want.append({'kind': x['kind'], 'id': x['id'], 'gid': g, 'url': clips.upload_url(g)})
    out['upload'] = want
    return out


# ---------- web ----------
PAGE = os.path.join(HERE, 'index.html')


def csp():
    media = ' '.join(x for x in ("'self'", storage.origin()) if x)
    return (f"default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            f"media-src {media} blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


class H(BaseHTTPRequestHandler):
    server_version = 'hub'
    sys_version = ''

    def log_message(self, *a):
        pass

    def ip(self):
        return self.headers.get('Cf-Connecting-IP') or self.headers.get('X-Forwarded-For', '').split(',')[0].strip() or self.client_address[0]

    def send(self, code, body, ctype='application/json', headers=None):
        b = body if isinstance(body, bytes) else (body.encode() if isinstance(body, str) else json.dumps(body, default=str).encode())
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Strict-Transport-Security', 'max-age=31536000')
        self.send_header('Permissions-Policy', 'camera=(), geolocation=(), microphone=(self)')
        self.send_header('Content-Security-Policy', csp())
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(b)

    def body(self, limit=MAX_BODY):
        n = int(self.headers.get('Content-Length') or 0)
        if n < 0 or n > limit:
            raise ValueError('bad length')
        return self.rfile.read(n) if n else b''

    def json_body(self, limit=MAX_BODY):
        b = self.body(limit)
        return json.loads(b) if b else {}

    def deny(self, why):
        print(f"{time.strftime('%H:%M:%S')} denied {self.command} {self.path[:80]} from {self.ip()}: {why}", flush=True)
        return self.send(403, b'Not approved.', 'text/plain')

    def who(self):
        missing = auth.configured()
        if missing:
            return None, 'hub not configured: ' + ', '.join(missing)
        return auth.identify(self.headers)

    def track_login(self, w):
        """New-login alerts: each new Access session is recorded; a new device or country tells Matt."""
        c = store.db()
        ip, country, agent = self.ip(), self.headers.get('Cf-IPCountry', ''), self.headers.get('User-Agent', '')[:200]
        if c.execute('SELECT 1 FROM logins WHERE identity=? AND session=?', (w.id, w.session)).fetchone():
            return
        seen = c.execute('SELECT 1 FROM logins WHERE identity=? AND country=? AND agent=?', (w.id, country, agent)).fetchone()
        first = not c.execute('SELECT 1 FROM logins WHERE identity=?', (w.id,)).fetchone()
        c.execute('INSERT OR IGNORE INTO logins(identity,session,ip,country,agent,at,new_device) VALUES(?,?,?,?,?,?,?)',
                  (w.id, w.session, ip, country, agent, now(), 0 if seen else 1))
        store.audit(w.id, w.kind, 'login', '', {'ip': ip, 'country': country, 'agent': agent, 'new_device': not seen}, ip)
        if not seen:
            send_alert(f"{'First' if first else 'New device'} sign-in to the hub: {w.label()} from {country or '?'} ({ip}), {agent[:80]}")

    def handle_one(self, method):
        try:
            w, why = self.who()
            if not w:
                return self.deny(why)
            path = urllib.parse.urlparse(self.path).path
            if w.kind == 'worker':
                return self.worker_route(w, method, path)
            self.track_login(w)
            if method != 'GET':
                # no cross-site requests: the page's own fetches carry this header, a form or link from elsewhere can't
                origin = self.headers.get('Origin')
                host = self.headers.get('X-Forwarded-Host') or self.headers.get('Host')  # set by the gate (trusted: gate secret checked)
                if self.headers.get('X-Hub') != '1' or (origin and urllib.parse.urlparse(origin).netloc != host):
                    return self.send(403, {'error': 'cross-site request refused'})
            return self.user_route(w, method, path)
        except ValueError as ex:
            return self.send(400, {'error': str(ex)[:200]})
        except Exception as ex:
            print(f'error {method} {self.path[:80]}: {ex}\n{traceback.format_exc()}', flush=True)
            return self.send(500, {'error': 'hub error'})

    def do_GET(self):
        if self.path == '/healthz':  # for the tunnel's own health check; says nothing
            return self.send(200, b'ok', 'text/plain')
        self.handle_one('GET')

    def do_HEAD(self):
        self.handle_one('GET')

    def do_POST(self):
        self.handle_one('POST')

    def do_PUT(self):
        self.handle_one('PUT')

    # ----- people -----
    def user_route(self, w, method, path):
        c = store.db()
        if method == 'GET':
            if path in ('/', '/index.html'):
                return self.send(200, open(PAGE, 'rb').read(), 'text/html; charset=utf-8')
            if path == '/api/me':
                return self.send(200, {'email': w.id, 'name': w.label(), 'role': w.role, 'owner': w.owner, 'editors': w.editors,
                                       'fresh': w.fresh(), 'actions': dv.CATALOGUE if w.can('director') else {}})
            if path == '/api/overview':
                return self.send(200, overview.build(w))
            m = re.match(r'^/(video|sourced)/(\d+)$', path)
            if m:
                return self.video(w, int(m.group(2)), 'sourced' if m.group(1) == 'sourced' else 'edit')
            m = re.match(r'^/api/chat/([a-z0-9-]{1,40})$', path)
            if m:
                if not w.can('director') or not w.sees(m.group(1)):
                    return self.send(403, {'error': 'not your editor'})
                after = int(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('after', ['0'])[0] or 0)
                return self.send(200, chat_payload(m.group(1), after))
            if path == '/api/proposals':
                rows = [dict(r) for r in c.execute("SELECT * FROM directives WHERE status='proposed' ORDER BY id")] if w.can('admin') else []
                return self.send(200, {'proposals': rows})
            if path == '/api/admin':
                if not w.can('admin'):
                    return self.send(403, {'error': 'admins only'})
                return self.send(200, {
                    'users': [dict(r) for r in c.execute('SELECT email, name, role, editors, client_id, active, added_at FROM users ORDER BY email')],
                    'owner': auth.OWNER,
                    'workers': [dict(r) for r in c.execute('SELECT id, name, access_client_id, active, created_at, last_seen, last_ip, info FROM workers')],
                    'editors': [dict(r, handles=clean.dct(json.loads(r['settings'] or '{}').get('handles')),
                                     usernames={x['account']: x['username'] for x in c.execute('SELECT account, username FROM zfollowers GROUP BY account')})
                                for r in c.execute('SELECT id, worker_id, name, accounts, settings, status, reported_at, approved_by FROM editors')],
                    'lead_worker': store.meta('lead_worker'),
                    'alerts': [dict(r) for r in c.execute('SELECT * FROM alerts ORDER BY id DESC LIMIT 50')],
                    'logins': [dict(r) for r in c.execute('SELECT identity, ip, country, agent, at, new_device FROM logins ORDER BY id DESC LIMIT 30')],
                    'audit_ok': store.verify_audit()})
            if path == '/api/audit':
                if not w.can('admin'):
                    return self.send(403, {'error': 'admins only'})
                before = int(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('before', ['0'])[0] or 0) or 1 << 62
                return self.send(200, {'entries': [dict(r) for r in c.execute('SELECT id, at, actor, actor_kind, action, target, detail, ip FROM audit '
                                                                           'WHERE id<? ORDER BY id DESC LIMIT 100', (before,))]})
            return self.send(404, {'error': 'not found'})

        if method != 'POST':
            return self.send(405, {'error': 'no'})
        a = self.json_body(64 << 10)
        m = re.match(r'^/api/(edit|sourced)/(\d+)/(approve|reject)$', path)
        if m:
            return self.review(w, m.group(1), int(m.group(2)), m.group(3), str(a.get('reason') or '').strip()[:1000])
        m = re.match(r'^/api/chat/([a-z0-9-]{1,40})$', path)
        if m:
            return self.chat(w, m.group(1), str(a.get('text') or '').strip())
        m = re.match(r'^/api/directive/(\d+)/(approve|reject|undo)$', path)
        if m:
            return self.decide_directive(w, int(m.group(1)), m.group(2))
        if path.startswith('/api/admin/'):
            return self.admin(w, path[len('/api/admin/'):], a)
        return self.send(404, {'error': 'not found'})

    def video(self, w, g, kind):
        it = store.item(g)
        if not it or it['kind'] != kind:
            return self.send(404, {'error': 'no video'})
        r = store.db().execute('SELECT * FROM clips WHERE gid=?', (g,)).fetchone()
        if not w.sees(item_editor(it)):
            return self.send(403, {'error': 'not your editor'})
        if not r:
            return self.send(404, {'error': 'no video yet'})
        if storage.ENABLED:
            return self.send(302, b'', 'text/plain', {'Location': clips.watch_url(g)})
        p = os.path.join(clips.LOCAL, f'{g}.mp4')
        return self.send(200, open(p, 'rb').read(), 'video/mp4') if os.path.exists(p) else self.send(404, {'error': 'no video'})

    def review(self, w, kind, g, action, reason):
        if not w.can('reviewer'):
            return self.send(403, {'error': 'reviewers only'})
        if action == 'reject' and not reason:
            return self.send(400, {'error': 'Say why it was rejected'})
        it = store.item(g)
        if not it or it['kind'] != kind:
            return self.send(404, {'error': 'no such clip'})
        snap = json.loads((store.db().execute('SELECT snap FROM snapshots WHERE worker_id=?', (it['worker_id'],)).fetchone() or {'snap': '{}'})['snap'])
        if kind == 'edit':
            row = next((r for r in snap.get('review', []) if r['edit_id'] == it['local_id']), None)
            editor = row and row['brand_set']
        else:
            row = next((r for r in (snap.get('sourcing') or {}).get('queue', []) if r['id'] == it['local_id']), None)
            editor = '__sourcing'
        if not row:
            return self.send(409, {'error': 'that clip is not waiting for review'})
        if not w.sees(editor):
            return self.send(403, {'error': 'not your editor'})
        c = store.db()
        c.execute('DELETE FROM review_actions WHERE gid=? AND sent_at IS NULL AND done_at IS NULL', (g,))
        c.execute('INSERT INTO review_actions(gid,worker_id,kind,local_id,editor,action,reason,by_email,at) VALUES(?,?,?,?,?,?,?,?,?)',
                  (g, it['worker_id'], kind, it['local_id'], editor, action, reason, w.id, now()))
        store.audit(w.id, 'user', f'review.{action}', f'{kind}:{g}', {'editor': editor, 'reason': reason}, self.ip())
        notify(it['worker_id'])
        return self.send(200, {'ok': True, 'edit_id': g, 'queued': True})

    def chat(self, w, eid, text):
        if not w.can('director'):
            return self.send(403, {'error': 'only directors and admins can talk to editors'})
        if not w.sees(eid) or not editor_row(eid):
            return self.send(404, {'error': 'no such editor'})
        if not 1 <= len(text) <= 4000:
            return self.send(400, {'error': 'say something (up to 4000 characters)'})
        limit = RATE.get(w.role, {}).get('chat')
        if limit and recent('chat', 'author', w.id) >= limit:
            return self.send(429, {'error': f'rate limit: {limit} messages an hour'})
        c = store.db()
        mid = c.execute('INSERT INTO chat(editor,role,author,text,at) VALUES(?,?,?,?,?)', (eid, 'user', w.id, text, now())).lastrowid
        waiting = c.execute("SELECT id FROM chat_jobs WHERE editor=? AND status='pending' AND by_email=?", (eid, w.id)).fetchone()
        if waiting:  # not picked up yet: fold this message into the same turn
            c.execute('UPDATE chat_jobs SET message_id=? WHERE id=?', (mid, waiting['id']))
        else:
            c.execute("INSERT INTO chat_jobs(editor,worker_id,message_id,by_email,by_role,status,created) VALUES(?,?,?,?,?,?,?)",
                      (eid, editor_row(eid)['worker_id'], mid, w.id, w.role, 'pending', now()))
        store.audit(w.id, 'user', 'chat', eid, {'message': mid, 'text': text[:500]}, self.ip())
        notify_editor(eid)
        return self.send(200, chat_payload(eid))

    def decide_directive(self, w, did, what):
        c = store.db()
        d = c.execute('SELECT * FROM directives WHERE id=?', (did,)).fetchone()
        if not d or not w.sees(d['editor']):
            return self.send(404, {'error': 'no such directive'})
        if what in ('approve', 'reject'):
            if not w.owner:
                return self.send(403, {'error': 'only Matt approves changes'})
            if not w.fresh():
                return self.send(401, {'error': 'sign in again to approve changes', 'reauth': True})
            if d['status'] != 'proposed':
                return self.send(409, {'error': f"already {d['status']}"})
            st = 'rejected' if what == 'reject' else ('applied' if d['action'] == 'add_rule' else 'approved')
            c.execute('UPDATE directives SET status=?, decided_by=?, decided_at=? WHERE id=?', (st, w.id, now(), did))
            if d['action'] == 'add_rule' and st == 'applied':  # network rule: every worker picks it up on its next sync
                p = json.loads(d['params'])
                c.execute('INSERT OR REPLACE INTO net_rules VALUES(?,?,?,?,?,?,?,?,?)', (f'd{did}', p['kind'], p['text'], f"directive from {d['requested_by']}",
                          None, 1, 'hub', now(), now()))
                c.execute('UPDATE directives SET applied_at=?, result=?, undo=? WHERE id=?', (now(), 'Rule added for every editor', json.dumps({'uid': f'd{did}'}), did))
                for r in c.execute('SELECT id FROM workers WHERE active=1'):
                    notify(r['id'])
            c.execute('INSERT INTO chat(editor,role,author,text,at) VALUES(?,?,?,?,?)', (d['editor'], 'system', 'hub',
                      f"Matt {'approved' if what == 'approve' else 'turned down'}: {d['summary']}", now()))
            store.audit(w.id, 'user', f'directive.{what}', d['editor'], {'directive': did}, self.ip())
            notify_editor(d['editor'])
            return self.send(200, {'ok': True, 'status': st})
        # undo
        if d['status'] != 'applied':
            return self.send(409, {'error': 'only applied changes can be undone'})
        if not w.owner and (not w.can('director') or d['requested_by'] != w.id or d['action'] == 'add_rule'):
            return self.send(403, {'error': 'you can undo only your own changes; Matt can undo any'})
        undo_vals = json.loads(d['undo'] or '{}')
        if d['action'] == 'set_posting' and not w.owner and dv.needs_approval('set_posting', w.role, {k: v for k, v in undo_vals.items() if k in ('enabled', 'auto_post')}):
            return self.send(403, {'error': 'undoing this would turn posting back on; only Matt can'})
        if d['action'] == 'reedit':
            return self.send(409, {'error': 'a re-edit made a new version; reject it in review instead'})
        if d['action'] == 'add_rule':
            c.execute('UPDATE net_rules SET active=0, updated=? WHERE uid=?', (now(), f'd{did}'))
            c.execute("UPDATE directives SET status='undone', undone_at=? WHERE id=?", (now(), did))
            for r in c.execute('SELECT id FROM workers WHERE active=1'):
                notify(r['id'])
        else:
            c.execute('UPDATE directives SET undo_requested=1 WHERE id=?', (did,))
            notify_editor(d['editor'])
        store.audit(w.id, 'user', 'directive.undo', d['editor'], {'directive': did}, self.ip())
        return self.send(200, {'ok': True})

    def admin(self, w, what, a):
        """People, workers and editors. Only Matt, signed in recently."""
        if not w.owner:
            return self.send(403, {'error': 'only Matt manages people, workers and roles'})
        if not w.fresh():
            return self.send(401, {'error': 'sign in again to change access', 'reauth': True})
        c = store.db()
        if what == 'users':
            email = str(a.get('email') or '').strip().lower()
            role = a.get('role')
            if not re.match(r'^[a-z0-9._%+-]{1,64}@[a-z0-9.-]{1,180}\.[a-z]{2,24}$', email) or email == auth.OWNER:
                return self.send(400, {'error': 'give an email address (not your own)'})
            if role not in ('viewer', 'reviewer', 'director'):
                return self.send(400, {'error': 'role is viewer, reviewer or director (admin is only Matt)'})
            eds = a.get('editors')
            if eds is not None and not (isinstance(eds, list) and all(isinstance(x, str) for x in eds)):
                return self.send(400, {'error': 'editors is a list of editor ids, or empty for all'})
            cid = str(a.get('client_id') or '').strip() or None
            if cid and not re.match(r'^[A-Za-z0-9._-]{4,100}$', cid):
                return self.send(400, {'error': 'that service token client id looks wrong'})
            if eds and not all(clean.eid(x) for x in eds):
                return self.send(400, {'error': 'editor ids look like editor-01'})
            c.execute('INSERT OR REPLACE INTO users(email,name,role,editors,client_id,active,added_by,added_at) VALUES(?,?,?,?,?,1,?,?)',
                      (email, str(a.get('name') or '')[:60], role, json.dumps(eds) if eds else None, cid, w.id, now()))
            store.audit(w.id, 'user', 'user.set', email, {'role': role, 'editors': eds, 'client_id': cid}, self.ip())
            send_alert(f'Hub access changed: {email} is now {role}.', 'info')
            return self.send(200, {'ok': True})
        m = re.match(r'^users/(.+)/remove$', what)
        if m:
            email = urllib.parse.unquote(m.group(1)).lower()
            if not c.execute('UPDATE users SET active=0 WHERE email=? AND active=1', (email,)).rowcount:
                return self.send(404, {'error': f'{email} is not an active user'})
            store.audit(w.id, 'user', 'user.remove', email, {}, self.ip())
            send_alert(f'Hub access removed: {email}.', 'info')
            return self.send(200, {'ok': True})
        if what == 'workers':
            wid = str(a.get('id') or '').strip().lower()
            cid = str(a.get('access_client_id') or '').strip()
            if not re.match(r'^[a-z0-9-]{2,30}$', wid) or not re.match(r'^[A-Za-z0-9._-]{4,100}$', cid):
                return self.send(400, {'error': 'give a short worker id (letters, digits, dashes) and its Access service token client id'})
            tok = secrets.token_urlsafe(32)
            c.execute('INSERT OR REPLACE INTO workers(id,name,token_hash,access_client_id,active,created_by,created_at) VALUES(?,?,?,?,1,?,?)',
                      (wid, str(a.get('name') or wid)[:60], auth.token_hash(tok), cid, w.id, now()))
            if not store.meta('lead_worker'):  # the first machine (Matt's Mac) holds the written Output Spec
                store.set_meta('lead_worker', wid)
            store.audit(w.id, 'user', 'worker.create', wid, {'access_client_id': cid}, self.ip())
            return self.send(200, {'ok': True, 'id': wid, 'token': tok, 'note': 'Shown once. Put it in the worker\'s secrets file.'})
        m = re.match(r'^workers/([a-z0-9-]+)/(revoke|restore)$', what)
        if m:
            c.execute('UPDATE workers SET active=? WHERE id=?', (1 if m.group(2) == 'restore' else 0, m.group(1)))
            store.audit(w.id, 'user', f'worker.{m.group(2)}', m.group(1), {}, self.ip())
            return self.send(200, {'ok': True})
        m = re.match(r'^editors/([a-z0-9-]+)/(approve|remove)$', what)
        if m:
            r = c.execute('SELECT * FROM editors WHERE id=?', (m.group(1),)).fetchone()
            if not r:
                return self.send(404, {'error': 'no such editor'})
            if m.group(2) == 'approve':
                accts = json.loads(r['accounts'] or '{}')
                taken = {}
                for o in c.execute("SELECT id, accounts FROM editors WHERE status='approved' AND id!=?", (r['id'],)):
                    for aid in json.loads(o['accounts'] or '{}').values():
                        taken[aid] = o['id']
                clash = [f"{pf} {aid} (already {taken[aid]})" for pf, aid in accts.items() if aid in taken]
                if clash:
                    return self.send(409, {'error': 'these accounts belong to another editor: ' + ', '.join(clash)})
                settings = json.loads(r['settings'] or '{}')
                c.execute("UPDATE editors SET status='approved', accounts=?, name=?, approved_by=?, approved_at=? WHERE id=?",
                          (json.dumps(accts), settings.get('name'), w.id, now(), r['id']))
                store.set_meta(f"acct_warned_{r['id']}", False)
                store.audit(w.id, 'user', 'editor.approve', r['id'], {'worker': r['worker_id'], 'accounts': accts}, self.ip())
            else:
                c.execute("UPDATE editors SET status='removed' WHERE id=?", (r['id'],))
                store.audit(w.id, 'user', 'editor.remove', r['id'], {}, self.ip())
            notify(r['worker_id'])
            return self.send(200, {'ok': True})
        if what == 'alerts/seen':
            c.execute('UPDATE alerts SET seen=1')
            return self.send(200, {'ok': True})
        return self.send(404, {'error': 'not found'})

    # ----- workers -----
    def worker_route(self, w, method, path):
        if method == 'GET' and path == '/api/worker/wait':
            # long poll: answer as soon as there's work, else after ~25 s (keeps chat replies quick without hammering)
            with WLOCK:
                ev = WAITERS.setdefault(w.id, threading.Event())
            p = pending_for(w.id)
            if not any(p.values()):
                ev.clear()
                ev.wait(25)
            return self.send(200, {'work': True})
        if method == 'POST' and path == '/api/worker/sync':
            return self.send(200, worker_sync(w, self.json_body(24 << 20), self.ip()))
        if method == 'POST' and path == '/api/worker/clip-done':
            a = self.json_body()
            if a.get('kind') not in ('edit', 'sourced'):
                return self.send(400, {'error': 'bad kind'})
            clips.done(store.gid(w.id, a['kind'], a['id']), a.get('size') or 0)
            return self.send(200, {'ok': True})
        m = re.match(r'^/api/worker/clip-local/(\d+)$', path)
        if method == 'PUT' and m and not storage.ENABLED and auth.TEST:
            it = store.item(int(m.group(1)))
            if not it or it['worker_id'] != w.id:
                return self.send(403, {'error': 'not yours'})
            os.makedirs(clips.LOCAL, exist_ok=True)
            open(os.path.join(clips.LOCAL, f'{m.group(1)}.mp4'), 'wb').write(self.body(400 << 20))
            return self.send(200, {'ok': True})
        m = re.match(r'^/api/worker/zernio/([a-z_]+)$', path)
        if method == 'POST' and m:
            if not zernio_hub.enabled():
                return self.send(503, {'error': 'posting is not set up on the hub', 'temporary': True})
            try:
                return self.send(200, {'ok': True, 'result': zernio_hub.call(w.id, m.group(1), self.json_body())})
            except zernio_hub.Refused as ex:
                store.audit(w.id, 'worker', 'zernio.refused', m.group(1), {'why': str(ex)}, self.ip())
                send_alert(f'Worker {w.label()} was refused a posting call ({m.group(1)}): {ex}')
                return self.send(403, {'error': str(ex)})
            except NotImplementedError as ex:
                return self.send(200, {'ok': False, 'not_implemented': True, 'error': str(ex)})
            except Exception as ex:
                return self.send(502, {'error': str(ex)[:400], 'temporary': type(ex).__name__ == 'Temporary'})
        return self.send(404, {'error': 'not found'})


def main():
    missing = auth.configured()
    if missing:
        print('warning: the hub refuses every request until these are set: ' + ', '.join(missing), flush=True)
    if not storage.ENABLED:
        print('warning: no bucket configured; clips stay on this disk (testing only)', flush=True)
    store.audit('hub', 'system', 'start', '', {'pid': os.getpid()})  # creates the database before any thread uses it
    threading.Thread(target=zernio_hub.background, daemon=True).start()

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        import socket
        address_family = socket.AF_INET6 if os.environ.get('HUB_BIND', '::') == '::' else socket.AF_INET

    host = os.environ.get('HUB_BIND', '::')
    srv = Server((host, PORT), H)
    print(f'hub on [{host}]:{PORT}, data in {store.DATA}', flush=True)
    srv.serve_forever()


if __name__ == '__main__':
    main()
