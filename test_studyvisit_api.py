"""End-to-end HTTP tests for the JSON backend, including partner scoping."""

import json
import unittest
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread

from studyvisit.scenario import replay_canonical
from studyvisit.server import create_server

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = replay_canonical(FIXTURES)
        cls.server = create_server(0, cls.service)
        cls.port = cls.server.server_address[1]
        cls.thread = Thread(target=cls.server.serve_forever,
                            kwargs={"poll_interval": 0.02}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    def call(self, method, path, payload=None, headers=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            body = None if payload is None else json.dumps(payload)
            hdrs = {"Content-Type": "application/json"}
            if headers:
                hdrs.update(headers)
            conn.request(method, path, body, hdrs)
            resp = conn.getresponse()
            raw = resp.read()
            data = json.loads(raw) if raw else None
            return resp.status, data
        finally:
            conn.close()

    def test_health_and_index(self):
        status, body = self.call("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(self.call("GET", "/")[0], 200)

    def test_school_queries_issuance_basis(self):
        status, body = self.call("GET", "/api/certificates/cert-7/basis")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "in_review")
        self.assertTrue(body["basis_was_sufficient_at_issue"])
        self.assertEqual(body["current_corroboration"]["unsupported_points"],
                         ["guide-confirm"])
        self.assertEqual(self.call("GET", "/api/certificates?status=in_review")[1]["certificates"][0]["id"],
                         "cert-7")

    def test_current_verification_and_issuance_block(self):
        status, body = self.call("GET", "/api/students/l-2/verification")
        self.assertEqual(status, 200)
        self.assertEqual(body["missing"], ["guide-confirm"])

        status, body = self.call("POST", "/api/certificates",
                                 {"certificate": "cert-x", "student": "l-2",
                                  "evidence": ["do-14", "ref-9"]})
        self.assertEqual(status, 422)
        self.assertEqual(body["code"], "certification_blocked")
        self.assertIn("guide-confirm", body["details"]["missing"])

    def test_teacher_registers_next_batch_idempotently(self):
        status, body = self.call("POST", "/api/cohorts/school-18/students/batch",
                                 {"student_ids": ["l-4", "l-5"]},
                                 headers={"X-Actor-Role": "teacher"})
        self.assertEqual(status, 201)
        self.assertEqual({r["status"] for r in body["results"]}, {"enrolled"})
        # Re-posting the same batch does not create duplicates.
        status, body = self.call("POST", "/api/cohorts/school-18/students/batch",
                                 {"student_ids": ["l-4", "l-5"]})
        self.assertEqual(status, 201)
        self.assertEqual({r["status"] for r in body["results"]}, {"already_enrolled"})
        _, roster = self.call("GET", "/api/cohorts/school-18/roster")
        self.assertEqual(roster["roster"].count("l-4"), 1)

    def test_academic_adds_and_narrows_substitution(self):
        status, body = self.call("POST", "/api/academic/substitutions",
                                 {"course": "alt-draw", "supports": ["hands-on"],
                                  "cohorts": ["school-18"]},
                                 headers={"X-Actor-Role": "academic"})
        self.assertEqual(status, 201)
        status, body = self.call("PATCH", "/api/academic/substitutions/alt-art",
                                 {"change": "limited_scope", "supports": ["hands-on"]})
        self.assertEqual(status, 200)
        self.assertEqual(body["substitution"]["status"], "limited")

    def test_partner_worklist_is_row_scoped(self):
        status, body = self.call("GET", "/api/partner/worklist?partner=site-b")
        self.assertEqual(status, 200)
        blob = json.dumps(body, ensure_ascii=False)
        self.assertIn("do-14", blob)
        # site-b must not see site-a attendance or the school reflection.
        self.assertNotIn("ref-9", blob)
        self.assertNotIn("obs-18", blob)
        self.assertNotIn("obs-18", blob)
        # Only site-b records are present.
        for item in body["items"]:
            for rec in item["records"]:
                self.assertNotEqual(rec["receipt"], "ref-9")

    def test_partner_cannot_submit_unauthorised_segment(self):
        # site-b is mandated for hands-on only, not observe.
        status, body = self.call("POST", "/api/evidence",
                                 {"evidence_id": "bad-1", "student": "l-2",
                                  "partner": "site-b", "goal": "observe"})
        self.assertEqual(status, 403)
        self.assertEqual(body["code"], "partner_not_authorized")

        # site-a is allowed to record observe.
        status, body = self.call("POST", "/api/evidence",
                                 {"evidence_id": "obs-ok-1", "student": "l-1",
                                  "partner": "site-a", "goal": "observe", "minute": 50})
        self.assertEqual(status, 201)

    def test_exchange_is_isolated_to_its_partner(self):
        _, created = self.call("POST", "/api/exchanges",
                               {"partner": "site-a", "subjects": ["l-1"]})
        eid = created["id"]
        self.assertEqual(self.call("GET", f"/api/partner/exchanges/{eid}?partner=site-a")[0], 200)
        status, body = self.call("GET", f"/api/partner/exchanges/{eid}?partner=site-b")
        self.assertEqual(status, 403)
        self.assertEqual(body["code"], "partner_not_authorized")

    def test_transfer_carries_verified_parts_and_mandate(self):
        status, body = self.call("POST", "/api/transfer",
                                 {"subjects": ["l-2"], "to_partner": "site-b", "minute": 30})
        self.assertEqual(status, 200)
        passport = body["passports"][0]
        self.assertEqual(set(passport["verified_points"]),
                         {"hands-on", "separate-reflection"})
        self.assertEqual(body["authorised_segments"], ["hands-on"])

    def test_identity_endpoint_shows_single_match(self):
        status, body = self.call("GET", "/api/identity/guest-token-x")
        self.assertEqual(status, 200)
        self.assertEqual(body["matched_student"], "l-2")
        self.assertEqual(body["candidates"], ["l-2", "l-3"])

    def test_school_review_board_classifies_cert7(self):
        status, body = self.call("GET", "/api/school/review-board")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["needs_review"]), 1)
        row = body["needs_review"][0]
        self.assertEqual(row["certificate"], "cert-7")
        self.assertEqual(row["status_zh"], "复议中")
        self.assertEqual(row["unsupported_points"], ["guide-confirm"])
        self.assertEqual(set(row["still_supported_points"]),
                         {"hands-on", "separate-reflection"})
        self.assertTrue(any(a.startswith("supplementary_activity:observe")
                            for a in row["required_actions"]))

    def test_evidence_batch_endpoint_partial_acceptance(self):
        status, body = self.call("POST", "/api/evidence/batch", {"items": [
            {"evidence_id": "api-b-1", "student": "l-3", "partner": "site-a", "goal": "observe"},
            {"evidence_id": "api-b-2", "student": "l-3", "partner": "site-b", "goal": "observe"},
        ]})
        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], 1)
        self.assertEqual(body["rejected"], 1)


if __name__ == "__main__":
    unittest.main()
