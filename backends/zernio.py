"""Zernio posting backend (https://docs.zernio.com). Standard library only.
API key: ~/.config/ai-editors/secrets.json -> {"zernio_api_key": "sk_..."} (created by Matt, chmod 600; never commit or print it).

publish(video, caption, targets, meta) uploads the video once and posts it to every target account in one request.
targets: [{"platform": "tiktok"|"instagram"|"youtube", "account": "<Zernio account _id>"}]
"""
import json, mimetypes, os, urllib.error, urllib.parse, urllib.request

BASE = 'https://zernio.com/api/v1'
SECRETS = os.path.expanduser('~/.config/ai-editors/secrets.json')


def _key():
    if os.environ.get('ZERNIO_API_KEY'):  # hosted dashboard (Railway variable)
        return os.environ['ZERNIO_API_KEY']
    try:
        return json.load(open(SECRETS))['zernio_api_key']
    except (OSError, KeyError, ValueError):
        raise RuntimeError(f'Zernio API key missing: put {{"zernio_api_key": "sk_..."}} in {SECRETS}')


class Temporary(RuntimeError):
    """Zernio or the network had a blip (429, 5xx, timeout): safe to try again later."""


def _call(method, path, body=None, query=None, headers=None, tries=4):
    import time
    url = BASE + path + ('?' + urllib.parse.urlencode(query) if query else '')
    h = {'Authorization': f'Bearer {_key()}', 'Content-Type': 'application/json', **(headers or {})}
    for k in range(tries):
        req = urllib.request.Request(url, method=method, headers=h, data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.status, json.loads(r.read() or b'{}')
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors='replace')[:400]
            if e.code == 429 or e.code >= 500:
                if k < tries - 1:
                    time.sleep(3 * (k + 1) ** 2)  # 3 s, 12 s, 27 s
                    continue
                raise Temporary(f'Zernio {method} {path} -> HTTP {e.code}: {detail}') from None
            raise RuntimeError(f'Zernio {method} {path} -> HTTP {e.code}: {detail}') from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if k < tries - 1:
                time.sleep(3 * (k + 1) ** 2)
                continue
            raise Temporary(f'Zernio {method} {path} -> {e}') from None



def list_accounts():
    out, cursor = [], None
    while True:
        q = {'limit': 100, **({'cursor': cursor} if cursor else {})}
        _, j = _call('GET', '/accounts', query=q)
        out += j.get('accounts', [])
        cursor = (j.get('pagination') or {}).get('nextCursor')
        if not cursor:
            return out


def upload(video_path):
    size = os.path.getsize(video_path)
    ctype = mimetypes.guess_type(video_path)[0] or 'video/mp4'
    _, p = _call('POST', '/media/presign', {'filename': os.path.basename(video_path), 'contentType': ctype, 'size': size})
    with open(video_path, 'rb') as f:  # presigned storage URL: no Authorization header
        req = urllib.request.Request(p['uploadUrl'], method='PUT', data=f.read(), headers={'Content-Type': ctype})
        urllib.request.urlopen(req, timeout=600).read()
    return p['publicUrl']


def _tiktok_privacy(account):
    _, info = _call('GET', f'/accounts/{account}/tiktok/creator-info', query={'mediaType': 'video'})
    opts = json.dumps(info)
    return 'PUBLIC_TO_EVERYONE' if 'PUBLIC_TO_EVERYONE' in opts else 'SELF_ONLY'


def publish(video_path, caption, targets, meta):
    """meta: {'title': hook, 'idempotency': 'edit-12'}. Returns {platform: {'id','url','status','error'}}."""
    url = upload(video_path)
    platforms = []
    for t in targets:
        entry = {'platform': t['platform'], 'accountId': t['account']}
        if t['platform'] == 'youtube':
            entry['platformSpecificData'] = {'title': meta['title'][:100], 'visibility': 'public', 'madeForKids': False}
        if t['platform'] == 'instagram':
            entry['platformSpecificData'] = {'shareToFeed': True}
        platforms.append(entry)
    body = {'content': caption, 'mediaItems': [{'url': url, 'type': 'video'}], 'platforms': platforms, 'publishNow': True}
    tt = [t for t in targets if t['platform'] == 'tiktok']
    if tt:
        # Matt's standing instruction is to publish these automatically; these flags record that consent for TikTok.
        body['tiktokSettings'] = {'privacy_level': _tiktok_privacy(tt[0]['account']), 'allow_comment': True, 'allow_duet': True,
                                  'allow_stitch': True, 'content_preview_confirmed': True, 'express_consent_given': True}
    code, j = _call('POST', '/posts', body, headers={'Idempotency-Key': meta['idempotency']})
    post = j.get('post', {})
    res = {}
    for p in post.get('platforms', []):
        res[p['platform']] = {'id': post.get('_id'), 'url': p.get('platformPostUrl'), 'status': p.get('status'),
                              'error': p.get('errorMessage')}
    for t in targets:  # platforms missing from the response
        res.setdefault(t['platform'], {'id': post.get('_id'), 'url': None, 'status': post.get('status', f'http {code}'), 'error': j.get('message')})
    return res


