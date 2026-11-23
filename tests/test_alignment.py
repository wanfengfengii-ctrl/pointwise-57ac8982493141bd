import json
from pathlib import Path
from tempfile import TemporaryDirectory
from fractions import Fraction

from analysis.alignment_issue import issue_alignment, read_alignment
from analysis.alignment_scale import pooled_auc
from handoff.alignment_package import decode_alignment, encode_alignment
from review import (
    AlignmentCatalog, BoardTicket, EvidenceSource, ModelPair, accept_alignment,
    admit_board, freeze_alignment, open_archive, open_source, record_pair,
)
from storage import (
    InvalidEvidence, InvalidInput, InvalidSelection, MissingItem,
    PublicationFailed,
)
from tests.support import ReviewTest, sample, scenario


MODELS = ModelPair('Line-A', 'Line-B')


class AlignmentCase:
    """Each sample case owns an isolated source and archive."""

    def __init__(self, test, case, model):
        self.test = test
        self.case = case
        self.model = model
        self.revisions = {}
        self.frozen = {}
        self.directory = TemporaryDirectory(prefix='alignment-case-')
        test.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.source = open_source(root / 'source.sqlite3', origin='test-center')
        self.archive = open_archive(root / 'archive.sqlite3')
        test.addCleanup(self.source.close)
        test.addCleanup(self.archive.close)
        for station, rows in case['measurements'].items():
            latest = None
            for ticket, occurrence, label, left, right in rows:
                admit_board(self.source, BoardTicket(ticket, f'film-{ticket}'), station)
                latest = record_pair(
                    self.source, station, ticket, occurrence, label, MODELS,
                    {MODELS.first: left, MODELS.second: right},
                )
            self.revisions[station] = latest.revision

    @staticmethod
    def _key(anchors):
        return json.dumps(anchors, sort_keys=True, separators=(',', ':'))

    def freeze(self, station, anchors, *, accept=True):
        text = freeze_alignment(self.source, station, self.revisions[station],
                                self.model, anchors)
        evidence = decode_alignment(text)
        if accept:
            accepted = accept_alignment(
                self.archive, text,
                EvidenceSource(evidence.source.origin, evidence.source.station,
                               evidence.source.revision),
            )
            self.frozen[(station, self._key(anchors))] = accepted
        return evidence, text

    def ids_for(self, request):
        ids = []
        for station, anchors in request['anchors'].items():
            key = (station, self._key(anchors))
            if key not in self.frozen:
                self.freeze(station, anchors)
            ids.append(self.frozen[key].package_id)
        return ids

    def reopen_archive(self):
        self.archive.close()
        self.archive = open_archive(Path(self.directory.name) / 'archive.sqlite3')


