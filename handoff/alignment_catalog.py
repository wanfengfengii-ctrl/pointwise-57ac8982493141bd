from dataclasses import replace
import sqlite3

from handoff.alignment import AlignmentEvidence, validate_alignment
from handoff.alignment_freeze import alignment_package_id
from handoff.alignment_package import decode_alignment, encode_alignment
from storage import InvalidEvidence, MissingItem, identifier, write_transaction


def initialize_alignment_catalog(db: sqlite3.Connection) -> None:
    db.execute('''
        CREATE TABLE IF NOT EXISTS alignment_packages (
            package_id TEXT PRIMARY KEY NOT NULL,
            origin TEXT NOT NULL,
            station TEXT NOT NULL,
            revision TEXT NOT NULL,
            model TEXT NOT NULL,
            binding TEXT NOT NULL,
            payload TEXT NOT NULL,
            UNIQUE (binding)
        )
    ''')


class AlignmentCatalog:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    @staticmethod
    def binding(evidence: AlignmentEvidence) -> str:
        # V2 acceptance identity: source + model + the complete reference
        # ticket to visible reading-version map. Levels and anchor level
        # numbers stay part of the body checked for text conflicts.
        anchor_versions = {ticket: info.reading_revision for ticket, info in evidence.anchors}
        return alignment_package_id(evidence.source, evidence.model, anchor_versions)

    def save(self, evidence: AlignmentEvidence) -> AlignmentEvidence:
        evidence = validate_alignment(evidence)
        binding = self.binding(evidence)
        with write_transaction(self.db):
            row = self.db.execute(
                'SELECT payload FROM alignment_packages WHERE package_id = ?', (evidence.package_id,)
            ).fetchone()
            if row is not None:
                existing = decode_alignment(row[0])
                if existing != evidence:
                    raise InvalidEvidence('the alignment package id already refers to different evidence')
                return existing
            row = self.db.execute(
                'SELECT package_id, payload FROM alignment_packages WHERE binding = ?', (binding,)
            ).fetchone()
            if row is not None:
                existing = decode_alignment(row[1])
                if replace(evidence, package_id=existing.package_id) != existing:
                    raise InvalidEvidence('the alignment binding already has different evidence')
                return existing
            self.db.execute('''
                INSERT INTO alignment_packages(package_id, origin, station, revision, model, binding, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (evidence.package_id, evidence.source.origin, evidence.source.station,
                  evidence.source.revision, evidence.model, binding, encode_alignment(evidence)))
        return evidence

    def read(self, package_id: str) -> AlignmentEvidence:
        identifier(package_id, 'package id')
        row = self.db.execute(
            'SELECT payload FROM alignment_packages WHERE package_id = ?', (package_id,)
        ).fetchone()
        if row is None:
            raise MissingItem(f'no such alignment package {package_id!r}')
        return decode_alignment(row[0])
