"""The dashboard's data: every worker's pipeline (as each last reported it) merged with the hub's own Zernio numbers.
Same shape as dashboard/server.py's overview(), so the page works the same; local ids are swapped for the hub's
global ids (two machines can both have an edit 12)."""
import datetime as dt, json, time

import clips
import store
import zernio_hub


def _editors():
    return {r['id']: dict(r) for r in store.db().execute("SELECT * FROM editors WHERE status='approved'")}


def build(who):
    c = store.db()
    eds = _editors()
    have = clips.have()
    workers = {r['id']: dict(r) for r in c.execute('SELECT id, name, active, last_seen, info FROM workers')}
    pending = {(r['gid']): r['action'] for r in c.execute('SELECT gid, action FROM review_actions WHERE done_at IS NULL')}
    sets, review, log, src, refs, posts_snap, beats = [], [], [], None, {'count': 0, 'views': 0}, [], []
    lead = store.meta('lead_worker')
    owned = {}  # remote post id -> editor, from the hub's own Zernio data: a worker can only label its own editors' posts
    for e in eds.values():
        for aid in (json.loads(e['accounts'] or '{}') or {}).values():
            for r in c.execute('SELECT remote_id FROM zposts WHERE account=?', (aid,)):
                owned[r['remote_id']] = e['id']
    for s in c.execute('SELECT * FROM snapshots ORDER BY worker_id'):
        w = workers.get(s['worker_id'])
        if not w or not w['active']:
            continue
        snap = json.loads(s['snap'] or '{}')
        mine = {e for e, r in eds.items() if r['worker_id'] == s['worker_id']}
        g = lambda kind, i: store.gid(s['worker_id'], kind, i)
        for b in snap.get('brand_sets', []):
            if b['id'] not in mine or not who.sees(b['id']):
                continue
            b['worker'] = w['name']
            for x in b.get('scheduled', []):
                x['edit_id'] = g('edit', x['edit_id'])
                x['has_video'] = x['edit_id'] in have
            sets.append(b)
        for r in snap.get('review', []):
            if r['brand_set'] in mine and who.sees(r['brand_set']):
                r['edit_id'] = g('edit', r['edit_id'])
                r['has_video'] = r['edit_id'] in have
                r['pending'] = pending.get(r['edit_id'])
                review.append(r)
        for p in snap.get('posts', []):
            if p.get('brand_set') in mine and (not p.get('remote_id') or owned.get(p['remote_id'], p['brand_set']) == p['brand_set']):
                p['edit_id'] = g('edit', p['edit_id'])
                posts_snap.append(p)
        so = snap.get('sourcing')
        if so and so.get('enabled') and s['worker_id'] == lead and who.sees('__sourcing'):
            for r in so.get('queue', []):
                r['id'] = g('sourced', r['id'])
                r['has_video'] = r['id'] in have
                r['pending'] = pending.get(r['id'])
            src = so
        tag = '' if len(workers) < 2 else f"[{w['name']}] "
        if who.editors is None:  # the activity feed covers every editor on the machine: only for people who see them all
            log += [l[:20] + tag + l[20:] for l in snap.get('log', [])]
        rf = snap.get('refs') or {}
        refs = {'count': max(refs['count'], rf.get('count') or 0), 'views': max(refs['views'], rf.get('views') or 0)}
        beats.append(snap.get('last_run') or {})

    # analytics: the hub's own Zernio numbers, by the accounts Matt locked to each editor
    acc2set = {}
    for e in eds.values():
        for aid in (json.loads(e['accounts'] or '{}') or {}).values():
            if aid:
                acc2set[aid] = e['id']
    hooks = {p.get('remote_id'): p['hook'] for p in posts_snap if p.get('remote_id')}
    removed = {(p.get('remote_id'), x['platform']) for p in posts_snap for x in p['platforms'] if x.get('status') in ('removed', 'remove_by_hand')}
    rows = c.execute('SELECT p.*, s.views, s.likes, s.comments, s.shares, s.saves FROM zposts p LEFT JOIN zstats s '
                     'ON s.remote_id=p.remote_id AND s.platform=p.platform ORDER BY p.published_at DESC').fetchall()
    week_ago = (dt.date.today() - dt.timedelta(days=7)).isoformat()
    fol = {}
    for r in c.execute('SELECT account, day, followers FROM zfollowers ORDER BY day'):
        fol.setdefault(r['account'], []).append((r['day'], r['followers']))
    zh = zernio_hub.HEALTH
    for b in sets:
        if rows:
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
            if a.get('id') and zh['at'] and not zh['error']:
                a['issue'] = zh['accounts'].get(a['id']) if a['id'] in zh['accounts'] else 'not connected in Zernio any more'
        b['proposals'] = c.execute("SELECT COUNT(*) FROM directives WHERE editor=? AND status='proposed'", (b['id'],)).fetchone()[0]
    posts, daily = {}, {}
    for r in rows:
        bs = acc2set.get(r['account'])
        if not bs or not who.sees(bs):
            continue
        gp = posts.setdefault(r['remote_id'], {'remote_id': r['remote_id'], 'brand_set': bs, 'posted_at': r['published_at'],
                                               'hook': hooks.get(r['remote_id']) or (r['content'] or '').split('\n')[0][:90], 'platforms': []})
        rem = (r['remote_id'], r['platform']) in removed
        gp['platforms'].append({'platform': r['platform'], 'status': 'removed' if rem else 'posted', 'url': r['url'],
                                **{k: r[k] for k in ('views', 'likes', 'comments', 'shares', 'saves')}})
        if r['published_at'] and r['published_at'] > time.time() - 30 * 86400 and not rem:
            day = time.strftime('%Y-%m-%d', time.localtime(r['published_at']))
            d = daily.setdefault((day, bs), {'day': day, 'brand_set': bs, 'views': 0, 'posts': set()})
            d['views'] += r['views'] or 0
            d['posts'].add(r['remote_id'])
    for d in daily.values():
        d['posts'] = len(d['posts'])
    if not rows:  # before the hub's first Zernio refresh: the workers' own numbers
        for p in posts_snap:
            if who.sees(p['brand_set']):
                posts[p.get('remote_id') or f"e{p['edit_id']}"] = p
    for p in posts_snap:  # still publishing or failed: only the worker knows
        if p.get('remote_id') not in posts and any(x.get('status') in ('publishing', 'failed') for x in p['platforms']) and who.sees(p['brand_set']):
            posts[p.get('remote_id') or f"e{p['edit_id']}"] = p
    beat = max(beats, key=lambda b: b.get('finished') or b.get('started') or 0) if beats else {}
    now = time.time()
    return {'hub': True, 'generated': now, 'brand_sets': sets, 'review': review, 'sourcing': src, 'refs': refs,
            'posts': sorted(posts.values(), key=lambda p: -(p['posted_at'] or 0))[:50],
            'daily': sorted(daily.values(), key=lambda d: d['day']),
            'log': sorted(log, reverse=True)[:25], 'last_run': beat,
            'platform_sync': store.meta('platform_sync') or {}, 'stats_at': store.meta('refreshed_at'),
            'health_at': zh['at'], 'health_error': zh['error'],
            'workers': [{'id': k, 'name': v['name'], 'last_seen': v['last_seen'], 'active': bool(v['active'])} for k, v in workers.items()]
            if who.can('admin') else []}
