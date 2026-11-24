"""Private clip storage: a Railway bucket (S3-compatible). Nothing in it is public; the hub signs links that expire
in minutes (AWS Signature V4, query-string form). Workers never hold the bucket keys: the hub hands them a signed
upload link for one clip at a time.

Env (Railway bucket variables; either naming works): BUCKET / AWS_S3_BUCKET_NAME, ENDPOINT / AWS_ENDPOINT_URL,
REGION / AWS_DEFAULT_REGION (default 'auto'), ACCESS_KEY_ID / AWS_ACCESS_KEY_ID, SECRET_ACCESS_KEY / AWS_SECRET_ACCESS_KEY,
URL_STYLE / AWS_S3_URL_STYLE ('virtual', the Railway default, or 'path').
Without a bucket (local testing only) clips go to DATA_DIR/clips and the hub serves them itself.
"""
import datetime as dt, hashlib, hmac, os, urllib.error, urllib.parse, urllib.request


def _env(*names, default=''):
    for n in names:
        if os.environ.get(n):
            return os.environ[n]
    return default


BUCKET = _env('BUCKET', 'AWS_S3_BUCKET_NAME')
ENDPOINT = _env('ENDPOINT', 'AWS_ENDPOINT_URL').rstrip('/')
REGION = _env('REGION', 'AWS_DEFAULT_REGION', default='auto')
KEY_ID = _env('ACCESS_KEY_ID', 'AWS_ACCESS_KEY_ID')
SECRET = _env('SECRET_ACCESS_KEY', 'AWS_SECRET_ACCESS_KEY')
STYLE = _env('URL_STYLE', 'AWS_S3_URL_STYLE', default='virtual')
ENABLED = bool(BUCKET and ENDPOINT and KEY_ID and SECRET)


def origin():
    """Where signed links point (for the page's Content-Security-Policy)."""
    if not ENABLED:
        return ''
    u = urllib.parse.urlparse(ENDPOINT)
    return f'{u.scheme}://{BUCKET}.{u.netloc}' if STYLE == 'virtual' else f'{u.scheme}://{u.netloc}'


def _q(s, safe='-_.~'):
    return urllib.parse.quote(s, safe=safe)


def presign(method, key, expires=600, now=None, *, bucket=None, endpoint=None, region=None, key_id=None, secret=None,
            style=None, content_type=None):
    """A signed URL for one object. Defaults come from the environment; the keyword overrides exist for tests."""
    bucket, endpoint = bucket or BUCKET, (endpoint or ENDPOINT).rstrip('/')
    region, key_id, secret, style = region or REGION, key_id or KEY_ID, secret or SECRET, style or STYLE
    u = urllib.parse.urlparse(endpoint)
    if style == 'virtual':
        host, path = f'{bucket}.{u.netloc}', '/' + _q(key, safe='/-_.~')
    else:
        host, path = u.netloc, f'/{bucket}/' + _q(key, safe='/-_.~')
    t = now or dt.datetime.now(dt.timezone.utc)
    amz, day = t.strftime('%Y%m%dT%H%M%SZ'), t.strftime('%Y%m%d')
    scope = f'{day}/{region}/s3/aws4_request'
    signed_headers = 'content-type;host' if content_type else 'host'
    q = {'X-Amz-Algorithm': 'AWS4-HMAC-SHA256', 'X-Amz-Credential': f'{key_id}/{scope}', 'X-Amz-Date': amz,
         'X-Amz-Expires': str(int(expires)), 'X-Amz-SignedHeaders': signed_headers}
    qs = '&'.join(f'{_q(k)}={_q(v)}' for k, v in sorted(q.items()))
    headers = (f'content-type:{content_type}\n' if content_type else '') + f'host:{host}\n'
    canonical = '\n'.join([method, path, qs, headers, signed_headers, 'UNSIGNED-PAYLOAD'])
    to_sign = '\n'.join(['AWS4-HMAC-SHA256', amz, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    k = ('AWS4' + secret).encode()
    for part in (day, region, 's3', 'aws4_request'):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    return f'{u.scheme}://{host}{path}?{qs}&X-Amz-Signature={sig}'


def put_bytes(key, data, content_type='application/octet-stream'):
    req = urllib.request.Request(presign('PUT', key, 300, content_type=content_type), data=data, method='PUT',
                                 headers={'Content-Type': content_type})
    urllib.request.urlopen(req, timeout=300).read()


def delete(key):
    req = urllib.request.Request(presign('DELETE', key, 300), method='DELETE')
    try:
        urllib.request.urlopen(req, timeout=60).read()
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
