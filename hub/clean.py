"""Worker reports are untrusted input: a compromised machine must not be able to put script into Matt's dashboard,
crash the page, or relabel other editors' posts. Every snapshot is rebuilt here, field by field, with fixed types,
length caps and safe values (ids, platforms, https-only links), before it is stored or shown."""
import math, re, time

PLATFORMS = {'tiktok', 'instagram', 'youtube', 'facebook', 'x'}
EDITOR = re.compile(r'^[a-z0-9-]{1,40}$')
REMOTE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
HANDLE = re.compile(r'^@?[A-Za-z0-9._-]{1,40}$')
HM = re.compile(r'^\d\d:\d\d$')
STATUSES = ['posted', 'queued', 'approved', 'ready', 'decided', 'qa_failed', 'excluded', 'skipped', 'rejected', 'taken_down']
POST_STATUS = {'posted', 'publishing', 'failed', 'removed', 'remove_by_hand', 'queued', 'cancelled'}


def s(v, n=300):
    return v[:n] if isinstance(v, str) else ('' if v is None else str(v)[:n])


def so(v, n=300):
    return None if v is None else s(v, n)


def i(v, lo=-1 << 53, hi=1 << 53):
    try:
        x = int(v)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(lo, min(hi, x))


def io(v):
    return None if v is None else i(v)


def f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def t(v):
    """A timestamp, never in the future (so a worker can't push other machines' cursors ahead)."""
    x = f(v)
    return None if x is None else max(0.0, min(x, time.time() + 300))


def b(v):
    return v is True


def eid(v):
    return v if isinstance(v, str) and EDITOR.match(v) else None


def url(v):
    return v[:500] if isinstance(v, str) and v.startswith('https://') and not any(c in v for c in '"\'<> \\') else None


def lst(v, n):
    return v[:n] if isinstance(v, list) else []


def dct(v):
    return v if isinstance(v, dict) else {}


def brand_set(x):
    x = dct(x)
    accts = []
    for a in lst(x.get('accounts'), 10):
        a = dct(a)
        if a.get('platform') not in PLATFORMS:
            continue
        h = a.get('handle')
        accts.append({'platform': a['platform'], 'id': a.get('id') if isinstance(a.get('id'), str) and REMOTE.match(a['id']) else None,
                      'connected': b(a.get('connected')), 'issue': so(a.get('issue'), 200), 'handle': h if isinstance(h, str) and HANDLE.match(h) else None,
                      'followers': io(a.get('followers')), 'growth_7d': io(a.get('growth_7d'))})
    np = dct(x.get('next_post'))
    L = dct(x.get('learning'))
    look = dct(x.get('look'))
    return {
        'id': eid(x.get('id')), 'editor': s(x.get('editor'), 60), 'name': s(x.get('name'), 80), 'theme': s(x.get('theme'), 120),
        'format': s(x.get('format'), 60), 'enabled': b(x.get('enabled')), 'auto_post': b(x.get('auto_post')),
        'slots': [v for v in lst(x.get('slots'), 12) if isinstance(v, str) and HM.match(v)],
        'look': {'captions': s(look.get('captions'), 80), 'hook': s(look.get('hook'), 120)},
        'accounts': accts,
        'counts': {k: i(dct(x.get('counts')).get(k), 0, 10 ** 7) for k in STATUSES},
        'totals': {k: i(dct(x.get('totals')).get(k), 0) for k in ('posts', 'views', 'likes', 'comments', 'shares', 'saves')},
        'next_post': {'slot': t(np.get('slot')) or f(np.get('slot')), 'hook': s(np.get('hook'), 140)} if np.get('slot') else None,
        'editor_issue': so(x.get('editor_issue'), 200),
        'learning': {k: i(L.get(k), 0, 10 ** 7) for k in ('days', 'made', 'held', 'approved', 'rejected', 'rules')},
        'working': b(x.get('working')),
        'scheduled': [{'edit_id': i(c.get('edit_id'), 0), 'hook': s(c.get('hook'), 140), 'slot': f(c.get('slot')),
                       'platforms': [p for p in lst(c.get('platforms'), 6) if p in PLATFORMS], 'has_video': b(c.get('has_video'))}
                      for c in map(dct, lst(x.get('scheduled'), 60))],
        'lessons': {'network': s(dct(x.get('lessons')).get('network'), 20000), 'style': s(dct(x.get('lessons')).get('style'), 5000)},
    }