class AlignmentFreezeTests(ReviewTest):
    def setUp(self):
        super().setUp()
        document = sample('reference-alignment.json')
        self.fixture = AlignmentCase(self, document['cases'][0], document['model'])
        self.case = self.fixture.case

    def test_freeze_payload_shapes_and_reference_exclusion(self):
        east, east_text = self.fixture.freeze('East', {'R': 'e-low'})
        west, west_text = self.fixture.freeze('West', {'R': 'w-ref'})
        parsed_east = json.loads(east_text)
        self.assertEqual(parsed_east['version'], 2)
        self.assertEqual(parsed_east['source'],
                         {'origin': 'test-center', 'station': 'East',
                          'revision': str(self.fixture.revisions['East'])})
        self.assertEqual(parsed_east['model'], 'Line-A')
        self.assertEqual(parsed_east['levels'], [[0, 0], [2, 0]])
        self.assertEqual(parsed_east['anchors'],
                         {'R': {'level': 0, 'reading_revision': '3'}})
        self.assertEqual(json.loads(west_text)['levels'], [[0, 2]])
        # All occurrences of the reference board leave production, including its other reading.
        self.assertEqual(sum(p for p, _ in east.levels), 2)
        self.assertEqual(sum(n for _, n in west.levels), 2)
        # Canonical JSON is independent of whitespace and object key order.
        rewritten = json.dumps(json.loads(east_text), indent=2)
        self.assertEqual(encode_alignment(decode_alignment(rewritten)), east_text)
        scenario('a1.reference_exclusion', {'case': self.case['name'], 'anchors': {'R': 'e-low'}},
                 parsed_east['levels'], parsed_east['levels'],
                 [0, 0], [p for p, _ in east.levels],
                 ['handoff/alignment_freeze.py', 'handoff/alignment_package.py'],
                 'Reference board R leaves production entirely; only P and Q positives remain.')

    def test_equivalent_freeze_keeps_id_and_boundary_revision(self):
        first, _ = self.fixture.freeze('East', {'R': 'e-low'})
        second, _ = self.fixture.freeze('East', {'R': 'e-low'})
        self.assertEqual(first.package_id, second.package_id)
        # A different reference choice on the same score revision gets its own id.
        other, _ = self.fixture.freeze('East', {'R': 'e-high'})
        self.assertNotEqual(first.package_id, other.package_id)
        high = decode_alignment(freeze_alignment(
            self.fixture.source, 'East', self.fixture.revisions['East'],
            'Line-A', {'R': 'e-high'}))
        self.assertEqual(high.anchor_map()['R'].reading_revision, '4')

    def test_freeze_rejects_bad_inputs(self):
        revision = self.fixture.revisions['East']
        source = self.fixture.source
        with self.assertRaises(MissingItem):
            freeze_alignment(source, 'East', revision + 100, 'Line-A', {'R': 'e-low'})
        with self.assertRaises(InvalidInput):
            freeze_alignment(source, 'East', revision, 'Line-Z', {'R': 'e-low'})
        with self.assertRaises(InvalidInput):
            freeze_alignment(source, 'East', revision, 'Line-A', {})
        with self.assertRaises(InvalidInput):
            freeze_alignment(source, 'East', revision, 'Line-A', {'R': 'missing-occurrence'})
        with self.assertRaises(InvalidInput):
            freeze_alignment(source, 'East', revision, 'Line-A', {'Q': 'e-low'})
        with self.assertRaises(InvalidInput):
            freeze_alignment(source, 'East', str(revision), 'Line-A', {'R': 'e-low'})

    def test_all_occurrences_of_reference_board_leave_production(self):
        from review import Correction
        # A corrected reading on R stays part of the same reference board; every
        # R occurrence must disappear from production, not only the anchor one.
        corrected = record_pair(
            self.fixture.source, 'East', 'R', 'e-low', 'positive', MODELS,
            {'Line-A': 1, 'Line-B': 1}, correction=Correction('recheck-r'))
        text = freeze_alignment(
            self.fixture.source, 'East', corrected.revision, 'Line-A', {'R': 'e-low'})
        evidence = decode_alignment(text)
        self.assertEqual(evidence.anchor_map()['R'].reading_revision, str(corrected.revision))
        self.assertEqual(evidence.levels, ((0, 0), (2, 0)))
        self.assertEqual(sum(p + n for p, n in evidence.levels), 2)

    def test_anchor_only_grade_is_allowed_and_strict_decoding_rejects_zero_levels(self):
        document = sample('reference-alignment.json')
        case = next(item for item in document['cases']
                    if item['name'] == 'no-production-negative')
        fixture = AlignmentCase(self, case, document['model'])
        evidence, _ = fixture.freeze('East', {'R': 'e-r'})
        self.assertEqual(evidence.levels, ((0, 0),))
        parsed = json.loads(encode_alignment(evidence))
        parsed['levels'] = [[0, 0], [0, 0]]
        parsed['anchors']['R']['level'] = 0
        with self.assertRaises(InvalidEvidence):
            accept_alignment(fixture.archive, json.dumps(parsed),
                             EvidenceSource(**evidence.source.__dict__))


