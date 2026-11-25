#!/usr/bin/env python3
"""Operations dashboard for the AI editor network: every brand set (one per editor), its accounts, analytics,
pipeline and review queue on one page. Local only (127.0.0.1). Numbers come from flywheel.db, which `flywheel.py run`
keeps fresh (stats step). Start: python3 dashboard/server.py  ->  http://127.0.0.1:8790
"""
import datetime as dt, json, os, re, sqlite3, subprocess, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import flywheel as fw  # noqa: E402  (reuses its config, database and editor loading)
import sourcing as src  # noqa: E402  (the sourcing agent: full episodes -> raw moments)

PORT = int(os.environ.get('DASHBOARD_PORT', 8790))
PLATFORMS = ['tiktok', 'instagram', 'youtube', 'facebook', 'x']
STATUSES = ['posted', 'queued', 'approved', 'ready', 'decided', 'qa_failed', 'excluded', 'skipped', 'rejected', 'taken_down']


HEALTH = {'at': 0, 'accounts': {}, 'error': None}


def health_forever(every=300):
    """Account health from the posting service every 5 minutes (kept in memory for the page)."""
    import threading
    def loop():
        while True:
            for be in {e['backend'] for e in fw.editors()}:
                fn = getattr(fw.backend(be), 'account_health', None)
                if fn:
                    try:
                        HEALTH['accounts'].update(fn()); HEALTH['error'] = None
                    except Exception as ex:
                        HEALTH['error'] = str(ex)
            HEALTH['at'] = time.time()
            time.sleep(every)
    threading.Thread(target=loop, daemon=True).start()


def account_issue(c, aid):
    """Why this account isn't posting properly, or None."""
    if aid in HEALTH['accounts'] and HEALTH['accounts'][aid]:
        return HEALTH['accounts'][aid]
    if HEALTH['at'] and not HEALTH['error'] and aid not in HEALTH['accounts']:
        return 'not connected in Zernio any more'
    last = c.execute("SELECT status, error FROM posts WHERE account=? AND status IN ('posted','failed') AND posted_at>? "
                     "ORDER BY posted_at DESC LIMIT 1", (aid, time.time() - 2 * 86400)).fetchone()
    if last and last['status'] == 'failed':
        return f"last post failed: {(last['error'] or 'no reason given')[:160]}"
    return None


def editor_issue(eid, lines):
    """An error in this editor's own pipeline (render, decide, scheduling) in the last 2 hours."""
    cutoff = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time() - 7200))
    errs = [l for l in lines if l[:19] >= cutoff and f'{eid} ERROR' in l]
    return errs[-1][20:200] if errs else None


def lessons(eid):
    """The performance playbook as this editor reads it: shared content lessons plus its own style notes."""
    pb = fw.read_playbook()
    return {'network': pb['network'].strip(), 'style': pb['style'].get(eid, '').strip()}


def scheduled(c, eid):
    """Clips lined up to post: queued with a time, then approved ones waiting for the next free slot."""
    out = []
    for r in c.execute("SELECT e.id, e.hook, e.path, MIN(p.slot) slot, GROUP_CONCAT(p.platform) pfs FROM posts p JOIN edits e ON e.id=p.edit_id "
                       "WHERE p.editor=? AND p.status='queued' GROUP BY e.id ORDER BY slot", (eid,)):
        out.append({'edit_id': r['id'], 'hook': r['hook'], 'slot': r['slot'], 'platforms': (r['pfs'] or '').split(','),
                    'has_video': bool(r['path'] and os.path.exists(r['path']))})
    for r in c.execute("SELECT id, hook, path FROM edits WHERE editor=? AND status='approved' ORDER BY updated", (eid,)):
        out.append({'edit_id': r['id'], 'hook': r['hook'], 'slot': None, 'platforms': [], 'has_video': bool(r['path'] and os.path.exists(r['path']))})
    return out


