"""The hub's central database (SQLite on the hub's private volume, WAL mode, backed up nightly to the private bucket).

Holds: people and roles, workers and their editors, each editor's settings as last reported, the review queue's
actions, chat history, directives, shared lessons (content profile, performance playbook, network rules, review
feedback), posts and stats pulled from Zernio, login events and alerts, and an append-only, hash-chained audit log.
"""
import hashlib, hmac, json, os, sqlite3, threading, time

DATA = os.environ.get('DATA_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data'))
os.makedirs(DATA, exist_ok=True)
PATH = os.path.join(DATA, 'hub.db')
_local = threading.local()
AUDIT_LOCK = threading.Lock()
# The audit chain is keyed (HMAC) with a secret kept outside the database, so someone with the database alone can't
# rewrite history and recompute the chain. The head is also sent out daily (zernio_hub.backup) so a cut-off tail shows.
PLACEHOLDER = b'local-test-only'
AUDIT_KEY = os.environ.get('AUDIT_KEY', '').encode() or PLACEHOLDER
if AUDIT_KEY == PLACEHOLDER and os.environ.get('RAILWAY_ENVIRONMENT'):
    raise SystemExit('AUDIT_KEY is not set: run hub/setup.sh hub. The hub will not run without it.')

SCHEMA = '''
CREATE TABLE IF NOT EXISTS users(
  email TEXT PRIMARY KEY, name TEXT, role TEXT NOT NULL, editors TEXT, client_id TEXT UNIQUE,
  active INTEGER NOT NULL DEFAULT 1, added_by TEXT, added_at REAL);
CREATE TABLE IF NOT EXISTS workers(
  id TEXT PRIMARY KEY, name TEXT, token_hash TEXT NOT NULL, access_client_id TEXT UNIQUE,
  active INTEGER NOT NULL DEFAULT 1, created_by TEXT, created_at REAL, last_seen REAL, last_ip TEXT, info TEXT);
CREATE TABLE IF NOT EXISTS editors(
  id TEXT PRIMARY KEY, worker_id TEXT, name TEXT, accounts TEXT, status TEXT NOT NULL DEFAULT 'pending',
  settings TEXT, style TEXT, notes TEXT, reported_at REAL, approved_by TEXT, approved_at REAL);
CREATE TABLE IF NOT EXISTS snapshots(worker_id TEXT PRIMARY KEY, snap TEXT, received REAL);
CREATE TABLE IF NOT EXISTS items(
  gid INTEGER PRIMARY KEY, worker_id TEXT, kind TEXT, local_id INTEGER, UNIQUE(worker_id, kind, local_id));
CREATE TABLE IF NOT EXISTS clips(
  gid INTEGER PRIMARY KEY, object_key TEXT, size INTEGER, uploaded_at REAL);
CREATE TABLE IF NOT EXISTS review_actions(
  id INTEGER PRIMARY KEY, gid INTEGER, worker_id TEXT, kind TEXT, local_id INTEGER, editor TEXT, action TEXT, reason TEXT,
  by_email TEXT, at REAL, sent_at REAL, done_at REAL, result TEXT);
CREATE TABLE IF NOT EXISTS chat(
  id INTEGER PRIMARY KEY, editor TEXT, role TEXT, author TEXT, text TEXT, at REAL, directives TEXT);
CREATE TABLE IF NOT EXISTS chat_jobs(
  id INTEGER PRIMARY KEY, editor TEXT, worker_id TEXT, message_id INTEGER, by_email TEXT, by_role TEXT,
  status TEXT, created REAL, sent_at REAL, done_at REAL, error TEXT);
CREATE TABLE IF NOT EXISTS directives(
  id INTEGER PRIMARY KEY, editor TEXT, action TEXT, params TEXT, summary TEXT, status TEXT,
  requested_by TEXT, requested_role TEXT, message_id INTEGER, decided_by TEXT, decided_at REAL,
  created REAL, sent_at REAL, applied_at REAL, result TEXT, undo TEXT, undo_requested INTEGER DEFAULT 0, undone_at REAL);
CREATE TABLE IF NOT EXISTS docs(
  name TEXT PRIMARY KEY, version INTEGER, text TEXT, updated_by TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS docs_history(id INTEGER PRIMARY KEY, name TEXT, version INTEGER, text TEXT, by TEXT, at REAL);
CREATE TABLE IF NOT EXISTS net_rules(
  uid TEXT PRIMARY KEY, kind TEXT, rule TEXT, reason TEXT, editor TEXT, active INTEGER, origin TEXT, at REAL, updated REAL);
CREATE TABLE IF NOT EXISTS net_feedback(
  uid TEXT PRIMARY KEY, editor TEXT, action TEXT, reason TEXT, hook TEXT, note TEXT, transcript TEXT, at REAL, origin TEXT);
CREATE TABLE IF NOT EXISTS zposts(
  remote_id TEXT, platform TEXT, account TEXT, username TEXT, url TEXT, published_at REAL, content TEXT,
  PRIMARY KEY(remote_id, platform));
CREATE TABLE IF NOT EXISTS zstats(
  remote_id TEXT, platform TEXT, taken_at REAL, views INTEGER, likes INTEGER, comments INTEGER, shares INTEGER, saves INTEGER,
  PRIMARY KEY(remote_id, platform));
CREATE TABLE IF NOT EXISTS zfollowers(
  account TEXT, platform TEXT, username TEXT, day TEXT, followers INTEGER, taken_at REAL, PRIMARY KEY(account, day));
CREATE TABLE IF NOT EXISTS uploads(public_url TEXT PRIMARY KEY, worker_id TEXT, at REAL);
CREATE TABLE IF NOT EXISTS published(remote_id TEXT PRIMARY KEY, worker_id TEXT, editor TEXT, at REAL);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS logins(
  id INTEGER PRIMARY KEY, identity TEXT, session TEXT, ip TEXT, country TEXT, agent TEXT, at REAL, new_device INTEGER,
  UNIQUE(identity, session));
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY, at REAL, level TEXT, text TEXT, seen INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY, at REAL, actor TEXT, actor_kind TEXT, action TEXT, target TEXT, detail TEXT, ip TEXT,
  prev TEXT, hash TEXT);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT, 'the audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT, 'the audit log is append-only'); END;
'''


