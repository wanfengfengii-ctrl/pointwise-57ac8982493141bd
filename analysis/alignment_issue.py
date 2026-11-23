from collections.abc import Iterable
import json
import sqlite3

from analysis.alignment_scale import (
    build_scale_model, extremal_scale, pooled_auc, station_scales,
)
from handoff.alignment import AlignmentEvidence
from handoff.alignment_catalog import AlignmentCatalog, initialize_alignment_catalog
from storage import (
    InvalidInput, InvalidSelection, MissingItem, PublicationFailed,
    identifier, write_transaction,
)


def initialize_alignment_records(db: sqlite3.Connection) -> None:
    initialize_alignment_catalog(db)
    db.execute('''
        CREATE TABLE IF NOT EXISTS alignment_reports (
            report_id INTEGER PRIMARY KEY,
            payload TEXT NOT NULL
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS alignment_sources (
            report_id INTEGER NOT NULL REFERENCES alignment_reports(report_id),
            position INTEGER NOT NULL,
            package_id TEXT NOT NULL,
            origin TEXT NOT NULL,
            station TEXT NOT NULL,
            revision TEXT NOT NULL,
            PRIMARY KEY (report_id, position)
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS alignment_receipts (
            report_id INTEGER PRIMARY KEY REFERENCES alignment_reports(report_id),
            receipt_id TEXT NOT NULL UNIQUE
        )
    ''')


def _select_packages(catalog: AlignmentCatalog, package_ids: Iterable[str]) -> tuple[AlignmentEvidence, ...]:
    if not isinstance(package_ids, Iterable) or isinstance(package_ids, (str, bytes, bytearray)):
        raise InvalidSelection('provide a collection of alignment package ids')
    try:
        ids = {identifier(package_id, 'package id') for package_id in package_ids}
    except InvalidInput as exc:
        raise InvalidSelection(str(exc)) from exc
    if len(ids) != 2:
        raise InvalidSelection('an alignment needs exactly two station packages')
    packages = []
    for package_id in sorted(ids):
        try:
            packages.append(catalog.read(package_id))
        except MissingItem as exc:
            raise InvalidSelection(str(exc)) from exc
    stations = {package.source.station for package in packages}
    if len(stations) != 2:
        raise InvalidSelection('the two packages must come from different stations')
    models = {package.model for package in packages}
    if len(models) != 1:
        raise InvalidSelection('the two packages must use the same model')
    anchor_sets = [set(package.anchor_map()) for package in packages]
    if anchor_sets[0] != anchor_sets[1]:
        raise InvalidSelection('the two packages must select the same reference tickets')
    return tuple(sorted(packages, key=lambda package: package.source.station))


def _report_payload(report_id: int, model: str, packages, minimum, maximum, scales_min, scales_max) -> dict:
    return {
        'status': 'issued',
        'report_id': report_id,
        'model': model,
        'min': {'auc': str(minimum.auc), 'scale': scales_min},
        'max': {'auc': str(maximum.auc), 'scale': scales_max},
        'sources': [
            {
                'package_id': package.package_id,
                'origin': package.source.origin,
                'station': package.source.station,
                'revision': package.source.revision,
            }
            for package in packages
        ],
        'receipt': {'report_id': report_id, 'receipt_id': f'alignment-receipt-{report_id}'},
    }


def issue_alignment(db: sqlite3.Connection, package_ids: Iterable[str]) -> dict:
    initialize_alignment_records(db)
    packages = _select_packages(AlignmentCatalog(db), package_ids)
    model = build_scale_model(packages)
    if model is None:
        return {'status': 'incompatible'}
    if model.positive_total == 0 or model.negative_total == 0:
        return {'status': 'insufficient'}

    minimum = extremal_scale(model, maximize=False)
    maximum = extremal_scale(model, maximize=True)
    scales_min = station_scales(model, minimum)
    scales_max = station_scales(model, maximum)
    # Every witness must reproduce its endpoint from the two received pieces.
    if (pooled_auc(packages, scales_min) != minimum.auc
            or pooled_auc(packages, scales_max) != maximum.auc):
        raise RuntimeError('alignment scale witness failed to reproduce the endpoint')

    try:
        with write_transaction(db):
            cursor = db.execute('INSERT INTO alignment_reports(payload) VALUES (?)', ('',))
            report_id = cursor.lastrowid
            payload = _report_payload(report_id, packages[0].model, packages,
                                      minimum, maximum, scales_min, scales_max)
            db.execute('UPDATE alignment_reports SET payload = ? WHERE report_id = ?',
                       (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                   separators=(',', ':')), report_id))
            db.executemany('''
                INSERT INTO alignment_sources(report_id, position, package_id, origin, station, revision)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', ((report_id, index, package.package_id, package.source.origin,
                   package.source.station, package.source.revision)
                  for index, package in enumerate(packages)))
            db.execute(
                'INSERT INTO alignment_receipts(report_id, receipt_id) VALUES (?, ?)',
                (report_id, f'alignment-receipt-{report_id}'),
            )
    except sqlite3.Error as exc:
        raise PublicationFailed('could not commit the alignment report') from exc
    return payload


def read_alignment(db: sqlite3.Connection, report_id: int, *, as_json: bool = False):
    initialize_alignment_records(db)
    if type(report_id) is not int or report_id <= 0:
        raise InvalidInput('report id must be a positive integer')
    if report_id >= 2**63:
        raise MissingItem(f'no such alignment report {report_id}')
    row = db.execute(
        'SELECT payload FROM alignment_reports WHERE report_id = ?', (report_id,)
    ).fetchone()
    if row is None:
        raise MissingItem(f'no such alignment report {report_id}')
    payload = json.loads(row[0])
    if as_json:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return payload