def learning(c, eid, days=14):
    """Is the editor getting more precise? Share of finished clips that needed a human, and how many of those were approved."""
    since = time.time() - days * 86400
    made = c.execute("SELECT COUNT(*) FROM edits WHERE editor=? AND created>? AND status NOT IN ('excluded','skipped','superseded','decided')",
                     (eid, since)).fetchone()[0]
    held = c.execute("SELECT COUNT(*) FROM edits WHERE editor=? AND created>? AND note<>'' AND status NOT IN ('excluded','skipped','superseded','decided')",
                     (eid, since)).fetchone()[0]
    ok, no = [c.execute('SELECT COUNT(*) FROM feedback WHERE editor=? AND action=? AND at>?', (eid, a, since)).fetchone()[0] for a in ('approve', 'reject')]
    rules = c.execute('SELECT COUNT(*) FROM rules WHERE active=1 AND (editor IS NULL OR editor=?)', (eid,)).fetchone()[0]
    return {'days': days, 'made': made, 'held': held, 'approved': ok, 'rejected': no, 'rules': rules}


def x_chain(c, days=14):
    """The X branch: the chief of staff (Grok Bot) posts each new source clip to Real AF's X account in full with its own
    caption, then adds the clip's file name to posted_to_x.txt in the Drive archive (Matt, 2026-11-24: "that is technically
    part of the process"). Stalled = new source clips waiting and nothing recorded for 2 days."""
    acct = fw.CFG.get('x_account') or {}
    try:
        posted = fw.x_posted()
        p = os.path.join(os.path.expanduser(fw.CFG.get('archive_root', '')), 'posted_to_x.txt')
        updated = os.path.getmtime(p)
    except OSError as ex:
        return {'handle': acct.get('handle'), 'status': 'stalled', 'issue': f'posted_to_x.txt unreadable: {ex}'[:200]}
    now = time.time()
    recent = c.execute("SELECT path, mtime FROM raw_clips WHERE origin='new' AND mtime>?", (now - days * 86400,)).fetchall()
    waiting = [r for r in recent if os.path.basename(r['path']) not in posted]
    oldest = max(((now - r['mtime']) / 86400 for r in waiting), default=0)
    out = {'handle': acct.get('handle'), 'by': 'Grok Bot', 'posted': len(posted), 'posted_recent': len(recent) - len(waiting),
           'waiting': len(waiting), 'oldest_wait_days': round(oldest, 1), 'last_update': updated if posted else None, 'issue': None}
    if not posted:
        out['status'] = 'not_reporting'
    elif waiting and oldest > 2 and now - updated > 2 * 86400:
        out['status'] = 'stalled'
        out['issue'] = f"{len(waiting)} source clips waiting for X; nothing recorded for {(now - updated) / 86400:.0f} days"
    else:
        out['status'] = 'ok'
    return out


def sourcing(c):
    """The sourcing agent: episodes it has gone through, moments it pulled, and the ones waiting for a human."""
    src.db()  # creates its tables on first use
    eps = [dict(r) for r in c.execute("SELECT id, title, episode, show, status, picks, note, started, finished FROM episodes ORDER BY started DESC LIMIT 8")]
    n = {r['status']: r['n'] for r in c.execute('SELECT status, COUNT(*) n FROM sourced GROUP BY status')}
    held = [{'id': r['id'], 'name': r['name'], 'duration': r['duration'], 'first_line': r['first_line'], 'why': r['why'],
             'flags': json.loads(r['risk_flags'] or '[]'), 'confidence': r['confidence'], 'episode': r['title'],
             'has_video': bool(r['path'] and os.path.exists(r['path']))}
            for r in c.execute("SELECT s.*, e.title FROM sourced s JOIN episodes e ON e.id=s.episode_id WHERE s.status='held' "
                               "ORDER BY s.episode_id DESC, s.confidence DESC, s.id")]
    used = c.execute("SELECT COUNT(DISTINCT e.raw_id) FROM edits e JOIN raw_clips r ON r.id=e.raw_id JOIN sourced s ON s.path=r.path").fetchone()[0]
    return {'x': x_chain(c), 'auto_approve': src.S['auto_approve'], 'handoff': src.S['handoff_to_editors'], 'enabled': src.S['enabled'] and bool(fw.CFG.get('episodes_folder_id')),
            'episodes_done': c.execute("SELECT COUNT(*) FROM episodes WHERE status='done'").fetchone()[0],
            'working': next((e['title'] for e in eps if e['status'] == 'processing' and time.time() - (e['started'] or 0) < 3 * 3600), None),
            'failed': next((f"{e['title']}: {e['note']}" for e in eps if e['status'] == 'failed'), None),
            'supplied': n.get('approved', 0), 'held': n.get('held', 0), 'rejected': n.get('rejected', 0), 'edited': used,
            'episodes': eps, 'queue': held}


