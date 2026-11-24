"""Dry-run posting backend: records what would be posted; nothing leaves this Mac.
Same interface as backends/zernio.py: publish(video, caption, targets, meta) and metrics(remote_id, platform, account)."""
import json, os, time

LOG = os.path.expanduser('~/Movies/AI Editors/logs/dryrun_posts.jsonl')


def publish(video_path, caption, targets, meta):
    rid = f"dryrun-{meta['idempotency']}-{int(time.time())}"
    with open(LOG, 'a') as f:
        f.write(json.dumps({'id': rid, 'targets': targets, 'video': video_path, 'caption': caption, 'meta': meta,
                            'at': time.strftime('%Y-%m-%d %H:%M:%S')}) + '\n')
    return {t['platform']: {'id': rid, 'url': None, 'status': 'published', 'error': None} for t in targets}


def metrics(remote_id, platform, account):
    return None  # nothing to measure on a dry run


def list_accounts():
    return []


def status(remote_id):
    return {}


def unpublish(remote_id, platform):
    return None
