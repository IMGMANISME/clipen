"""Preparation data is inert text, never a file grant, Plan, or AI work."""

from .store import Rejected

FIELDS = ('source_directory', 'reference_directory', 'brief')
LIMITS = {'source_directory': 4096, 'reference_directory': 4096, 'brief': 8000}


def validate(body):
    if set(body) != {'request_id', 'expected_version', *FIELDS}:
        raise Rejected('invalid_draft_fields', 422)
    if type(body['expected_version']) is not int or body['expected_version'] < 0:
        raise Rejected('expected_draft_version_required', 422)
    for name in FIELDS:
        value = body[name]
        try:
            valid = isinstance(value, str) and len(value.encode('utf-8')) <= LIMITS[name]
        except UnicodeError:
            valid = False
        if not valid:
            raise Rejected('invalid_draft_' + name, 422)
        allowed_controls = '\n\t' if name == 'brief' else ''
        if any((ord(char) < 32 or ord(char) == 127) and char not in allowed_controls for char in value):
            raise Rejected('invalid_draft_' + name, 422)
    # A partial draft is useful. Do not resolve, expand, stat or open its paths.
    return {name: body[name] for name in FIELDS}