def overview():
    c = fw.db()
    now = time.time()
    beat_path = os.path.join(fw.WORK, 'last_run.json')
    beat = json.load(open(beat_path)) if os.path.exists(beat_path) else {}
    log_path = os.path.join(fw.WORK, 'logs', 'run.log')
    if not beat and os.path.exists(log_path):  # before the first run that writes the heartbeat
        beat = {'finished': os.path.getmtime(log_path)}
    followers = {}  # account -> (today, 7 days ago)
    for r in c.execute('SELECT account, day, followers FROM account_stats ORDER BY day'):
        followers.setdefault(r['account'], []).append((r['day'], r['followers']))
    week_ago = (dt.date.today() - dt.timedelta(days=7)).isoformat()

    log_path = os.path.join(fw.WORK, 'logs', 'run.log')
    all_lines = open(log_path).read().splitlines() if os.path.exists(log_path) else []
    # which editor is mid-job: a pass is running (started, not finished, under 45 min old) and its latest log line names it
    busy = None
    if beat.get('started') and not beat.get('finished') and now - beat['started'] < 2700:
        ids = [e['id'] for e in fw.editors()]
        busy = next((i for l in reversed(all_lines[-60:]) for i in ids if f' {i} ' in f' {l} '), None)
    sets = []
    for ed in fw.editors():
        bs = ed.get('brand_set', {})
        style = json.load(open(os.path.join(ed['dir'], 'style.json')))
        accts = []
        for pf in PLATFORMS:
            aid = ed.get('accounts', {}).get(pf)
            hist = followers.get(aid, [])
            now_f = hist[-1][1] if hist else None
            then_f = next((f for d, f in reversed(hist) if d <= week_ago), hist[0][1] if hist else None)
            if aid or pf in bs.get('planned_platforms', []) or pf in ed.get('platforms', []):
                accts.append({'platform': pf, 'id': aid, 'connected': bool(aid), 'issue': account_issue(c, aid) if aid else None, 'handle': ed.get('handles', {}).get(pf),
                              'followers': now_f, 'growth_7d': (now_f - then_f) if now_f is not None and then_f is not None else None})
        counts = {s: 0 for s in STATUSES}
        for r in c.execute('SELECT status, COUNT(*) n FROM edits WHERE editor=? GROUP BY status', (ed['id'],)):
            counts[r['status']] = r['n']
        t = c.execute("SELECT COUNT(DISTINCT p.edit_id) posts, SUM(s.views) views, SUM(s.likes) likes, SUM(s.comments) comments, "
                      "SUM(s.shares) shares, SUM(s.saves) saves FROM posts p LEFT JOIN post_stats s ON s.post_id=p.id "
                      "WHERE p.editor=? AND p.status='posted'", (ed['id'],)).fetchone()
        nxt = c.execute("SELECT p.slot, e.hook FROM posts p JOIN edits e ON e.id=p.edit_id WHERE p.editor=? AND p.status='queued' "
                        "ORDER BY p.slot LIMIT 1", (ed['id'],)).fetchone()
        sets.append({
            'id': ed['id'], 'editor': ed['name'], 'name': bs.get('name', ed['name']), 'theme': bs.get('theme', ''),
            'format': bs.get('format', ''), 'enabled': ed.get('enabled', True), 'auto_post': ed.get('auto_post', False),
            'slots': ed.get('slots', []),
            'look': {'captions': f"{style.get('caption_font')} {style.get('caption_size')}px",
                     'hook': f"{style.get('hook_font')} {style.get('hook_size')}px, {style.get('hook_color')} on {style.get('hook_bg')}"},
            'accounts': accts, 'counts': counts, 'totals': {k: t[k] or 0 for k in t.keys()},
            'next_post': {'slot': nxt['slot'], 'hook': nxt['hook']} if nxt else None,
            'editor_issue': editor_issue(ed['id'], all_lines[-400:]),
            'learning': learning(c, ed['id']),
            'working': busy == ed['id'],
            'scheduled': scheduled(c, ed['id']),
            'lessons': lessons(ed['id'])})

    posts = {}
    for r in c.execute("SELECT p.*, e.hook, s.views, s.likes, s.comments, s.shares, s.saves, s.taken_at FROM posts p "
                       "JOIN edits e ON e.id=p.edit_id LEFT JOIN post_stats s ON s.post_id=p.id "
                       "WHERE p.status IN ('posted','publishing','failed','removed','remove_by_hand') "
                       "ORDER BY p.posted_at DESC LIMIT 200"):
        g = posts.setdefault(r['edit_id'], {'edit_id': r['edit_id'], 'remote_id': r['remote_id'], 'brand_set': r['editor'], 'hook': r['hook'],
                                            'posted_at': r['posted_at'], 'platforms': []})
        g['platforms'].append({k: r[k] for k in ('platform', 'status', 'url', 'views', 'likes', 'comments', 'shares', 'saves', 'error')})

    review = [{'edit_id': r['id'], 'brand_set': r['editor'], 'hook': r['hook'], 'note': r['note'], 'status': r['status'],
               'created': r['created'], 'file': os.path.basename(r['path'] or ''), 'has_video': bool(r['path'] and os.path.exists(r['path']))}
              for r in c.execute("SELECT * FROM edits WHERE status IN ('ready','qa_failed') ORDER BY created")]

    daily = [dict(r) for r in c.execute(
        "SELECT date(p.posted_at,'unixepoch','localtime') day, p.editor brand_set, SUM(COALESCE(s.views,0)) views, "
        "COUNT(DISTINCT p.edit_id) posts FROM posts p LEFT JOIN post_stats s ON s.post_id=p.id "
        "WHERE p.status='posted' AND p.posted_at>? GROUP BY day, p.editor ORDER BY day", (now - 30 * 86400,))]

    lines = [l for l in all_lines if re.match(r'^\d{4}-\d\d-\d\d ', l)][-25:]
    stats_at = c.execute('SELECT MAX(taken_at) FROM post_stats').fetchone()[0]
    lift = {r['edit_id']: r['lift'] for r in fw.perf_rows(c)}
    for p in posts.values():
        p['lift'] = lift.get(p['edit_id'])
    nref = c.execute('SELECT COUNT(*), SUM(views) FROM refs WHERE analyzed_at IS NOT NULL').fetchone()
    ps_path = os.path.join(fw.WORK, 'platform_sync.json')
    platform_sync = json.load(open(ps_path)) if os.path.exists(ps_path) else {}
    return {'sourcing': sourcing(c), 'platform_sync': platform_sync, 'refs': {'count': nref[0], 'views': nref[1] or 0}, 'generated': now, 'last_run': beat, 'health_at': HEALTH['at'], 'health_error': HEALTH['error'], 'stats_at': stats_at, 'brand_sets': sets,
            'posts': list(posts.values())[:50], 'review': review, 'daily': daily, 'log': lines[::-1]}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype='application/json'):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path in ('/', '/index.html'):
            return self.send(200, open(os.path.join(HERE, 'index.html'), 'rb').read(), 'text/html; charset=utf-8')
        if self.path == '/api/overview':
            try:
                return self.send(200, overview())
            except Exception as ex:
                return self.send(500, {'error': str(ex)})
        m = re.match(r'^/(video|sourced)/(\d+)$', self.path)
        if m:
            return self.video(int(m.group(2)), m.group(1))
        self.send(404, {'error': 'not found'})

    def video(self, edit_id, kind='video'):
        r = (src.db().execute('SELECT path FROM sourced WHERE id=?', (edit_id,)) if kind == 'sourced'
             else fw.db().execute('SELECT path FROM edits WHERE id=?', (edit_id,))).fetchone()
        if not r or not r['path'] or not os.path.exists(r['path']):
            return self.send(404, {'error': 'no video'})
        size = os.path.getsize(r['path'])
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
        with open(r['path'], 'rb') as f:
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
        m = re.match(r'^/api/(edit|sourced)/(\d+)/(approve|reject)$', self.path)
        if not m:
            return self.send(404, {'error': 'not found'})
        kind, eid, action = m.group(1), int(m.group(2)), m.group(3)
        n = int(self.headers.get('Content-Length') or 0)
        reason = (json.loads(self.rfile.read(n) or b'{}').get('reason') or '').strip()[:1000] if n else ''
        if action == 'reject' and not reason:
            return self.send(400, {'error': 'Say why it was rejected'})
        if kind == 'sourced':  # a raw moment from the sourcing agent: approve releases it to the editors
            row = src.db().execute('SELECT status FROM sourced WHERE id=?', (eid,)).fetchone()
            if not row or row['status'] != 'held':
                return self.send(409, {'error': f'clip {eid} is not waiting for approval'})
            out = subprocess.run([sys.executable, os.path.join(ROOT, 'sourcing.py'), action, str(eid), reason], capture_output=True, text=True)
            if out.returncode:
                return self.send(500, {'error': out.stderr[-400:]})
            return self.send(200, {'ok': True, 'id': eid, 'status': 'approved' if action == 'approve' else 'rejected'})
        row = fw.db().execute('SELECT status FROM edits WHERE id=?', (eid,)).fetchone()
        if not row or row['status'] not in ('ready', 'qa_failed'):
            return self.send(409, {'error': f'edit {eid} is not waiting for review'})
        out = subprocess.run([sys.executable, os.path.join(ROOT, 'flywheel.py'), action, str(eid), reason], capture_output=True, text=True)
        if out.returncode:
            return self.send(500, {'error': out.stderr[-400:]})
        self.send(200, {'ok': True, 'edit_id': eid, 'status': 'approved' if action == 'approve' else 'rejected'})


