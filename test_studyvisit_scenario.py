"""Tests for the exact synthetic scenario and the receipt protocol."""

import json
import tempfile
import unittest
from http.client import RemoteDisconnected
from pathlib import Path
from threading import Thread

from fixture_gateway import FixtureGateway
from studyvisit import StudyVisitService
from studyvisit.gateway_client import GatewayClient
from studyvisit.scenario import CERT_REFERENCE, build_report, replay_canonical

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class CanonicalFixtureScenarioTest(unittest.TestCase):
    def setUp(self):
        self.svc = replay_canonical(FIXTURES)
        self.report = build_report(self.svc)

    def test_every_student_verdict(self):
        students = self.report["students"]
        self.assertEqual(students["l-1"]["status"], "incomplete")
        self.assertEqual(students["l-1"]["missing"],
                         ["guide-confirm", "hands-on", "separate-reflection"])

        l2 = students["l-2"]
        self.assertEqual(l2["status"], "incomplete")
        self.assertEqual(l2["satisfied"], ["hands-on", "separate-reflection"])
        self.assertEqual(l2["missing"], ["guide-confirm"])
        self.assertTrue(l2["modules"]["making"]["complete"])
        self.assertFalse(l2["modules"]["observe"]["complete"])

        # The ambiguous bridge must not give l-3 the shared sign-in.
        l3 = students["l-3"]
        self.assertEqual(l3["status"], "incomplete")
        self.assertEqual(l3["satisfied"], [])
        self.assertEqual(l3["missing"],
                         ["guide-confirm", "hands-on", "separate-reflection"])

    def test_one_signin_attributed_once_to_the_confirmed_student(self):
        view = self.svc.identity_view("guest-token-x")
        self.assertEqual(view["candidates"], ["l-2", "l-3"])
        self.assertEqual(view["matched_student"], "l-2")
        self.assertEqual(view["receipts"], ["do-14"])
        do14 = self.svc.state["evidence"]["do-14"]
        self.assertEqual(do14["student_id"], "l-2")
        # l-3 has no evidence at all.
        self.assertEqual(
            [r for r in self.svc.state["evidence"].values() if r["student_id"] == "l-3"],
            [],
        )

    def test_issued_cert_keeps_frozen_basis_but_enters_review(self):
        basis = self.svc.certificate_basis("cert-7")
        self.assertEqual(basis["status"], "in_review")
        self.assertEqual(basis["adopted_basis_evidence"], ["obs-18", "do-14", "ref-9"])
        self.assertTrue(basis["basis_was_sufficient_at_issue"])
        # Frozen snapshot still shows the pre-correction obs-18 as qualifying.
        self.assertTrue(basis["frozen_basis"]["evidence"]["obs-18"]["qualifies"])
        self.assertTrue(basis["frozen_basis"]["evaluation"]["complete"])

        corr = basis["current_corroboration"]
        self.assertEqual(corr["unsupported_points"], ["guide-confirm"])
        self.assertEqual(set(corr["still_supported_points"]),
                         {"hands-on", "separate-reflection"})
        self.assertIn("observe", corr["recommended_action"])

    def test_late_fixture_receipt_is_bound_to_original_decision(self):
        basis = self.svc.certificate_basis("cert-7")
        self.assertEqual(len(basis["receipts"]), 1)
        receipt = basis["receipts"][0]
        self.assertTrue(receipt["late"])
        self.assertTrue(receipt["receipt"]["acknowledged"])
        self.assertEqual(receipt["bound_to_decision"], "cert-7#issued@17")
        # The late acknowledgement must not restore a certificate in review.
        self.assertEqual(basis["status"], "in_review")

    def test_new_issuance_is_blocked_on_current_evidence(self):
        from studyvisit.errors import CertificationBlocked

        with self.assertRaises(CertificationBlocked) as ctx:
            self.svc.issue_certificate("cert-new", "l-2", ["do-14", "ref-9"])
        self.assertEqual(ctx.exception.details["missing"], ["guide-confirm"])

    def test_supplementary_activity_then_reaffirm(self):
        # A new valid observation confirmation for l-2.
        self.svc.submit_evidence(
            evidence_id="obs-19", student="l-2", partner="site-a",
            source="attendance", minute=40, goal="observe",
        )
        verdict = self.svc.evaluate_student("l-2")
        self.assertTrue(verdict["complete"], verdict["missing"])
        assessed = self.svc.assess_certificate("cert-7", persist=True)
        self.assertEqual(assessed["unsupported_points"], [])
        self.assertTrue(assessed["can_reaffirm"])
        # Evidence alone does not leave review; the school decision does.
        self.assertEqual(self.svc.state["certificates"]["cert-7"]["status"], "in_review")
        cert = self.svc.reaffirm_certificate("cert-7", minute=41)
        self.assertEqual(cert["status"], "reaffirmed")

    def test_narrowing_substitution_changes_current_but_not_frozen_basis(self):
        result = self.svc.adjust_substitution("alt-art", supports=[], minute=30)
        self.assertIn("cert-7", [a["certificate"] for a in result["reassessed"]])
        basis = self.svc.certificate_basis("cert-7")
        # Current corroboration now also loses hands-on ...
        self.assertIn("hands-on", basis["current_corroboration"]["unsupported_points"])
        # ... while the immutable issuance snapshot still records sufficiency.
        self.assertTrue(basis["frozen_basis"]["sufficient_at_issue"])
        self.assertIn("hands-on", basis["frozen_basis"]["evaluation"]["satisfied"])

    def test_retroactive_substitution_helps_unfinished_student_only(self):
        # l-1 is school-18 but has no records; add evidence under a newly
        # approved equivalent and confirm it counts for current verification.
        self.svc.add_substitution(
            "craft-x", ["hands-on"], cohorts=["school-18"], retroactive=True,
            effective_from_minute=0,
        )
        self.svc.submit_evidence(
            evidence_id="do-x", student="l-1", partner="site-b",
            course="craft-x", minute=12,
        )
        verdict = self.svc.evaluate_student("l-1")
        self.assertIn("hands-on", verdict["satisfied"])
        self.assertIn("separate-reflection", verdict["missing"])

    def test_persistence_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            self.svc.save(path)
            restored = StudyVisitService.load(path)
            self.assertEqual(
                restored.certificate_basis("cert-7")["status"], "in_review"
            )
            self.assertTrue(restored.evaluate_student("l-2")["missing"] == ["guide-confirm"])


