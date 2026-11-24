"""Who is asking, checked on every request (the second gate behind Cloudflare Access).

1. Cloudflare Access lets a request through only for an allowlisted identity and adds a signed token
   (Cf-Access-Jwt-Assertion). Here the hub verifies that token itself: RS256 signature against the team's published
   keys, this application's audience tag, the issuer and expiry. A request without a valid token gets nothing.
2. The identity must also be approved in the hub's own tables: a person (users, by email, with a role) or a worker
   machine (workers, by its Access service-token client id, plus its own hub token). Unknown identities get nothing.
3. The front-door Worker adds a shared secret header, so only traffic that came through the gate is answered.

Env: ACCESS_TEAM_DOMAIN (e.g. realaf.cloudflareaccess.com), ACCESS_AUD (the Access application's audience tag),
GATE_SECRET, HUB_OWNER_EMAIL (Matt: always admin, the only one who manages people and roles),
SESSION_HOURS (default 12; older sign-ins must sign in again), REAUTH_MINUTES (default 60: admin changes need a
sign-in this recent).
Testing only: HUB_ENV=test with HUB_TEST_JWKS (a local key file) instead of the team's keys.
"""
import hashlib, hmac, json, os, time

import jwt  # PyJWT[crypto]

import store
from directives import rank

TEAM = os.environ.get('ACCESS_TEAM_DOMAIN', '').strip().rstrip('/').replace('https://', '')
AUD = os.environ.get('ACCESS_AUD', '').strip()
GATE_SECRET = os.environ.get('GATE_SECRET', '')
OWNER = os.environ.get('HUB_OWNER_EMAIL', '').strip().lower()
SESSION_HOURS = float(os.environ.get('SESSION_HOURS', 12))
REAUTH_MINUTES = float(os.environ.get('REAUTH_MINUTES', 60))
TEST = os.environ.get('HUB_ENV') == 'test' and not os.environ.get('RAILWAY_ENVIRONMENT')  # never on the real host

_jwks = None


def configured():
    """Refuse to serve anything unless every gate setting is present."""
    missing = [n for n, v in (('ACCESS_TEAM_DOMAIN', TEAM or TEST), ('ACCESS_AUD', AUD), ('GATE_SECRET', GATE_SECRET),
                              ('HUB_OWNER_EMAIL', OWNER)) if not v]
    return missing


def _keys():
    global _jwks
    if _jwks is None:
        if TEST and os.environ.get('HUB_TEST_JWKS'):
            _jwks = jwt.PyJWKSet.from_json(open(os.environ['HUB_TEST_JWKS']).read())
        else:
            _jwks = jwt.PyJWKClient(f'https://{TEAM}/cdn-cgi/access/certs', cache_keys=True, lifespan=3600)
    return _jwks


def _verify(token):
    k = _keys()
    if isinstance(k, jwt.PyJWKSet):
        kid = jwt.get_unverified_header(token).get('kid')
        key = next((x.key for x in k.keys if x.key_id == kid), None)
        if key is None:
            raise jwt.InvalidTokenError('unknown key')
    else:
        key = k.get_signing_key_from_jwt(token).key
    iss = os.environ.get('HUB_TEST_ISS') if TEST else f'https://{TEAM}'
    return jwt.decode(token, key, algorithms=['RS256'], audience=AUD, issuer=iss,
                      options={'require': ['exp', 'aud', 'iss']}, leeway=30)


def token_hash(t):
    return hashlib.sha256(t.encode()).hexdigest()


class Who:
    """The caller. kind: 'user' (a person or Grok Bot), 'worker' (an editing machine)."""
    def __init__(self, kind, ident, role=None, editors=None, name=None, iat=0, session=''):
        self.kind, self.id, self.role, self.editors, self.name, self.iat, self.session = kind, ident, role, editors, name, iat, session

    @property
    def owner(self):
        return self.kind == 'user' and self.id == OWNER

    def can(self, role):
        return self.kind == 'user' and rank(self.role) >= rank(role)

    def sees(self, editor):
        return self.editors is None or editor in self.editors

    def fresh(self, minutes=None):
        return time.time() - self.iat <= 60 * (minutes or REAUTH_MINUTES)

    def label(self):
        return self.name or self.id


def identify(headers):
    """Returns (Who, None) or (None, reason). The reason is logged, never shown in detail to the caller."""
    if not GATE_SECRET or not hmac.compare_digest(headers.get('X-Hub-Gate', ''), GATE_SECRET):
        return None, 'request did not come through the gate'
    tok = headers.get('Cf-Access-Jwt-Assertion', '')
    if not tok:
        return None, 'no Access token'
    try:
        claims = _verify(tok)
    except Exception as ex:
        return None, f'Access token rejected: {type(ex).__name__}'
    c = store.db()
    iat = float(claims.get('iat', 0))
    email = (claims.get('email') or '').strip().lower()
    if email:  # a person (or Grok Bot's own Google account) signed in through Google
        if time.time() - iat > SESSION_HOURS * 3600:
            return None, 'session too old'
        if email == OWNER:
            r = c.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
            return Who('user', email, 'admin', None, (r['name'] if r else None) or 'Matt', iat, claims.get('nonce') or str(int(iat))), None
        r = c.execute('SELECT * FROM users WHERE email=? AND active=1', (email,)).fetchone()
        if not r:
            return None, f'{email} is not approved in the hub'
        return Who('user', email, r['role'], json.loads(r['editors']) if r['editors'] else None, r['name'], iat,
                   claims.get('nonce') or str(int(iat))), None
    cid = claims.get('common_name') or ''
    if not cid:
        return None, 'token names no identity'
    w = c.execute('SELECT * FROM workers WHERE access_client_id=? AND active=1', (cid,)).fetchone()
    if w:  # an editing machine: its service token AND its own hub token
        t = headers.get('X-Hub-Worker-Token', '')
        if not t or not hmac.compare_digest(token_hash(t), w['token_hash']):
            return None, f"worker {w['id']}: wrong hub token"
        eds = [r['id'] for r in c.execute("SELECT id FROM editors WHERE worker_id=? AND status='approved'", (w['id'],))]
        return Who('worker', w['id'], None, eds, w['name'], iat, cid), None
    u = c.execute('SELECT * FROM users WHERE client_id=? AND active=1', (cid,)).fetchone()
    if u:  # a bot calling the API with its own service token (e.g. Grok Bot without a browser)
        return Who('user', u['email'], u['role'], json.loads(u['editors']) if u['editors'] else None, u['name'], iat, cid), None
    return None, f'service token {cid[:8]}... is not approved in the hub'
