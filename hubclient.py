"""How a worker machine talks to the central hub. Outbound HTTPS only, through Cloudflare Access.

Credentials, in ~/.config/ai-editors/secrets.json on the worker (chmod 600, never committed):
  hub_url                  https://<hub>.workers.dev
  hub_worker_token         this machine's own hub token (shown once when Matt adds the worker in the hub)
  cf_access_client_id      this machine's Cloudflare Access service token
  cf_access_client_secret
Revoking either the service token (Cloudflare) or the worker (hub) cuts the machine off.
"""
import json, os, urllib.error, urllib.request

SECRETS = os.path.expanduser(os.environ.get('AI_EDITORS_SECRETS') or '~/.config/ai-editors/secrets.json')


class HubError(RuntimeError):
    pass


class Temporary(HubError):
    """The hub or the network had a blip; safe to try again (flywheel's post retry looks for this name)."""


def creds():
    try:
        s = json.load(open(SECRETS))
    except (OSError, ValueError):
        s = {}
    need = ('hub_url', 'hub_worker_token', 'cf_access_client_id', 'cf_access_client_secret')
    if not all(s.get(k) for k in need):
        raise HubError(f'hub credentials missing in {SECRETS}: ' + ', '.join(k for k in need if not s.get(k)))
    return s


def configured():
    try:
        creds()
        return True
    except HubError:
        return False


def request(method, path, body=None, timeout=120, raw=None, ctype='application/json'):
    s = creds()
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(s['hub_url'].rstrip('/') + path, method=method, data=data, headers={
        'CF-Access-Client-Id': s['cf_access_client_id'], 'CF-Access-Client-Secret': s['cf_access_client_secret'],
        'X-Hub-Worker-Token': s['hub_worker_token'], 'Content-Type': ctype, 'User-Agent': 'ai-editor-worker'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            b = r.read()
            if 'json' not in (r.headers.get('Content-Type') or ''):
                raise Temporary(f'hub answered {path} with {r.headers.get("Content-Type")}: is the Access service token right?')
            return json.loads(b or b'{}')
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors='replace')[:300]
        try:
            j = json.loads(detail)
        except ValueError:
            j = {'error': detail}
        if e.code >= 500 or e.code == 429 or j.get('temporary'):
            raise Temporary(f'hub {path} -> HTTP {e.code}: {j.get("error")}') from None
        raise HubError(f'hub {path} -> HTTP {e.code}: {j.get("error")}') from None
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        raise Temporary(f'hub {path} -> {e}') from None


def put_file(url, path, ctype='video/mp4', timeout=900):
    """Upload a file to a signed storage link (no credentials needed or sent)."""
    if url.startswith('/'):  # local testing: the hub stores it itself
        return request('PUT', url, raw=open(path, 'rb').read(), ctype=ctype, timeout=timeout)
    with open(path, 'rb') as f:
        req = urllib.request.Request(url, method='PUT', data=f.read(), headers={'Content-Type': ctype})
        urllib.request.urlopen(req, timeout=timeout).read()
