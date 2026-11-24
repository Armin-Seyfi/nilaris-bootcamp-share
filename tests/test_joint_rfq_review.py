"""Regression checks for the editable joint draft; no customer data required."""
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import time
from unittest import TestCase, mock
from zipfile import ZipFile

from nilaris import joint_rfq_review as joint


def workbook():
    raw = BytesIO()
    main = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    rel = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    with ZipFile(raw, "w") as book:
        book.writestr("xl/workbook.xml", f'<workbook xmlns="{main}" xmlns:r="{rel}"><sheets>'
                      '<sheet name="Sheet1" r:id="rId7"/></sheets></workbook>')
        book.writestr("xl/_rels/workbook.xml.rels",
                      '<Relationships><Relationship Id="rId7" Target="worksheets/third.xml"/></Relationships>')
        book.writestr("xl/sharedStrings.xml", f'<sst xmlns="{main}"><si><r><t>Test pump; </t></r>'
                      '<r><t>see datasheet</t></r></si></sst>')
        cells = ''.join(f'<c r="{col}{n}"><v>{n}</v></c>' for n in (1, 2) for col in "ABCDE"
                        if (col, n) != ("E", 2))
        book.writestr("xl/worksheets/third.xml", f'<worksheet xmlns="{main}"><sheetData><row>{cells}'
                      '<c r="E2" t="s"><v>0</v></c></row></sheetData></worksheet>')
    return raw.getvalue()


class JointDraftTests(TestCase):
    def test_verified_inputs_supply_the_observations(self):
        sources = [joint.Source("request.xlsx", "a", workbook()), joint.Source("sheet.pdf", "b", b"sheet"),
                   joint.Source("offer.pdf", "c", b"offer")]
        def evidence(_source, role):
            text = "3.1 m3/h - minimum pressure" if role == "datasheet" else "Discharge Flow: 3.7 m3/h"
            passages = [{"text": text, "bounds": [.1, .2, .3, .05]}]
            if role == "offer":
                passages.append({"text": "Sleeve Dia: 64 mm", "bounds": [.1, .4, .3, .05]})
            return dict(passages=passages, image=b"png", pageText=text, size=[600, 800])

        with mock.patch.object(joint, "load_source", side_effect=sources), \
             mock.patch.object(joint, "pdf_evidence", side_effect=evidence):
            routes = joint.build_review(Path("."), Path("."))
        draft = routes[joint.PREFIX + "/"][1].decode()
        for expectation in ("Test pump", "3.1 m³/h", "minimum pressure", "3.7 m3/h", "Sleeve diameter", "64 mm", "Save draft", "Customer RFQ", "Supplier offer", "After order and before manufacture"):
            self.assertIn(expectation, draft)
        self.assertIn('<mark id=\'E2\'>Test pump; see datasheet</mark>',
                      routes[sources[0].url + "/Sheet1/E2"][1].decode())
        for source, suffix in zip(sources, ["original.xlsx", "original.pdf", "original.pdf"]):
            self.assertEqual(routes[source.url + "/" + suffix][1], source.data)

    def test_historical_sources_disabled_before_file_access(self):
        from nilaris.case001_import import extract, main as import_main

        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("Unexpected file access")):
            for read in (joint.inspect_case, extract):
                with self.assertRaisesRegex(ValueError, joint.HISTORICAL_SOURCES_UNAVAILABLE):
                    read(Path("."))
            for command in (joint.main, import_main):
                with self.assertRaises(SystemExit) as stopped:
                    command()
                self.assertEqual(str(stopped.exception), joint.HISTORICAL_SOURCES_UNAVAILABLE)

    def test_changed_revision_refused_before_extraction(self):
        pin = ("synthetic.xlsx", sha256(b"expected").hexdigest())
        with TemporaryDirectory() as tmp, mock.patch.dict(joint.PINS, workbook=pin):
            (Path(tmp) / pin[0]).write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "revision"):
                joint.build_review(Path(tmp), Path(tmp))


    def test_offer_extraction_uses_item_labels_and_joint_source_scope(self):
        from test_fixture_extraction import selectable_pdf
        pdf = selectable_pdf([["1. SCOPE OF SUPPLY:", "# Qty ITEM DESCRIPTION", "1 1 PUMP MODEL: Example",
                               "Discharge Flow: 5 m3/h", "Sleeve Dia: 44 mm", "Shaft Diameter: 41 mm",
                               "2 1 MOTOR", "Plunger Dia: 10 mm"]])
        offer = joint.pdf_evidence(joint.Source("variant.pdf", "test", pdf), "offer")
        flow, measurements = joint.offer_measurements(offer["passages"], "Shaft Dia. 42 mm")
        self.assertEqual(flow["text"], "Discharge Flow: 5 m3/h")
        self.assertEqual([(item.name, item.value, item.passage["text"]) for item in measurements],
                         [("Sleeve diameter", "44 mm", "Sleeve Dia: 44 mm")])
        _, none = joint.offer_measurements(offer["passages"], "Sleeve diameter and shaft diameter")
        self.assertEqual(none, [])

    def test_absolute_expiry_closes_an_incomplete_request_and_removes_key(self):
        with TemporaryDirectory() as directory:
            with joint.reviewer_credential(directory) as key, joint.reviewer({}, key, 0) as server:
                with socket.create_connection(server.server_address) as client:
                    client.settimeout(2)
                    client.sendall(b"POST /rfq/session HTTP/1.1\r\n")
                    started = time.monotonic()
                    joint.serve_for(server, .2)
                    self.assertLess(time.monotonic() - started, 1.5)
                    self.assertEqual(client.recv(1), b"")
            self.assertFalse((Path(directory) / "reviewer.key").exists())


class ProcurementDecisionTests(TestCase):
    def test_combinations_reopen_in_the_same_scope(self):
        from itertools import combinations
        from nilaris.rfq_stage_decision import EXPERT, STAGES, StageDecision
        with TemporaryDirectory() as tmp:
            path = Path(tmp)
            record = StageDecision(path, "draft-scenario-attribute")
            revision = 0
            for count in range(4):
                for subset in combinations(STAGES, count):
                    record.save({"revision": revision, "stages": list(subset)})
                    revision += 1
                    reopened = StageDecision(path, "draft-scenario-attribute").read()
                    self.assertEqual(reopened, {"revision": revision, "stages": list(subset), "decided_by": EXPERT})
            self.assertIsNone(StageDecision(path, "other-scenario").read()["decided_by"])

    def test_invalid_or_stale_decision_preserves_prior_save(self):
        from nilaris.rfq_stage_decision import StageDecision, StaleDecision
        with TemporaryDirectory() as tmp:
            record = StageDecision(Path(tmp), "draft")
            saved = record.save({"revision": 0, "stages": ["supplier-offer"]})
            for patch in ({"stages": ["unknown"]}, {"stages": ["supplier-offer"] * 2},
                          {"decided_by": "somebody else"}, {"revision": True}):
                with self.assertRaises(ValueError):
                    record.save({"revision": 1, "stages": [], **patch})
            with self.assertRaises(StaleDecision):
                record.save({"revision": 0, "stages": []})
            self.assertEqual(record.read(), saved)