def snapshot(snap):
    snap = dct(snap)
    sets = [z for z in (brand_set(x) for x in lst(snap.get('brand_sets'), 50)) if z['id']]
    review = [{'edit_id': i(r.get('edit_id'), 0), 'brand_set': eid(r.get('brand_set')), 'hook': s(r.get('hook'), 140), 'note': s(r.get('note'), 1500),
               'status': s(r.get('status'), 20), 'created': f(r.get('created')), 'file': s(r.get('file'), 200), 'has_video': b(r.get('has_video'))}
              for r in map(dct, lst(snap.get('review'), 300))]
    posts = []
    for p in map(dct, lst(snap.get('posts'), 100)):
        rid = p.get('remote_id')
        posts.append({'edit_id': i(p.get('edit_id'), 0), 'remote_id': rid if isinstance(rid, str) and REMOTE.match(rid) else None,
                      'brand_set': eid(p.get('brand_set')), 'hook': s(p.get('hook'), 140), 'posted_at': f(p.get('posted_at')), 'lift': f(p.get('lift')),
                      'platforms': [{'platform': x['platform'], 'status': x.get('status') if x.get('status') in POST_STATUS else 'failed',
                                     'url': url(x.get('url')), 'error': so(x.get('error'), 300),
                                     **{k: io(x.get(k)) for k in ('views', 'likes', 'comments', 'shares', 'saves')}}
                                    for x in map(dct, lst(p.get('platforms'), 6)) if x.get('platform') in PLATFORMS]})
    src = None
    so_ = snap.get('sourcing')
    if isinstance(so_, dict):
        x = dct(so_.get('x'))
        h = x.get('handle')
        src = {'x': {'handle': h if isinstance(h, str) and HANDLE.match(h) else None, 'by': s(x.get('by'), 40),
                     'status': x.get('status') if x.get('status') in ('ok', 'stalled', 'not_reporting') else 'not_reporting',
                     'issue': so(x.get('issue'), 200), 'last_update': t(x.get('last_update')),
                     **{k: i(x.get(k), 0) for k in ('posted', 'posted_recent', 'waiting')}, 'oldest_wait_days': f(x.get('oldest_wait_days')) or 0},
               'auto_approve': b(so_.get('auto_approve')), 'enabled': b(so_.get('enabled')), 'episodes_done': i(so_.get('episodes_done'), 0),
               'working': so(so_.get('working'), 200), 'failed': so(so_.get('failed'), 300),
               **{k: i(so_.get(k), 0) for k in ('supplied', 'held', 'rejected', 'edited')},
               'episodes': [{'id': i(e.get('id'), 0), 'title': s(e.get('title'), 200), 'episode': so(e.get('episode'), 20), 'show': s(e.get('show'), 40),
                             'status': s(e.get('status'), 20), 'picks': io(e.get('picks')), 'note': s(e.get('note'), 300),
                             'started': f(e.get('started')), 'finished': f(e.get('finished'))} for e in map(dct, lst(so_.get('episodes'), 10))],
               'queue': [{'id': i(r.get('id'), 0), 'name': s(r.get('name'), 200), 'duration': f(r.get('duration')) or 0, 'first_line': s(r.get('first_line'), 300),
                          'why': s(r.get('why'), 600), 'flags': [s(z, 120) for z in lst(r.get('flags'), 10)], 'confidence': f(r.get('confidence')) or 0,
                          'episode': s(r.get('episode'), 200), 'has_video': b(r.get('has_video'))} for r in map(dct, lst(so_.get('queue'), 200))]}
    lr = dct(snap.get('last_run'))
    rf = dct(snap.get('refs'))
    return {'brand_sets': sets, 'review': [r for r in review if r['brand_set']], 'posts': posts, 'sourcing': src,
            'last_run': {'started': f(lr.get('started')), 'finished': f(lr.get('finished'))},
            'refs': {'count': i(rf.get('count'), 0), 'views': i(rf.get('views'), 0)},
            'log': [s(l, 300) for l in lst(snap.get('log'), 25)]}