# ---------- sync to the hosted copy (dashboard/cloud.py on Railway) ----------
SECRETS = os.path.expanduser('~/.config/ai-editors/secrets.json')


def _cloud():
    try:
        s = json.load(open(SECRETS))
        return s['dashboard_url'].rstrip('/'), s['dashboard_ingest_token']
    except (OSError, KeyError, ValueError):
        return None, None


def _req(method, url, token, data=None, ctype='application/json', timeout=60):
    import urllib.request
    req = urllib.request.Request(url, method=method, data=data, headers={'Authorization': f'Bearer {token}', 'Content-Type': ctype})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b'{}')


def sync_once():
    """Push the snapshot and any missing review videos, then apply Approve/Reject clicked on the hosted page."""
    url, token = _cloud()
    if not url:
        return
    acts = _req('GET', f'{url}/api/actions', token)
    done = []
    for a in acts:
        row = fw.db().execute('SELECT status FROM edits WHERE id=?', (a['edit_id'],)).fetchone()
        if row and row['status'] in ('ready', 'qa_failed'):
            subprocess.run([sys.executable, os.path.join(ROOT, 'flywheel.py'), a['action'], str(a['edit_id']), a.get('reason', '')], capture_output=True)
            fw.log(f"dashboard: edit {a['edit_id']} {a['action']}d on the hosted dashboard")
        done.append(a['edit_id'])
    if done:
        _req('POST', f'{url}/api/actions/ack', token, json.dumps({'edit_ids': done}).encode())
    snap = overview()
    have = set(_req('POST', f'{url}/api/ingest', token, json.dumps(snap).encode())['have_videos'])
    for r in snap['review']:
        if r['edit_id'] in have or not r['has_video']:
            continue
        path = fw.db().execute('SELECT path FROM edits WHERE id=?', (r['edit_id'],)).fetchone()['path']
        _req('PUT', f"{url}/api/video/{r['edit_id']}", token, open(path, 'rb').read(), 'video/mp4', timeout=600)


def sync_forever(every=60):
    import threading
    def loop():
        while True:
            try:
                sync_once()
            except Exception as ex:
                print(f'{time.strftime("%H:%M:%S")} sync failed: {ex}', flush=True)
            time.sleep(every)
    threading.Thread(target=loop, daemon=True).start()


if __name__ == '__main__':
    url, _ = _cloud()
    if '--sync-only' in sys.argv:  # what the editing Mac runs: no local page, just keep the hosted dashboard fed
        if not url:
            sys.exit(f'no dashboard_url / dashboard_ingest_token in {SECRETS}')
        print(f'syncing the editing pipeline to {url} every minute')
        health_forever()
        while True:
            try:
                sync_once()
            except Exception as ex:
                print(f'{time.strftime("%H:%M:%S")} sync failed: {ex}', flush=True)
            time.sleep(60)
    print(f'AI editor dashboard on http://127.0.0.1:{PORT}')
    health_forever()
    if url:
        print(f'syncing to {url} every minute')
        sync_forever()
    ThreadingHTTPServer(('127.0.0.1', PORT), H).serve_forever()
