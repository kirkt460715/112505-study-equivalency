"""Additional invariants: batching, minimum evidence, reclass/identity fixes."""

import unittest
from pathlib import Path

from studyvisit import StudyVisitService
from studyvisit.scenario import replay_canonical

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REG = __import__("json").loads((FIXTURES / "registers.json").read_text(encoding="utf-8"))


class BatchAndAdoptionTest(unittest.TestCase):
    def setUp(self):
        self.svc = StudyVisitService(REG)

    def test_batch_accepts_good_rows_and_rejects_unauthorised_row(self):
        result = self.svc.submit_evidence_batch([
            {"evidence_id": "b-1", "student": "l-1", "partner": "site-a", "goal": "observe"},
            # site-b cannot teach observe -> this row is rejected, not the batch.
            {"evidence_id": "b-2", "student": "l-1", "partner": "site-b", "goal": "observe"},
            {"evidence_id": "b-3", "student": "l-1", "partner": "site-b", "course": "alt-art"},
        ])
        self.assertEqual(result["accepted"], 2)
        self.assertEqual(result["rejected"], 1)
        self.assertEqual(result["results"][1]["code"], "partner_not_authorized")
        # Re-running is idempotent for the accepted ids.
        again = self.svc.submit_evidence_batch([
            {"evidence_id": "b-1", "student": "l-1", "partner": "site-a", "goal": "observe"},
        ])
        self.assertFalse(again["results"][0]["created"])

    def test_school_registers_higher_minimum_evidence(self):
        self.svc.register_adoption(
            "school-18", minimum_evidence={"guide-confirm": {"count": 2}}
        )
        self.svc.submit_evidence(evidence_id="g1", student="l-1", partner="site-a", goal="observe")
        self.assertFalse(self.svc.evaluate_student("l-1")["points"]["guide-confirm"]["satisfied"])
        self.svc.submit_evidence(evidence_id="g2", student="l-1", partner="site-a", goal="observe")
        self.assertTrue(self.svc.evaluate_student("l-1")["points"]["guide-confirm"]["satisfied"])

    def test_minimum_evidence_independent_repetition(self):
        self.svc.register_adoption(
            "school-18",
            minimum_evidence={"hands-on": {"count": 2, "independent_practice": True}},
        )
        # A single action record cannot meet a two-time independent requirement.
        self.svc.submit_evidence(evidence_id="h1", student="l-1", partner="site-b",
                                 course="alt-art", supports=["hands-on"])
        point = self.svc.evaluate_student("l-1")["points"]["hands-on"]
        self.assertFalse(point["satisfied"])
        self.assertEqual(point["required_count"], 2)


class IdentityAndReclassNoDuplicationTest(unittest.TestCase):
    def test_identity_correction_moves_single_signin_never_copies(self):
        svc = replay_canonical(FIXTURES)
        self.assertEqual(svc.state["evidence"]["do-14"]["student_id"], "l-2")
        # The school corrects the name match to the other bridged candidate.
        svc.confirm_identity("guest-token-x", "l-3", matched_by="school-21", minute=18)
        do14 = svc.state["evidence"]["do-14"]
        self.assertEqual(do14["student_id"], "l-3")  # moved, single-valued
        l2_records = [r for r in svc.state["evidence"].values() if r["student_id"] == "l-2"
                      and r["id"] == "do-14"]
        self.assertEqual(l2_records, [])
        # Still exactly one do-14 record in the store.
        self.assertEqual([k for k in svc.state["evidence"] if k == "do-14"], ["do-14"])

    def test_reclass_keeps_evidence_on_one_student(self):
        svc = replay_canonical(FIXTURES)
        svc.reclass_student("l-2", "school-21")
        self.assertEqual(svc.state["student_cohort"]["l-2"], "school-21")
        self.assertEqual(svc.state["evidence"]["do-14"]["student_id"], "l-2")
        # l-3 remains evidence-free; reclass did not merge identities.
        self.assertEqual(
            [r for r in svc.state["evidence"].values() if r["student_id"] == "l-3"], []
        )


class PartnerMinimizationTest(unittest.TestCase):
    def test_exchange_only_exposes_named_subjects_and_own_records(self):
        svc = replay_canonical(FIXTURES)
        # A fresh site-a exchange about l-1 only.
        ex = svc.create_exchange(partner="site-a", subjects=["l-1"])
        view = svc.partner_exchange_view(ex["id"], "site-a")
        self.assertEqual([i["subject"] for i in view["items"]], ["l-1"])
        blob = repr(view)
        self.assertNotIn("ref-9", blob)   # school-only artifact
        self.assertNotIn("do-14", blob)   # site-b record
        self.assertNotIn("obs-18", blob)  # site-a record belongs to l-2, not l-1


if __name__ == "__main__":
    unittest.main()
