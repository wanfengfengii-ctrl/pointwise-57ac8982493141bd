import argparse
from contextlib import closing
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from analysis.alignment_issue import (
    initialize_alignment_records, issue_alignment as issue_alignment,
    read_alignment as read_alignment,
)
from analysis.comparison import issue_comparison as issue_comparison
from boards.catalog import initialize_boards
from boards.stations import admit_board as admit_board
from boards.tickets import BoardTicket as BoardTicket
from handoff.alignment import AlignmentEvidence as AlignmentEvidence
from handoff.alignment import AnchorInfo as AnchorInfo
from handoff.alignment_catalog import AlignmentCatalog as AlignmentCatalog
from handoff.alignment_exchange import accept_alignment as accept_alignment
from handoff.alignment_freeze import freeze_alignment as freeze_alignment
from handoff.alignment_package import (
    decode_alignment as decode_alignment, encode_alignment as encode_alignment,
)
from handoff.catalog import StationCatalog as StationCatalog
from handoff.catalog import initialize_handoff
from handoff.evidence import EvidenceSource as EvidenceSource
from handoff.exchange import accept_station as accept_station
from handoff.freeze import freeze_station as freeze_station
from handoff.package import encode_evidence as encode_evidence
from records.publication import initialize_records
from records.retrieval import read_report as read_report
from scoring.intake import record_pair as record_pair
from scoring.ledger import initialize_scoring
from scoring.pairs import ModelPair as ModelPair
from scoring.revisions import Correction as Correction
from scoring.snapshots import read_station_snapshot as read_station_snapshot
from storage import (
    InvalidEvidence, connect_database, write_transaction,
)


def open_source(path, *, origin: str | None = None):
    db = connect_database(path)
    try:
        with write_transaction(db):
            initialize_boards(db)
            initialize_scoring(db, origin=origin)
    except BaseException:
        db.close()
        raise
    return db


def open_archive(path):
    db = connect_database(path)
    try:
        with write_transaction(db):
            initialize_handoff(db)
            initialize_records(db)
            initialize_alignment_records(db)
    except BaseException:
        db.close()
        raise
    return db


def _sample(name: str):
    return json.loads((Path(__file__).resolve().parent / 'samples' / name).read_text(encoding='utf-8'))


def _freeze_shift(source, archive, shift: dict) -> dict[str, str]:
    models = ModelPair(*shift['models'])
    for board in shift['tickets']:
        ticket = BoardTicket(board['number'], board['entity'])
        for station in board['stations']:
            admit_board(source, ticket, station)
    packages = {}
    for station, measurements in shift['measurements'].items():
        latest = None
        for ticket, occurrence, label, left, right in measurements:
            scores = dict(zip(models.names, (left, right), strict=True))
            latest = record_pair(source, station, ticket, occurrence, label, models, scores)
        if latest is None:
            raise ValueError(f'no sample measurements for {station}')
        packages[station] = freeze_station(source, archive, station, latest.revision).package_id
    return packages


def _numbers(report) -> dict:
    return {
        'auc': [str(value) for value in report.auc],
        'deletions': [[row.ticket, *(str(value) for value in row.auc)] for row in report.deletions],
        'covariance': str(report.covariance),
    }


def _show_comparison(archive, case_id, inputs, ids, expected, expected_ids, oracle, paths):
    delivery = issue_comparison(archive, ids)
    report_id = delivery.report.report_id
    received = read_report(archive, report_id)
    actual = _numbers(delivery.report)
    downstream_expected = {'package_ids': expected_ids, 'complete_receipt': True}
    downstream_actual = {
        'package_ids': [source.package_id for source in received.sources],
        'complete_receipt': received == delivery and received.receipt.report_id == report_id,
    }
    if actual != expected or downstream_actual != downstream_expected:
        raise RuntimeError(f'{case_id}: comparison or its archived receipt disagrees with the hand ledger')
    print('POINTWISE_SCENARIO ' + json.dumps({
        'case_id': case_id,
        'input': inputs,
        'expected': expected,
        'actual': actual,
        'downstream_expected': downstream_expected,
        'downstream_actual': downstream_actual,
        'code_paths': paths,
        'oracle': oracle,
    }, ensure_ascii=False, sort_keys=True))
    print(read_report(archive, report_id, as_json=True))


