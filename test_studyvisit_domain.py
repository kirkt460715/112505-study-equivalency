"""Unit tests for the pure verification / anti-double-counting rules."""

import unittest

from studyvisit.domain import (
    evaluate_evidence,
    partner_uncovered,
    scope_covers,
    effective_substitution,
)


def rec(rid, claims, action=None, minute=1, qualifies=True, status="active"):
    return {
        "id": rid,
        "claims": list(claims),
        "action_id": action or rid,
        "minute": minute,
        "qualifies": qualifies,
        "status": status,
    }


class MultiPointAndIndependentPracticeTest(unittest.TestCase):
    def test_one_record_may_prove_two_ordinary_points(self):
        policy = {"a": {"min_count": 1}, "b": {"min_count": 1}}
        verdict = evaluate_evidence([rec("e1", ["a", "b"])], ["a", "b"], policy)
        self.assertTrue(verdict["complete"])
        self.assertEqual(verdict["satisfied"], ["a", "b"])

    def test_one_action_cannot_count_twice_for_repeated_independent_practice(self):
        policy = {"drill": {"min_count": 2, "independent_practice": True}}
        once = evaluate_evidence([rec("e1", ["drill"])], ["drill"], policy)
        self.assertFalse(once["points"]["drill"]["satisfied"])
        self.assertEqual(once["points"]["drill"]["evidence"], ["e1"])
        twice = evaluate_evidence(
            [rec("e1", ["drill"]), rec("e2", ["drill"])], ["drill"], policy
        )
        self.assertTrue(twice["complete"])
        self.assertEqual(twice["points"]["drill"]["evidence"], ["e1", "e2"])

    def test_one_action_cannot_cover_two_independent_points(self):
        policy = {
            "hands-on": {"min_count": 1, "independent_practice": True},
            "solo-build": {"min_count": 1, "independent_practice": True},
        }
        verdict = evaluate_evidence([rec("do", ["hands-on", "solo-build"])],
                                    ["hands-on", "solo-build"], policy)
        # The single action is allocated to at most one independent credit.
        used = (verdict["points"]["hands-on"]["evidence"]
                + verdict["points"]["solo-build"]["evidence"])
        self.assertLessEqual(used.count("do"), 1)
        self.assertFalse(verdict["complete"])

    def test_dedicated_reflection_needs_its_own_record(self):
        policy = {
            "hands-on": {"min_count": 1},
            "separate-reflection": {
                "min_count": 1, "independent_practice": True, "dedicated_evidence": True
            },
        }
        # Sharing the one craft action is not enough for the reflection.
        shared = evaluate_evidence(
            [rec("do-14", ["hands-on", "separate-reflection"])],
            ["hands-on", "separate-reflection"], policy,
        )
        uses = (shared["points"]["hands-on"]["evidence"]
                + shared["points"]["separate-reflection"]["evidence"])
        self.assertLessEqual(uses.count("do-14"), 1)
        self.assertFalse(shared["complete"])

        # A separate reflection record makes both points hold.
        distinct = evaluate_evidence(
            [rec("do-14", ["hands-on"], minute=8),
             rec("ref-9", ["separate-reflection"], minute=16)],
            ["hands-on", "separate-reflection"], policy,
        )
        self.assertTrue(distinct["complete"])
        self.assertEqual(distinct["points"]["hands-on"]["evidence"], ["do-14"])
        self.assertEqual(distinct["points"]["separate-reflection"]["evidence"], ["ref-9"])

    def test_corrected_evidence_is_rejected(self):
        policy = {"guide-confirm": {"min_count": 1}}
        verdict = evaluate_evidence(
            [rec("obs-18", ["guide-confirm"], qualifies=False, status="superseded")],
            ["guide-confirm"], policy,
        )
        self.assertFalse(verdict["complete"])
        self.assertTrue(verdict["points"]["guide-confirm"]["rejected"])


class ScopeAndSubstitutionTest(unittest.TestCase):
    MODULES = {"observe": ["guide-confirm"], "making": ["hands-on", "separate-reflection"]}

    def test_module_scope_covers_its_confirmation_point(self):
        self.assertTrue(scope_covers("observe", "guide-confirm", self.MODULES))
        self.assertTrue(scope_covers("hands-on", "hands-on", self.MODULES))
        self.assertFalse(scope_covers("hands-on", "guide-confirm", self.MODULES))

    def test_partner_uncovered_segments(self):
        self.assertEqual(
            partner_uncovered(["observe"], ["guide-confirm"], self.MODULES), []
        )
        self.assertEqual(
            partner_uncovered(["hands-on"], ["guide-confirm"], self.MODULES),
            ["guide-confirm"],
        )

    def test_substitution_is_cohort_scoped(self):
        sub = {"course": "alt-art", "supports": ["hands-on"], "cohorts": ["school-18"],
               "effective_round": "summer", "status": "approved"}
        self.assertIsNotNone(effective_substitution(
            [sub], "alt-art", cohort="school-18", round_id="summer"))
        self.assertIsNone(effective_substitution(
            [sub], "alt-art", cohort="school-21", round_id="summer"))
        self.assertIsNone(effective_substitution(
            [sub], "alt-art", cohort="school-18", round_id="winter"))
        limited = dict(sub, status="revoked")
        self.assertIsNone(effective_substitution(
            [limited], "alt-art", cohort="school-18", round_id="summer"))


if __name__ == "__main__":
    unittest.main()
