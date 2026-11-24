"""Review and scheduled clips in private storage, keyed by the hub's global item id."""
import os, time

import storage
import store

LOCAL = os.path.join(store.DATA, 'clips')  # local testing only (no bucket)


def key(gid):
    return f'clips/{gid}.mp4'


def have():
    return {r['gid'] for r in store.db().execute('SELECT gid FROM clips')}


def upload_url(gid):
    if storage.ENABLED:
        return storage.presign('PUT', key(gid), 900, content_type='video/mp4')
    return f'/api/worker/clip-local/{gid}'


def done(gid, size):
    store.db().execute('INSERT OR REPLACE INTO clips VALUES(?,?,?,?)', (gid, key(gid), int(size), time.time()))


def watch_url(gid):
    """A playback link that works for 10 minutes."""
    if storage.ENABLED:
        return storage.presign('GET', key(gid), 600)
    return None


def needed():
    """Clips some snapshot still lists for review or the schedule."""
    import json
    keep = set()
    c = store.db()
    for s in c.execute('SELECT worker_id, snap FROM snapshots'):
        snap = json.loads(s['snap'] or '{}')
        for r in snap.get('review', []):
            keep.add((s['worker_id'], 'edit', r['edit_id']))
        for b in snap.get('brand_sets', []):
            for x in b.get('scheduled', []):
                keep.add((s['worker_id'], 'edit', x['edit_id']))
        for r in (snap.get('sourcing') or {}).get('queue', []):
            keep.add((s['worker_id'], 'sourced', r['id']))
    out = set()
    for w, k, i in keep:
        r = c.execute('SELECT gid FROM items WHERE worker_id=? AND kind=? AND local_id=?', (w, k, i)).fetchone()
        if r:
            out.add(r['gid'])
    return out


def prune():
    keep = needed()
    c = store.db()
    for r in c.execute('SELECT gid, object_key, uploaded_at FROM clips').fetchall():
        if r['gid'] in keep or time.time() - r['uploaded_at'] < 3600:
            continue
        if storage.ENABLED:
            storage.delete(r['object_key'])
        else:
            p = os.path.join(LOCAL, f"{r['gid']}.mp4")
            if os.path.exists(p):
                os.remove(p)
        c.execute('DELETE FROM clips WHERE gid=?', (r['gid'],))
