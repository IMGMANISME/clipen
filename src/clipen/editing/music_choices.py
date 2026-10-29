"""Display-only grouping of imported music; rendering and asset IDs stay intact."""
from clipen.finishing.models import digest


def music_choices(store):
    result = []
    for record in store.records():
        asset = record['asset']
        if asset['role'] != 'music':
            continue
        public = {k: v for k, v in asset.items() if k not in {'relative_path', 'fingerprint', 'mtime_ns', 'size_bytes'}}
        rights = {k: v for k, v in asset.get('provenance', {}).items() if k not in {'retrieved_at', 'changes'}}
        public['duplicate_group'] = digest([asset.get('fingerprint') or asset['asset_id'], asset['duration_seconds'], rights])
        result.append(public)
    return result