class AlignmentAcceptanceTests(ReviewTest):
    def setUp(self):
        super().setUp()
        document = sample('reference-alignment.json')
        self.fixture = AlignmentCase(self, document['cases'][0], document['model'])
        self.archive = self.fixture.archive
        self.evidence, self.text = self.fixture.freeze('East', {'R': 'e-low'}, accept=False)
        self.declared = EvidenceSource(self.evidence.source.origin, 'East',
                                       self.evidence.source.revision)

    def test_declared_source_must_match(self):
        with self.assertRaises(InvalidEvidence):
            accept_alignment(self.archive, self.text,
                             EvidenceSource('wrong-origin', 'East', self.evidence.source.revision))
        with self.assertRaises(MissingItem):
            AlignmentCatalog(self.archive).read(self.evidence.package_id)
        accepted = accept_alignment(self.archive, self.text, self.declared)
        self.assertEqual(accepted, self.evidence)
        self.assertEqual(AlignmentCatalog(self.archive).read(accepted.package_id), accepted)

    def test_equivalent_replay_keeps_first_id_but_foreign_text_conflicts(self):
        accepted = accept_alignment(self.archive, self.text, self.declared)
        document = json.loads(self.text)
        document['anchors'] = dict(reversed(list(document['anchors'].items())))
        replayed = accept_alignment(self.archive, json.dumps(document), self.declared)
        self.assertEqual(replayed.package_id, accepted.package_id)
        # Same binding under another id still resolves to the original.
        document['package_id'] = 'another-name'
        self.assertEqual(accept_alignment(
            self.archive, json.dumps(document), self.declared), accepted)
        # Same binding with different text is rejected.
        document['levels'][1] = [3, 0]
        with self.assertRaises(InvalidEvidence):
            accept_alignment(self.archive, json.dumps(document), self.declared)
        # An occupied id carrying different text is rejected.
        document['package_id'] = accepted.package_id
        with self.assertRaises(InvalidEvidence):
            accept_alignment(self.archive, json.dumps(document), self.declared)
        self.assertEqual(
            self.archive.execute('SELECT COUNT(*) FROM alignment_packages').fetchone()[0], 1)

    def test_bad_payloads_are_rejected_without_storage(self):
        variants = []
        broken = json.loads(self.text)
        broken['version'] = 1
        variants.append(json.dumps(broken))
        broken = json.loads(self.text)
        broken['reading'] = broken.pop('model')
        variants.append(json.dumps(broken))
        broken = json.loads(self.text)
        broken['levels'][0] = [0, -1]
        variants.append(json.dumps(broken))
        broken = json.loads(self.text)
        broken['source']['revision'] = '0'
        variants.append(json.dumps(broken))
        broken = json.loads(self.text)
        broken['anchors']['R']['reading_revision'] = str(int(broken['source']['revision']) + 1)
        variants.append(json.dumps(broken))
        for payload in variants:
            with self.subTest(payload=payload), self.assertRaises(InvalidEvidence):
                accept_alignment(self.archive, payload, self.declared)
        with self.assertRaises(InvalidEvidence):
            accept_alignment(self.archive, '{not json', self.declared)
        self.assertEqual(
            self.archive.execute('SELECT COUNT(*) FROM alignment_packages').fetchone()[0], 0)


