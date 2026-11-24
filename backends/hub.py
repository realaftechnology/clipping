"""Posting through the central hub: same interface as backends/zernio.py, but the Zernio key stays on the hub.

The clip goes straight from this machine to Zernio's storage on a signed link the hub asks for; the hub then
publishes it, after checking the accounts belong to this machine's approved editors. flywheel.py uses this in place
of zernio when config.local.json has "zernio_via_hub": true, so queued and past posts keep working unchanged.
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hubclient as hc  # noqa: E402

Temporary = hc.Temporary


def _op(op, **a):
    r = hc.request('POST', f'/api/worker/zernio/{op}', a, timeout=300)
    if r.get('not_implemented'):
        raise NotImplementedError(r.get('error'))
    return r['result']


def upload(video_path):
    p = _op('presign', filename=os.path.basename(video_path), size=os.path.getsize(video_path))
    try:
        hc.put_file(p['uploadUrl'], video_path)
    except Exception as ex:
        raise Temporary(f'upload to Zernio storage failed: {ex}') from None
    return p['publicUrl']


def publish(video_path, caption, targets, meta):
    return _op('publish', media_url=upload(video_path), caption=caption, targets=targets, meta=meta)


def metrics(remote_id, platform, account):
    return _op('metrics', remote_id=remote_id, platform=platform, account=account)


def status(remote_id):
    return _op('status', remote_id=remote_id)


def unpublish(remote_id, platform):
    return _op('unpublish', remote_id=remote_id, platform=platform)


def post_stats(remote_id):
    return _op('post_stats', remote_id=remote_id)


def account_info():
    return _op('account_info')


def list_accounts():
    return _op('list_accounts')


def account_health():
    return _op('account_health')


def nudge_sync():
    return _op('nudge_sync')
