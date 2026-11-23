import sqlite3

from handoff.alignment import AlignmentEvidence
from handoff.alignment_catalog import AlignmentCatalog
from handoff.alignment_package import decode_alignment
from handoff.evidence import EvidenceSource
from storage import InvalidEvidence


def accept_alignment(
    db: sqlite3.Connection, payload: str, declared_source: EvidenceSource
) -> AlignmentEvidence:
    evidence = decode_alignment(payload)
    if not isinstance(declared_source, EvidenceSource) or evidence.source != declared_source:
        raise InvalidEvidence('the declared source does not match the alignment package')
    return AlignmentCatalog(db).save(evidence)
