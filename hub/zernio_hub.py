"""The hub's side of Zernio: the only place the posting key lives (ZERNIO_API_KEY, a Railway secret).

- Analytics of its own, every 15 minutes: published posts, post stats, follower counts and account health, so the
  numbers stay current whichever machines are on.
- Posting for the workers (backends/hub.py on each machine): a worker asks for an upload link, uploads its clip
  straight to Zernio's storage, then asks the hub to publish. The hub checks every call against the accounts of the
  editors that worker is approved to run, so a worker can't post to, read or remove anyone else's accounts.
"""
import datetime as dt, json, os, re, sys, time, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from backends import zernio as z  # noqa: E402

import store  # noqa: E402

HEALTH = {'at': 0, 'accounts': {}, 'error': None}


def enabled():
    return bool(os.environ.get('ZERNIO_API_KEY'))


def ts(iso):
    return dt.datetime.fromisoformat(iso.replace('Z', '+00:00')).timestamp() if iso else None


def refresh():
    c = store.db()
    now = time.time()
    page = 1
    while True:
        _, j = z._call('GET', '/posts', query={'status': 'published', 'limit': 100, 'page': page})
        for p in j.get('posts', []):
            for x in p.get('platforms', []):
                if x.get('status') != 'published':
                    continue
                acc = x.get('accountId') or {}
                c.execute('INSERT OR REPLACE INTO zposts VALUES(?,?,?,?,?,?,?)', (
                    p['_id'], x['platform'], acc.get('_id') if isinstance(acc, dict) else acc,
                    acc.get('username') if isinstance(acc, dict) else None, x.get('platformPostUrl'),
                    ts(x.get('publishedAt') or p.get('publishedAt')), p.get('content')))
        pg = j.get('pagination') or {}
        if page >= (pg.get('pages') or 1):
            break
        page += 1
    for r in c.execute('SELECT p.remote_id, MAX(p.published_at) pub, MIN(COALESCE(s.taken_at,0)) seen FROM zposts p LEFT JOIN zstats s '
                       'ON s.remote_id=p.remote_id AND s.platform=p.platform WHERE p.published_at>? GROUP BY p.remote_id',
                       (now - 30 * 86400,)).fetchall():
        every = 3600 if now - (r['pub'] or 0) < 7 * 86400 else 6 * 3600
        if now - r['seen'] < every:
            continue
        try:
            got = z.post_stats(r['remote_id'])
        except Exception as ex:
            print(f"stats {r['remote_id']}: {ex}", flush=True)
            continue
        for pf, m in got.items():
            c.execute('INSERT OR REPLACE INTO zstats VALUES(?,?,?,?,?,?,?,?)', (r['remote_id'], pf, now, m.get('views'), m.get('likes'),
                      m.get('comments'), m.get('shares'), m.get('saves')))
            if m.get('url'):
                c.execute('UPDATE zposts SET url=? WHERE remote_id=? AND platform=? AND url IS NULL', (m['url'], r['remote_id'], pf))
    if now - (store.meta('followers_at', 0) or 0) > 6 * 3600:
        day = dt.date.today().isoformat()
        for a in z.account_info():
            c.execute('INSERT OR REPLACE INTO zfollowers VALUES(?,?,?,?,?,?)', (a['id'], a['platform'], a['username'], day, a['followers'], now))
        store.set_meta('followers_at', now)
    try:
        store.set_meta('platform_sync', z.nudge_sync())
    except Exception as ex:
        print(f'nudge_sync: {ex}', flush=True)
    store.set_meta('refreshed_at', now)


def health():
    try:
        HEALTH['accounts'].update(z.account_health())
        HEALTH['error'] = None
    except Exception as ex:
        HEALTH['error'] = str(ex)
    HEALTH['at'] = time.time()


def backup():
    """Nightly copy of the database into the private bucket (kept 30 days)."""
    import storage
    if not storage.ENABLED:
        return
    day = dt.date.today().isoformat()
    if store.meta('backup_day') == day:
        return
    storage.put_bytes(f'backups/hub-{day}.db', store.backup_bytes())
    old = (dt.date.today() - dt.timedelta(days=30)).isoformat()
    storage.delete(f'backups/hub-{old}.db')
    store.set_meta('backup_day', day)
    store.audit('hub', 'system', 'backup', f'backups/hub-{day}.db')
    v = store.verify_audit()  # the chain's head goes out daily, so a cut-off or rewritten log can be spotted later
    storage.put_bytes(f'backups/audit-head-{day}.json', json.dumps(v).encode(), 'application/json')


def background():
    n = 0
    while True:
        try:
            if enabled():
                if n % 3 == 0:
                    refresh()
                health()
            backup()
            # review clips nobody needs any more (decided, posted or gone from the worker) leave the bucket
            import clips
            clips.prune()
        except Exception as ex:
            print(f'background: {ex}\n{traceback.format_exc()}', flush=True)
        n += 1
        time.sleep(300)


