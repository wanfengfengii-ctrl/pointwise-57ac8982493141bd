from collections.abc import Mapping
from hashlib import sha256
import json
import sqlite3

from handoff.alignment import AlignmentEvidence, AnchorInfo
from handoff.alignment_package import encode_alignment
from handoff.evidence import EvidenceSource
from scoring.snapshots import read_station_snapshot
from storage import InvalidInput, identifier


def alignment_package_id(source: EvidenceSource, model: str, anchor_versions: dict[str, str]) -> str:
    identity = json.dumps(
        [source.origin, source.station, source.revision, model,
         sorted(anchor_versions.items())],
        separators=(',', ':'),
    )
    return 'alignment-' + sha256(identity.encode('utf-8')).hexdigest()


def _check_anchors(anchors: object) -> dict[str, str]:
    if not isinstance(anchors, Mapping) or not anchors:
        raise InvalidInput('anchors must map each reference ticket to one approved occurrence')
    selected = {}
    for ticket, occurrence in anchors.items():
        identifier(ticket, 'reference ticket')
        identifier(occurrence, 'reference occurrence')
        if ticket in selected:
            raise InvalidInput('a reference ticket appears more than once')
        selected[ticket] = occurrence
    return selected


def freeze_alignment(
    source_db: sqlite3.Connection,
    station: str,
    revision: int,
    model: str,
    anchors: Mapping[str, str],
) -> str:
    identifier(station, 'station')
    identifier(model, 'model')
    if type(revision) is not int or revision <= 0:
        raise InvalidInput('revision must be a positive V1 score revision id')
    selected = _check_anchors(anchors)
    snapshot = read_station_snapshot(source_db, station, revision)
    if model not in snapshot.models.names:
        raise InvalidInput(f'unknown model {model!r} for station {station!r}')
    model_index = snapshot.models.names.index(model)

    by_occurrence = {version.pair.occurrence: version for version in snapshot.occurrences}
    anchor_occurrences = set()
    anchor_versions = {}
    anchor_tickets = {}
    for ticket, occurrence in selected.items():
        version = by_occurrence.get(occurrence)
        if version is None:
            raise InvalidInput(f'occurrence {occurrence!r} is not visible at revision {revision}')
        if version.pair.ticket != ticket:
            raise InvalidInput(f'occurrence {occurrence!r} does not belong to reference ticket {ticket!r}')
        anchor_occurrences.add(occurrence)
        anchor_versions[ticket] = str(version.revision)
        anchor_tickets[ticket] = version

    # All occurrences of any selected reference board leave production measurement.
    production = [
        version for version in snapshot.occurrences
        if version.pair.ticket not in selected
    ]
    grade_scores = sorted({
        version.pair.scores[model_index]
        for version in [*production, *anchor_tickets.values()]
    })
    grade_index = {score: index for index, score in enumerate(grade_scores)}
    levels = [[0, 0] for _ in grade_scores]
    for version in production:
        index = grade_index[version.pair.scores[model_index]]
        if version.pair.label.value == 'positive':
            levels[index][0] += 1
        else:
            levels[index][1] += 1

    anchor_infos = []
    for ticket, version in anchor_tickets.items():
        level = grade_index[version.pair.scores[model_index]]
        anchor_infos.append((ticket, AnchorInfo(level, str(version.revision))))
    anchor_infos.sort()

    source = EvidenceSource(snapshot.origin, snapshot.station, str(snapshot.revision))
    evidence = AlignmentEvidence(
        alignment_package_id(source, model, anchor_versions),
        source, model, tuple(tuple(level) for level in levels),
        tuple(anchor_infos),
    )
    return encode_alignment(evidence)