class AlignmentIssueTests(ReviewTest):
    def _run_case(self, case_name):
        document = sample('reference-alignment.json')
        case = next(item for item in document['cases'] if item['name'] == case_name)
        return AlignmentCase(self, case, document['model']), case

    def test_sample_requests_match_expected_aucs_and_counts(self):
        document = sample('reference-alignment.json')
        for case in document['cases']:
            fixture = AlignmentCase(self, case, document['model'])
            for request in case['requests']:
                ids, packages = [], []
                for station, anchors in request['anchors'].items():
                    evidence, _ = fixture.freeze(station, anchors)
                    ids.append(evidence.package_id)
                    packages.append(evidence)
                result = issue_alignment(fixture.archive, ids)
                expected = request['expected']
                self.assertEqual(result['status'], expected['status'], case['name'])
                if result['status'] != 'issued':
                    self.assertEqual(
                        fixture.archive.execute(
                            'SELECT COUNT(*) FROM alignment_reports').fetchone()[0], 0)
                    continue
                self.assertEqual([result['min']['auc'], result['max']['auc']], expected['auc'])
                positives = sum(p for ev in packages for p, _ in ev.levels)
                negatives = sum(n for ev in packages for _, n in ev.levels)
                self.assertEqual([positives, negatives], expected['production_counts'])
                for bound in ('min', 'max'):
                    self.assertEqual(
                        str(pooled_auc(tuple(packages), result[bound]['scale'])),
                        result[bound]['auc'])
                scenario(f'a2.{case["name"]}', request['anchors'], expected,
                         {'status': result['status'],
                          'auc': [result['min']['auc'], result['max']['auc']]},
                         expected['production_counts'], [positives, negatives],
                         ['analysis/alignment_scale.py', 'analysis/alignment_issue.py'],
                         f'{case["name"]} endpoint scales reproduce the pooled AUC.')

    def test_scale_preserves_strict_grades_anchor_ties_and_rational_text(self):
        fixture, case = self._run_case('exclusive-wins')
        ids = fixture.ids_for(case['requests'][0])
        packages = [AlignmentCatalog(fixture.archive).read(package_id) for package_id in ids]
        result = issue_alignment(fixture.archive, ids)
        for bound in ('min', 'max'):
            for package in packages:
                values = result[bound]['scale'][package.source.station]
                self.assertEqual(len(values), len(package.levels))
                self.assertTrue(all(Fraction(values[i]) < Fraction(values[i + 1])
                                    for i in range(len(values) - 1)))
            anchor_levels = [package.anchor_map()['R'].level for package in packages]
            stations = [package.source.station for package in packages]
            self.assertEqual(result[bound]['scale'][stations[0]][anchor_levels[0]],
                             result[bound]['scale'][stations[1]][anchor_levels[1]])
        self.assertTrue(all(
            isinstance(value, str) and value
            for bound in ('min', 'max')
            for values in result[bound]['scale'].values() for value in values))

    def test_incompatible_and_insufficient_have_no_publication(self):
        for name, status in (('inconsistent-anchor-tie', 'incompatible'),
                            ('no-production-negative', 'insufficient')):
            fixture, case = self._run_case(name)
            ids = fixture.ids_for(case['requests'][0])
            self.assertEqual(issue_alignment(fixture.archive, ids), {'status': status})
            for table in ('alignment_reports', 'alignment_sources', 'alignment_receipts'):
                self.assertEqual(
                    fixture.archive.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)

    def test_invalid_selections(self):
        fixture, case = self._run_case('reference-choice')
        ids = fixture.ids_for(case['requests'][0])
        archive = fixture.archive
        # A V1 package id is unknown to the V2 catalog and must be rejected.
        v1 = self.accept_partner('aligned')
        for selection in ([], [ids[0]], [ids[0], ids[0]], [ids[0], 'missing-package'],
                          [ids[0], v1.package_id], 'not-a-collection',
                          [ids[0], ids[1], 'missing-package']):
            with self.subTest(selection=selection), self.assertRaises(InvalidSelection):
                issue_alignment(archive, selection)
        # Two packages from the same station, and differing reference ticket sets.
        east = AlignmentCatalog(archive).read(ids[0])
        duplicate = type(east)(f'{east.package_id}-copy', east.source, east.model,
                               east.levels, east.anchors)
        AlignmentCatalog(archive).save(duplicate)
        with self.assertRaises(InvalidSelection):
            issue_alignment(archive, [east.package_id, duplicate.package_id])
        # Models differ: build a second case and borrow one of its V2 packages.
        other, other_case = self._run_case('exclusive-wins')
        other_ids = other.ids_for(other_case['requests'][0])
        with self.assertRaises(InvalidSelection):
            issue_alignment(archive, [ids[0], other_ids[1]])

    def test_failed_commit_leaves_no_alignment_report(self):
        fixture, case = self._run_case('reference-choice')
        ids = fixture.ids_for(case['requests'][0])
        archive = fixture.archive
        archive.execute('CREATE TABLE allowed_links (report_id INTEGER PRIMARY KEY)')
        archive.execute('''
            CREATE TABLE pending_links (
                report_id INTEGER REFERENCES allowed_links(report_id)
                DEFERRABLE INITIALLY DEFERRED)
        ''')
        archive.execute('''
            CREATE TRIGGER alignment_commit_check AFTER INSERT ON alignment_receipts
            BEGIN
                INSERT INTO pending_links(report_id) VALUES (NEW.report_id);
            END
        ''')
        with self.assertRaises(PublicationFailed):
            issue_alignment(archive, ids)
        self.assertFalse(archive.in_transaction)
        for table in ('alignment_reports', 'alignment_sources', 'alignment_receipts'):
            self.assertEqual(archive.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)
        archive.execute('DROP TRIGGER alignment_commit_check')
        self.assertEqual(issue_alignment(archive, ids)['status'], 'issued')

    def test_reopen_after_source_closed_reads_original_id_and_export(self):
        fixture, case = self._run_case('reference-choice')
        first = issue_alignment(fixture.archive, fixture.ids_for(case['requests'][0]))
        second = issue_alignment(fixture.archive, fixture.ids_for(case['requests'][1]))
        fixture.source.close()
        fixture.reopen_archive()
        archive = fixture.archive
        report_id = first['report_id']
        received = read_alignment(archive, report_id)
        self.assertEqual(received, first)
        exported = read_alignment(archive, report_id, as_json=True)
        self.assertEqual(json.loads(exported), first)
        self.assertEqual(json.dumps(first, sort_keys=True, separators=(',', ':')), exported)
        self.assertEqual(
            read_alignment(archive, second['report_id'])['report_id'], second['report_id'])
        with self.assertRaises(MissingItem):
            read_alignment(archive, 999)
        with self.assertRaises(InvalidInput):
            read_alignment(archive, '1')
        scenario('a3.reopen_claim', {'report_id': report_id}, first, received,
                 exported, exported,
                 ['analysis/alignment_issue.py', 'records persistence'],
                 'After the source is closed and the archive reopened, the original id and values remain.')

    def test_v1_package_and_wrong_station_cannot_enter_v2_issue(self):
        fixture, case = self._run_case('reference-choice')
        ids = fixture.ids_for(case['requests'][0])
        # A V1 station package id is unknown to the V2 catalog.
        v1 = self.accept_partner('aligned')
        self.assertNotIn(v1.package_id, ids)
        with self.assertRaises(InvalidSelection):
            issue_alignment(fixture.archive, [ids[0], v1.package_id])
        # Both packages from one station.
        east = AlignmentCatalog(fixture.archive).read(ids[0])
        duplicate = type(east)(f'{east.package_id}-dup', east.source, east.model,
                               east.levels, east.anchors)
        AlignmentCatalog(fixture.archive).save(duplicate)
        with self.assertRaises(InvalidSelection):
            issue_alignment(fixture.archive, [east.package_id, duplicate.package_id])
        # No report was written by any rejected selection.
        self.assertEqual(
            fixture.archive.execute('SELECT COUNT(*) FROM alignment_reports').fetchone()[0], 0)

    def test_issued_report_round_trips_through_canonical_json(self):
        fixture, case = self._run_case('exclusive-wins')
        result = issue_alignment(fixture.archive, fixture.ids_for(case['requests'][0]))
        exported = read_alignment(fixture.archive, result['report_id'], as_json=True)
        again = read_alignment(fixture.archive, result['report_id'], as_json=True)
        self.assertEqual(exported, again)
        self.assertEqual(json.loads(exported), result)
        # Sources are station-sorted and carry the actual received identities.
        stations = [source['station'] for source in result['sources']]
        self.assertEqual(stations, sorted(stations))
        self.assertEqual(len(result['receipt']['receipt_id']),
                         len('alignment-receipt-') + len(str(result['report_id'])))