# ---------- posting proxy (called by backends/hub.py on the workers) ----------
class Refused(Exception):
    pass


def worker_accounts(worker_id):
    """{account_id: editor_id} for every approved editor this worker runs (accounts locked by Matt at approval)."""
    out = {}
    for r in store.db().execute("SELECT id, accounts FROM editors WHERE worker_id=? AND status='approved'", (worker_id,)):
        for aid in (json.loads(r['accounts'] or '{}') or {}).values():
            if aid:
                out[aid] = r['id']
    return out


def _owns_post(worker_id, remote_id):
    c = store.db()
    if c.execute('SELECT 1 FROM published WHERE remote_id=? AND worker_id=?', (remote_id, worker_id)).fetchone():
        return True
    mine = worker_accounts(worker_id)
    rows = c.execute('SELECT account FROM zposts WHERE remote_id=?', (remote_id,)).fetchall()
    if not rows:  # not seen yet (still publishing, or older than the last refresh): ask Zernio which accounts it's on
        try:
            _, j = z._call('GET', f'/posts/{remote_id}')
            p = j.get('post', j)
            accts = [(x.get('accountId') or {}).get('_id') if isinstance(x.get('accountId'), dict) else x.get('accountId')
                     for x in p.get('platforms', [])]
        except Exception:
            return False
    else:
        accts = [r['account'] for r in rows]
    return bool(accts) and all(a in mine for a in accts)


def call(worker_id, op, a):
    """One posting operation for a worker. Raises Refused for anything outside its own editors' accounts."""
    mine = worker_accounts(worker_id)
    c = store.db()
    if op == 'presign':
        size = int(a.get('size') or 0)
        if not 0 < size <= 1 << 30:
            raise Refused('bad size')
        _, p = z._call('POST', '/media/presign', {'filename': os.path.basename(str(a.get('filename') or 'clip.mp4'))[:120],
                                                    'contentType': 'video/mp4', 'size': size})
        c.execute('INSERT OR REPLACE INTO uploads VALUES(?,?,?)', (p['publicUrl'], worker_id, time.time()))
        return {'uploadUrl': p['uploadUrl'], 'publicUrl': p['publicUrl']}
    if op == 'publish':
        targets = a.get('targets') or []
        if not targets or any(t.get('account') not in mine for t in targets):
            raise Refused('a target account is not one of this worker\'s editors')
        if len({mine[t['account']] for t in targets}) != 1:
            raise Refused('one post goes to one editor\'s accounts')
        up = c.execute('SELECT worker_id FROM uploads WHERE public_url=?', (a.get('media_url'),)).fetchone()
        if not up or up['worker_id'] != worker_id:
            raise Refused('upload the clip through the hub first')
        res = z.publish_url(a['media_url'], str(a.get('caption') or '')[:2200], targets, {'title': str((a.get('meta') or {}).get('title') or '')[:150],
                                                                                         'idempotency': f"{worker_id}-{(a.get('meta') or {}).get('idempotency')}"})
        for r in res.values():
            if r.get('id'):
                c.execute('INSERT OR IGNORE INTO published VALUES(?,?,?,?)', (r['id'], worker_id, mine[targets[0]['account']], time.time()))
        store.audit(worker_id, 'worker', 'publish', mine[targets[0]['account']],
                    {'targets': [t['platform'] for t in targets], 'result': {k: v.get('status') for k, v in res.items()}})
        return res
    if op in ('status', 'unpublish', 'post_stats', 'metrics'):
        rid = str(a.get('remote_id') or '')
        if not re.match(r'^[A-Za-z0-9_-]{1,64}$', rid) or not _owns_post(worker_id, rid):
            raise Refused('not one of this worker\'s posts')
        if op == 'status':
            return z.status(rid)
        if op == 'post_stats':
            return z.post_stats(rid)
        if op == 'metrics':
            return z.metrics(rid, a.get('platform'), a.get('account'))
        if a.get('platform') not in ('tiktok', 'instagram', 'youtube', 'facebook', 'x', 'twitter'):
            raise Refused('bad platform')
        z.unpublish(rid, a.get('platform'))
        store.audit(worker_id, 'worker', 'unpublish', rid, {'platform': a.get('platform')})
        return {'ok': True}
    if op == 'account_info':
        return [x for x in z.account_info() if x['id'] in mine]
    if op == 'list_accounts':
        return [x for x in z.list_accounts() if x.get('_id') in mine]
    if op == 'account_health':
        if not HEALTH['at']:
            health()
        return {k: v for k, v in HEALTH['accounts'].items() if k in mine}
    if op == 'nudge_sync':
        return store.meta('platform_sync') or z.nudge_sync()
    raise Refused(f'unknown operation {op}')