def _build_alignment_case(root: Path, case: dict, origin: str, model_names: ModelPair, model: str):
    source = open_source(root / f'{case["name"]}-source.sqlite3', origin=origin)
    archive = open_archive(root / f'{case["name"]}-archive.sqlite3')
    revisions = {}
    for station, measurements in case['measurements'].items():
        latest = None
        for ticket, occurrence, label, left, right in measurements:
            admit_board(source, BoardTicket(ticket, f'film-{ticket}'), station)
            scores = dict(zip(model_names.names, (left, right), strict=True))
            latest = record_pair(source, station, ticket, occurrence, label, model_names, scores)
        revisions[station] = latest.revision
    return source, archive, revisions


def _accept_alignment_text(archive, text: str):
    decoded = decode_alignment(text)
    return accept_alignment(
        archive, text,
        EvidenceSource(decoded.source.origin, decoded.source.station, decoded.source.revision),
    )


def _show_alignment(archive, case_id, anchors, result, expected, oracle, paths):
    actual = (
        {'status': result['status']}
        if result['status'] != 'issued'
        else {'status': result['status'],
              'auc': [result['min']['auc'], result['max']['auc']],
              'production_counts': [
                  sum(positive for package in (
                      AlignmentCatalog(archive).read(source['package_id'])
                      for source in result['sources'])
                      for positive, _ in package.levels),
                  sum(negative for package in (
                      AlignmentCatalog(archive).read(source['package_id'])
                      for source in result['sources'])
                      for _, negative in package.levels),
              ]}
    )
    if actual != expected:
        raise RuntimeError(f'{case_id}: alignment result disagrees with the sample ledger')
    print('POINTWISE_SCENARIO ' + json.dumps({
        'case_id': case_id,
        'input': {'anchors': anchors},
        'expected': expected,
        'actual': actual,
        'code_paths': paths,
        'oracle': oracle,
    }, ensure_ascii=False, sort_keys=True))
    if result['status'] == 'issued':
        print(read_alignment(archive, result['report_id'], as_json=True))