class RedundantCorrectionTest(unittest.TestCase):
    def test_cert_stands_when_corrected_proof_has_independent_backup(self):
        reg = json.loads((FIXTURES / "registers.json").read_text(encoding="utf-8"))
        svc = StudyVisitService(reg)
        for eid, minute in (("obs-a", 4), ("obs-b", 6)):
            svc.submit_evidence(evidence_id=eid, student="l-1", partner="site-a",
                                minute=minute, goal="observe")
        svc.submit_evidence(evidence_id="do-1", student="l-1", partner="site-b",
                            minute=8, course="alt-art")
        svc.submit_evidence(evidence_id="ref-1", student="l-1", partner="school",
                            minute=9, goal="separate-reflection")
        cert = svc.issue_certificate("cert-dup", "l-1",
                                     ["obs-a", "obs-b", "do-1", "ref-1"], issued_at=10)
        self.assertEqual(cert["certificate"]["status"], "issued")
        out = svc.apply_correction(supersedes="obs-a", actual="departed_early", minute=20)
        assessment = out["affected_certificates"][0]
        self.assertEqual(assessment["unsupported_points"], [])
        self.assertEqual(assessment["status"], "issued")  # obs-b independently backs it


class ReceiptGatewayIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.gw = FixtureGateway(0, FIXTURES)
        self.addCleanup(self.gw.server_close)
        self.thread = Thread(target=self.gw.serve_forever,
                             kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.gw.shutdown)
        self.client = GatewayClient(f"http://127.0.0.1:{self.gw.server_port}")
        self.svc = replay_canonical(FIXTURES, bind_receipt=False)

    def test_lost_reply_is_recoverable_and_late_ack_does_not_restore(self):
        # First attempt: stub drops the connection but records the reference.
        with self.assertRaises(RemoteDisconnected):
            self.client.post_receipt_once(CERT_REFERENCE, "publish-certificate", "school-18")
        recovered = self.client.get_receipt(CERT_REFERENCE)
        self.assertTrue(recovered["receipt"]["acknowledged"])
        self.assertEqual(recovered["receipt"]["certificate"], "cert-7")

        # High-level helper reports the recovery path.
        result = self.client.publish_with_recovery(
            # fresh reference for the helper-level assertion
            "cert-7-publish-certificate-school-18-r2",
            "publish-certificate", "school-18",
        )
        self.assertEqual(result["recovery"], "lookup_after_lost_reply")
        self.assertTrue(result["receipt"]["acknowledged"])

        # Binding the late recovered receipt cannot re-enable the certificate.
        bound = self.svc.bind_receipt(
            "cert-7", reference=CERT_REFERENCE, operation="publish-certificate",
            recipient="school-18", receipt=recovered["receipt"], late=True,
        )
        self.assertEqual(bound["certificate_status"], "in_review")
        self.assertEqual(self.svc.certificate_basis("cert-7")["status"], "in_review")

    def test_reference_reuse_for_other_operation_conflicts(self):
        with self.assertRaises(RemoteDisconnected):
            self.client.post_receipt_once("ref-x", "publish-certificate", "school-18")
        # Same reference, different recipient/signature -> 409.
        from studyvisit.errors import ReferenceReused

        with self.assertRaises(ReferenceReused):
            self.client.publish_with_recovery("ref-x", "publish-certificate", "school-21")

    def test_sync_register_and_paged_events_over_http(self):
        from studyvisit.scenario import sync_from_gateway

        svc = sync_from_gateway(self.client)
        self.assertEqual(svc.certificate_basis("cert-7")["status"], "in_review")
        self.assertEqual(svc.evaluate_student("l-2")["missing"], ["guide-confirm"])
        self.assertEqual(svc.identity_view("guest-token-x")["matched_student"], "l-2")


if __name__ == "__main__":
    unittest.main()
