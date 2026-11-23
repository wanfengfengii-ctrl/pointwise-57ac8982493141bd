from dataclasses import dataclass
import re

from handoff.evidence import EvidenceSource
from storage import InvalidEvidence, InvalidInput, identifier


POSITIVE_INTEGER = re.compile(r'^[1-9][0-9]*$')


@dataclass(frozen=True)
class AnchorInfo:
    level: int
    reading_revision: str


@dataclass(frozen=True)
class AlignmentEvidence:
    package_id: str
    source: EvidenceSource
    model: str
    levels: tuple[tuple[int, int], ...]
    anchors: tuple[tuple[str, AnchorInfo], ...]

    def anchor_map(self) -> dict[str, AnchorInfo]:
        return dict(self.anchors)


def require_positive_integer_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not POSITIVE_INTEGER.fullmatch(value):
        raise InvalidEvidence(f'{field} must be the decimal text of a positive integer')
    return value


def _validate_source(source: object) -> EvidenceSource:
    if not isinstance(source, EvidenceSource):
        raise InvalidEvidence('an alignment package needs a package source')
    try:
        identifier(source.origin, 'source origin')
        identifier(source.station, 'station')
        identifier(source.revision, 'source revision')
    except InvalidInput as exc:
        raise InvalidEvidence(str(exc)) from exc
    require_positive_integer_text(source.revision, 'source revision')
    return source


def validate_alignment(evidence: AlignmentEvidence) -> AlignmentEvidence:
    if not isinstance(evidence, AlignmentEvidence):
        raise InvalidEvidence('an alignment station package is required')
    source = _validate_source(evidence.source)
    try:
        identifier(evidence.package_id, 'package id')
        identifier(evidence.model, 'model')
    except InvalidInput as exc:
        raise InvalidEvidence(str(exc)) from exc
    if not isinstance(evidence.levels, tuple) or not evidence.levels:
        raise InvalidEvidence('an alignment package needs at least one level')
    levels = []
    referenced_levels = set()
    for entry in evidence.levels:
        if not isinstance(entry, tuple) or len(entry) != 2:
            raise InvalidEvidence('each level needs positive and negative multiplicities')
        positive, negative = entry
        if type(positive) is not int or type(negative) is not int or positive < 0 or negative < 0:
            raise InvalidEvidence('level multiplicities must be nonnegative integers')
        levels.append((positive, negative))
    if not isinstance(evidence.anchors, tuple) or not evidence.anchors:
        raise InvalidEvidence('an alignment package needs at least one reference anchor')
    anchors = {}
    boundary = int(source.revision)
    for ticket, info in evidence.anchors:
        try:
            identifier(ticket, 'reference ticket')
        except InvalidInput as exc:
            raise InvalidEvidence(str(exc)) from exc
        if ticket in anchors:
            raise InvalidEvidence('a reference ticket appears more than once')
        if not isinstance(info, AnchorInfo):
            raise InvalidEvidence('each anchor needs a level and a reading revision')
        if type(info.level) is not int or not 0 <= info.level < len(levels):
            raise InvalidEvidence('anchor level must index a package level')
        require_positive_integer_text(info.reading_revision, 'reading revision')
        if int(info.reading_revision) > boundary:
            raise InvalidEvidence('reading revision must not be later than the freeze boundary')
        anchors[ticket] = AnchorInfo(info.level, info.reading_revision)
        referenced_levels.add(info.level)
    for index, (positive, negative) in enumerate(levels):
        if positive == 0 and negative == 0 and index not in referenced_levels:
            raise InvalidEvidence('a level without production counts must be referenced by an anchor')
    return AlignmentEvidence(
        evidence.package_id, source, evidence.model,
        tuple(levels), tuple(sorted(anchors.items())),
    )