def _run_alignment_demo(root: Path) -> None:
    document = _sample('reference-alignment.json')
    model_names = ModelPair(*document['models'])
    model = document['model']
    cases = {case['name']: case for case in document['cases']}
    paths = ['handoff/alignment_freeze.py', 'handoff/alignment_exchange.py',
             'analysis/alignment_scale.py', 'analysis/alignment_issue.py',
             'records persistence']

    # reference-choice: after the source is closed, the two reference choices
    # are signed from the received V2 station packages alone.
    case = cases['reference-choice']
    source, archive, revisions = _build_alignment_case(
        root, case, 'center-demo', model_names, model)
    accepted_ids = []
    for request in case['requests']:
        ids = []
        for station, anchors in request['anchors'].items():
            text = freeze_alignment(source, station, revisions[station], model, anchors)
            ids.append(_accept_alignment_text(archive, text).package_id)
        accepted_ids.append(ids)
    # A broken source declaration is rejected and creates no new receipt.
    text = freeze_alignment(source, 'East', revisions['East'], model, {'R': 'e-low'})
    decoded = decode_alignment(text)
    try:
        accept_alignment(
            archive, text,
            EvidenceSource('wrong-ledger', decoded.source.station, decoded.source.revision))
    except InvalidEvidence as exc:
        print('POINTWISE_SCENARIO ' + json.dumps({
            'case_id': 'a4.broken_source_rejected',
            'input': {'declared_origin': 'wrong-ledger'},
            'expected': 'INVALID_STATION_EVIDENCE',
            'actual': exc.code,
            'code_paths': ['handoff/alignment_exchange.py'],
            'oracle': 'A wrong source declaration leaves the received V2 catalog unchanged.',
        }, ensure_ascii=False, sort_keys=True))
    source.close()  # 关站：后续签发与领取只依赖归档库。
    finalized = []
    for request, ids in zip(case['requests'], accepted_ids, strict=True):
        result = issue_alignment(archive, ids)
        finalized.append(result)
        _show_alignment(
            archive, f"a4.reference_choice.{next(iter(request['anchors']['East']))}",
            request['anchors'], result, request['expected'],
            'Tied production scores count half; moving the East reference flips both AUC bounds.',
            paths,
        )
    # Reopen the archive and claim the finalized reports by their original ids.
    archive_path = root / f'{case["name"]}-archive.sqlite3'
    report_ids = [report['report_id'] for report in finalized]
    archive.close()
    with closing(open_archive(archive_path)) as reopened:
        for report_id, original in zip(report_ids, finalized, strict=True):
            stored = read_alignment(reopened, report_id)
            exported = read_alignment(reopened, report_id, as_json=True)
            if stored != original or json.loads(exported) != original:
                raise RuntimeError('reopened alignment export disagrees with the finalized report')
        print('POINTWISE_SCENARIO ' + json.dumps({
            'case_id': 'a4.reopen_claim',
            'input': {'report_ids': report_ids},
            'expected': report_ids,
            'actual': [read_alignment(reopened, report_id)['report_id'] for report_id in report_ids],
            'code_paths': ['analysis/alignment_issue.py'],
            'oracle': 'After the source closes and the archive reopens, original ids and values remain.',
        }, ensure_ascii=False, sort_keys=True))

    # The remaining sample ledgers demonstrate the incompatible and insufficient exits.
    for name, oracle in (
        ('inconsistent-anchor-tie', 'One shared value would have to match two strictly ordered East grades.'),
        ('no-production-negative', 'A compatible scale exists, but the pooled production set lacks a label.'),
    ):
        case = cases[name]
        source, archive, revisions = _build_alignment_case(
            root, case, f'{name}-origin', model_names, model)
        request = case['requests'][0]
        ids = []
        for station, anchors in request['anchors'].items():
            text = freeze_alignment(source, station, revisions[station], model, anchors)
            ids.append(_accept_alignment_text(archive, text).package_id)
        source.close()
        result = issue_alignment(archive, ids)
        _show_alignment(archive, f'a4.{name}', request['anchors'], result,
                        request['expected'], oracle, paths)
        archive.close()


def run_demo() -> None:
    shift = _sample('shift.json')
    manual = _sample('manual-ledger.json')
    partner = _sample('foreign-stations.json')['reverse']
    with TemporaryDirectory(prefix='film-review-') as directory:
        root = Path(directory)
        with closing(open_archive(root / 'archive.sqlite3')) as archive:
            with closing(open_source(root / 'source.sqlite3', origin=shift['origin'])) as source:
                ids = _freeze_shift(source, archive, shift)
            _show_comparison(
                archive, 'v6.reversed_selection',
                {'measurements': shift['measurements'], 'station_order': ['West', 'East']},
                [ids['West'], ids['East']], manual['cross_station'], [ids['East'], ids['West']],
                manual['oracles']['cross_station'],
                ['handoff/freeze.py', 'analysis/pool.py', 'analysis/jackknife.py', 'records/retrieval.py'],
            )
            imported = accept_station(archive, json.dumps(partner), EvidenceSource(**partner['source']))
            _show_comparison(
                archive, 'v5.aligned_pairs', partner, [imported.package_id], manual['reverse'],
                [partner['package_id']], manual['oracles']['reverse'],
                ['handoff/exchange.py', 'analysis/jackknife.py', 'records/publication.py', 'records/retrieval.py'],
            )
        _run_alignment_demo(root)


def main() -> None:
    parser = argparse.ArgumentParser(description='Paired film inspection review')
    parser.add_argument('command', choices=['demo'])
    parser.parse_args()
    run_demo()


if __name__ == '__main__':
    main()
