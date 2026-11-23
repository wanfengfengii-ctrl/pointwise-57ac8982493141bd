import json

from handoff.alignment import AlignmentEvidence, AnchorInfo, require_positive_integer_text, validate_alignment
from handoff.evidence import EvidenceSource
from storage import InvalidEvidence, InvalidInput


PACKAGE_VERSION = 2


def encode_alignment(evidence: AlignmentEvidence) -> str:
    evidence = validate_alignment(evidence)
    payload = {
        'version': PACKAGE_VERSION,
        'package_id': evidence.package_id,
        'source': {
            'origin': evidence.source.origin,
            'station': evidence.source.station,
            'revision': evidence.source.revision,
        },
        'model': evidence.model,
        'levels': [[positive, negative] for positive, negative in evidence.levels],
        'anchors': {
            ticket: {'level': info.level, 'reading_revision': info.reading_revision}
            for ticket, info in evidence.anchors
        },
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _unique_fields(pairs: list) -> dict:
    result = {}
    for name, value in pairs:
        if name in result:
            raise InvalidEvidence(f'duplicate JSON field {name!r}')
        result[name] = value
    return result


def _object(value: object, fields: set[str], description: str) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise InvalidEvidence(f'invalid fields for {description}')
    return value


def _array(value: object, description: str, size: int | None = None) -> list:
    if not isinstance(value, list) or (size is not None and len(value) != size):
        raise InvalidEvidence(f'invalid array for {description}')
    return value


def _decode_anchors(value: object, level_count: int) -> tuple[tuple[str, AnchorInfo], ...]:
    if not isinstance(value, dict) or not value:
        raise InvalidEvidence('an alignment package needs at least one reference anchor')
    anchors = []
    for ticket, info in value.items():
        if not isinstance(ticket, str):
            raise InvalidEvidence('reference ticket must be a string')
        info = _object(info, {'level', 'reading_revision'}, 'reference anchor')
        if type(info['level']) is not int or not 0 <= info['level'] < level_count:
            raise InvalidEvidence('anchor level must index a package level')
        revision = require_positive_integer_text(info['reading_revision'], 'reading revision')
        anchors.append((ticket, AnchorInfo(info['level'], revision)))
    return tuple(anchors)


def decode_alignment(payload: str) -> AlignmentEvidence:
    if not isinstance(payload, str):
        raise InvalidEvidence('alignment package must be JSON text')
    try:
        data = json.loads(payload, object_pairs_hook=_unique_fields)
    except (ValueError, RecursionError) as exc:
        raise InvalidEvidence('invalid alignment package JSON') from exc
    data = _object(
        data,
        {'version', 'package_id', 'source', 'model', 'levels', 'anchors'},
        'alignment package',
    )
    if type(data['version']) is not int or data['version'] != PACKAGE_VERSION:
        raise InvalidEvidence('unsupported alignment package version')
    source = _object(data['source'], {'origin', 'station', 'revision'}, 'package source')
    try:
        levels = []
        for entry in _array(data['levels'], 'grade levels'):
            pair = _array(entry, 'grade level', 2)
            if any(type(value) is not int or value < 0 for value in pair):
                raise InvalidEvidence('level multiplicities must be nonnegative integers')
            levels.append(tuple(pair))
        if not levels:
            raise InvalidEvidence('an alignment package needs at least one level')
        anchors = _decode_anchors(data['anchors'], len(levels))
    except InvalidInput as exc:
        raise InvalidEvidence(str(exc)) from exc
    evidence = AlignmentEvidence(
        data['package_id'], EvidenceSource(**source), data['model'], tuple(levels), anchors,
    )
    return validate_alignment(evidence)