def metrics(remote_id, platform, account):
    _, j = _call('GET', '/analytics', query={'postId': remote_id})
    for p in j.get('posts', []):
        if p.get('platform') == platform:
            a = p.get('analytics', {})
            return {'views': a.get('views'), 'likes': a.get('likes'), 'comments': a.get('comments'), 'shares': a.get('shares'),
                    'saves': a.get('saves'), 'impressions': a.get('impressions'), 'reach': a.get('reach'), 'url': p.get('platformPostUrl')}
    return None


def status(remote_id):
    """Per-platform state of a post: {platform: {'status', 'url', 'error'}}."""
    _, j = _call('GET', f'/posts/{remote_id}')
    p = j.get('post', j)
    return {x['platform']: {'status': x.get('status'), 'url': x.get('platformPostUrl'), 'error': x.get('errorMessage')}
            for x in p.get('platforms', [])}


UNPUBLISH_OK = {'youtube', 'facebook', 'linkedin', 'twitter', 'threads', 'pinterest', 'reddit', 'bluesky'}  # per Zernio's error list, 2026-11-23


def unpublish(remote_id, platform):
    """Remove a live post from the platform. TikTok and Instagram are not supported by Zernio (as of 2026-11-23)."""
    if platform not in UNPUBLISH_OK:
        raise NotImplementedError(f'Zernio cannot remove {platform} posts')
    _call('POST', f'/posts/{remote_id}/unpublish', {'platform': platform})


def post_stats(remote_id):
    """Lifetime stats for one Zernio post, per platform: {platform: {...}}. Platforms still syncing are left out."""
    _, j = _call('GET', '/analytics', query={'postId': remote_id})
    out = {}
    for p in j.get('platformAnalytics', []):
        a = p.get('analytics')
        if a:
            out[p['platform']] = {k: a.get(k) for k in ('views', 'likes', 'comments', 'shares', 'saves', 'reach', 'impressions')}
            out[p['platform']]['url'] = p.get('platformPostUrl')
    return out


def account_info():
    """Every connected account with its follower count (Zernio refreshes followers once a day)."""
    out = []
    for a in list_accounts():
        prof = a.get('profileId')
        out.append({'id': a['_id'], 'platform': a.get('platform'), 'username': a.get('username'), 'name': a.get('displayName'),
                    'followers': a.get('followersCount'), 'active': a.get('isActive'),
                    'profile': prof.get('_id') if isinstance(prof, dict) else prof})
    return out


def account_health():
    """{account_id: problem text or None} for every connected account. A problem means posting is at risk:
    can't post, token invalid, needs reconnecting, restricted/banned, or Zernio marks it 'error'.
    Analytics-only warnings are not problems."""
    _, j = _call('GET', '/accounts/health')
    out = {}
    for a in j.get('accounts', []):
        why = []
        if a.get('needsReconnect'):
            why.append('needs reconnecting in Zernio')
        if a.get('tokenValid') is False:
            why.append('login token expired')
        if a.get('canPost') is False:
            why.append("can't post")
        if a.get('publishingRestriction'):
            why.append(f"publishing restricted: {a['publishingRestriction']}")
        if a.get('disconnectReason'):
            why.append(f"disconnected: {a['disconnectReason']}")
        if a.get('status') == 'error' or why:
            iss = [i.get('message', i) if isinstance(i, dict) else i for i in a.get('issues') or []]
            out[a['accountId']] = '; '.join(why + [str(i) for i in iss]) or 'account error in Zernio'
        else:
            out[a['accountId']] = None
    return out


def nudge_sync():
    """Ask Zernio to re-pull stale accounts from the platforms (the account-wide analytics read triggers it; per-post reads
    don't). Returns when Zernio last pulled numbers from the platforms (epoch seconds) or None."""
    import datetime as _dt
    _, j = _call('GET', '/analytics', query={'limit': 1})
    ov = j.get('overview') or {}
    last = ov.get('lastSync')
    return {'last_sync': _dt.datetime.fromisoformat(last.replace('Z', '+00:00')).timestamp() if last else None,
            'stale_accounts': (ov.get('dataStaleness') or {}).get('staleAccountCount'),
            'sync_triggered': (ov.get('dataStaleness') or {}).get('syncTriggered')}