def db():
    c = getattr(_local, 'c', None)
    if c is None:
        c = sqlite3.connect(PATH, timeout=30, isolation_level=None)  # autocommit; explicit BEGIN where it matters
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA busy_timeout=30000')
        if c.execute('PRAGMA journal_mode').fetchone()[0] != 'wal':
            c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA foreign_keys=ON')
        c.executescript(SCHEMA)
        _local.c = c
    return c


def meta(k, default=None):
    r = db().execute('SELECT v FROM meta WHERE k=?', (k,)).fetchone()
    return json.loads(r['v']) if r else default


def set_meta(k, v):
    db().execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (k, json.dumps(v)))


def audit(actor, actor_kind, action, target='', detail=None, ip=''):
    """Append one entry. Each entry's hash covers the previous one, so a rewritten history shows (verify_audit)."""
    with AUDIT_LOCK:
        c = db()
        last = c.execute('SELECT hash FROM audit ORDER BY id DESC LIMIT 1').fetchone()
        prev = last['hash'] if last else ''
        at = time.time()
        d = json.dumps(detail or {}, sort_keys=True, default=str)
        h = hmac.new(AUDIT_KEY, json.dumps([prev, at, actor, actor_kind, action, target, d, ip]).encode(), hashlib.sha256).hexdigest()
        c.execute('INSERT INTO audit(at,actor,actor_kind,action,target,detail,ip,prev,hash) VALUES(?,?,?,?,?,?,?,?,?)',
                  (at, actor, actor_kind, action, target, d, ip, prev, h))
        return h


def verify_audit():
    """Re-walk the chain. Entries from before the audit key was set (the hub's first starts, before setup) were sealed
    with the built-in placeholder key; those are accepted only at the very start of the log and counted separately."""
    prev, n, before_key, keyed = '', 0, 0, False
    for r in db().execute('SELECT * FROM audit ORDER BY id'):
        msg = json.dumps([prev, r['at'], r['actor'], r['actor_kind'], r['action'], r['target'], r['detail'], r['ip']]).encode()
        h = hmac.new(AUDIT_KEY, msg, hashlib.sha256).hexdigest()
        if r['prev'] == prev and r['hash'] == h:
            keyed = True
        elif not keyed and r['prev'] == prev and r['hash'] == hmac.new(PLACEHOLDER, msg, hashlib.sha256).hexdigest():
            h, before_key = r['hash'], before_key + 1
        else:
            return {'ok': False, 'broken_at': r['id'], 'checked': n, 'before_key': before_key}
        prev, n = h, n + 1
    return {'ok': True, 'checked': n, 'before_key': before_key, 'head': prev[:16]}


def alert(text, level='warn'):
    db().execute('INSERT INTO alerts(at,level,text) VALUES(?,?,?)', (time.time(), level, text[:500]))


def gid(worker_id, kind, local_id):
    c = db()
    c.execute('INSERT OR IGNORE INTO items(worker_id,kind,local_id) VALUES(?,?,?)', (worker_id, kind, int(local_id)))
    return c.execute('SELECT gid FROM items WHERE worker_id=? AND kind=? AND local_id=?', (worker_id, kind, int(local_id))).fetchone()['gid']


def item(g):
    return db().execute('SELECT * FROM items WHERE gid=?', (g,)).fetchone()


def backup_bytes():
    """A consistent copy of the database (SQLite online backup), for the nightly off-volume backup."""
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix='.db', dir=DATA)
    os.close(fd)
    dst = sqlite3.connect(tmp)
    db().backup(dst)
    dst.close()
    data = open(tmp, 'rb').read()
    os.remove(tmp)
    return data
