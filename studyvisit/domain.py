"""Pure policy rules for cross-village study verification.

Nothing in here performs I/O.  The stateful facade in :mod:`studyvisit.service`
stores records and calls these functions.  Keeping the rules pure makes the
business invariants directly unit-testable.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

# A correction whose ``actual`` is one of these means the learner did not in
# fact complete the segment, so the receipt cannot be used as credit.
NON_QUALIFYING_ACTUALS: Set[str] = {
    "departed_early",
    "left_early",
    "absent",
    "did_not_attend",
    "did_not_participate",
    "withdrew",
    "half_day_only",
}

# Policy metadata for the seeded program.  The register only lists the adopted
# capability points; the "separate reflection" point is, by definition of the
# programme, an independently produced, dedicated piece of work.  Deployments
# register their own flags through the adoption API, so this is only the seed.
DEFAULT_POINT_POLICY: Dict[str, Dict[str, Any]] = {
    "guide-confirm": {
        "label": "讲解点带队指导确认",
        "evidence_kinds": ["guide_confirmation", "attendance_confirmation"],
    },
    "hands-on": {
        "label": "在地手艺动手实践",
        "evidence_kinds": ["practice"],
    },
    "separate-reflection": {
        "label": "独立反思（须单独完成，不能与一次操作抵两次）",
        "independent_practice": True,
        "dedicated_evidence": True,
        "evidence_kinds": ["reflection"],
    },
}


def build_adoption(register: Dict[str, Any]) -> Dict[str, Any]:
    """Derive the cohort adoption/catalog from a fixture register."""
    goals = register.get("goals", {})
    modules: Dict[str, List[str]] = {mid: list(points) for mid, points in goals.items()}
    all_points = [p for points in modules.values() for p in points]
    point_policy: Dict[str, Dict[str, Any]] = {}
    for pid in all_points:
        point_policy[pid] = dict(DEFAULT_POINT_POLICY.get(pid, {}))
        point_policy[pid].setdefault("min_count", 1)
        point_policy[pid].setdefault("independent_practice", False)
        point_policy[pid].setdefault("dedicated_evidence", False)
    return {
        "program": register.get("program"),
        "modules": modules,
        "point_policy": point_policy,
    }


def module_of_point(modules: Dict[str, List[str]], point: str) -> Optional[str]:
    for mid, points in modules.items():
        if point == mid or point in points:
            return mid
    return None


def expand_goal(modules: Dict[str, List[str]], goal: str) -> List[str]:
    """A raw event goal may be a module id (``observe``) or a point id."""
    if goal in modules:
        return list(modules[goal])
    for points in modules.values():
        if goal in points:
            return [goal]
    return []


def scope_covers(scope: str, point: str, modules: Dict[str, List[str]]) -> bool:
    """A partner scope covers a point if it names the point or its module."""
    if scope == point:
        return True
    if scope in modules and point in modules[scope]:
        return True
    # scope names a module and point is that module id itself
    if scope == point:  # pragma: no cover - clarity
        return True
    return False


def partner_uncovered(
    partner_scopes: Iterable[str], points: Iterable[str], modules: Dict[str, List[str]]
) -> List[str]:
    scopes = list(partner_scopes or [])
    uncovered = []
    for point in points:
        if not any(scope_covers(s, point, modules) for s in scopes):
            uncovered.append(point)
    return uncovered


def substitution_applies(
    sub: Dict[str, Any],
    *,
    cohort: Optional[str],
    round_id: Optional[str] = None,
    minute: Optional[int] = None,
) -> bool:
    if sub.get("status") == "revoked":
        return False
    cohorts = sub.get("cohorts")
    if cohorts not in (None, "*"):
        if cohort is None or cohort not in cohorts:
            return False
    if round_id is not None and sub.get("effective_round") not in (None, "*", round_id):
        return False
    start = sub.get("effective_from_minute")
    if minute is not None and start is not None and minute < start:
        return False
    return True


def effective_substitution(
    substitutions: Iterable[Dict[str, Any]],
    course: str,
    *,
    cohort: Optional[str],
    round_id: Optional[str] = None,
    minute: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Return the effective approval for a course in a cohort.

    Applicability (cohort / round / effective minute / status) is filtered
    first; an exact cohort-scoped approval then wins over a wildcard, and among
    equal specificity the latest decision wins.
    """
    applicable = [
        s
        for s in substitutions
        if s.get("course") == course
        and substitution_applies(s, cohort=cohort, round_id=round_id, minute=minute)
    ]
    if not applicable:
        return None

    def decided(s):
        return s.get("decided_minute") if s.get("decided_minute") is not None else -1

    exact = [s for s in applicable if cohort in (s.get("cohorts") or [])]
    if exact:
        return sorted(exact, key=decided)[-1]
    wildcard = [s for s in applicable if s.get("cohorts") in (None, "*")]
    pool = wildcard or applicable
    return sorted(pool, key=decided)[-1]


def _eligible_candidates(
    records: Iterable[Dict[str, Any]],
    point: str,
    allowed_ids: Optional[Set[str]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    candidates: List[Dict[str, Any]] = []
    rejected: List[Dict[str, str]] = []
    for rec in records:
        rid = rec.get("id")
        if allowed_ids is not None and rid not in allowed_ids:
            continue
        if point not in (rec.get("claims") or []):
            continue
        if rec.get("status") == "superseded" or not rec.get("qualifies", True):
            rejected.append({"id": rid, "reason": rec.get("disqualify_reason") or "corrected"})
            continue
        candidates.append(rec)
    candidates.sort(key=lambda r: (r.get("minute") if r.get("minute") is not None else 10**9, r.get("id") or ""))
    return candidates, rejected


def _assign_all(
    slots: List[Tuple[str, int]],
    candidates: Dict[str, List[Dict[str, Any]]],
    policy: Dict[str, Dict[str, Any]],
) -> Optional[Dict[str, List[str]]]:
    """Backtracking assignment.

    Constraints implemented:
      * within one point, ``min_count`` units need distinct actions;
      * independent-practice actions cannot do duty for two independent credits;
      * dedicated evidence is exclusive to its point.

    Slot order is dedicated points first, then independent, then ordinary so
    that exclusivity locks are established before shared use is considered.
    Returns ``{point: [record ids]}`` for a fully feasible assignment or None.
    """
    assignment: Dict[str, List[str]] = {p: [] for p, _ in slots}
    ind_used: Set[str] = set()
    dedicated_owner: Dict[str, str] = {}

    # We track chosen record objects directly for clarity.
    chosen_records: Dict[str, List[Dict[str, Any]]] = {p: [] for p, _ in slots}

    def action_of(rec):
        return rec.get("action_id") or rec.get("id")

    def is_legal(point: str, rec: Dict[str, Any]) -> bool:
        act = action_of(rec)
        pol = policy.get(point, {})
        if any(action_of(r) == act for r in chosen_records[point]):
            return False  # one action cannot be counted twice for same requirement
        owner = dedicated_owner.get(act)
        if owner is not None and owner != point:
            return False  # action is dedicated to a different point
        if pol.get("dedicated_evidence"):
            # must not already be used by any other point
            for other, recs in chosen_records.items():
                if other != point and any(action_of(r) == act for r in recs):
                    return False
        if pol.get("independent_practice") and not pol.get("dedicated_evidence"):
            # cannot let one action cover two independent practice credits
            for other, recs in chosen_records.items():
                if other == point:
                    continue
                if policy.get(other, {}).get("independent_practice") and any(
                    action_of(r) == act for r in recs
                ):
                    return False
        return True

    def reserve(point: str, rec: Dict[str, Any]):
        act = action_of(rec)
        chosen_records[point].append(rec)
        pol = policy.get(point, {})
        if pol.get("dedicated_evidence"):
            dedicated_owner[act] = point
        if pol.get("independent_practice"):
            ind_used.add(act)

    def release(point: str, rec: Dict[str, Any]):
        act = action_of(rec)
        chosen_records[point].pop()
        pol = policy.get(point, {})
        if pol.get("dedicated_evidence") and dedicated_owner.get(act) == point:
            dedicated_owner.pop(act, None)
        if pol.get("independent_practice"):
            # only remove if no remaining independent record uses it
            still = any(
                policy.get(p, {}).get("independent_practice")
                and any(action_of(r) == act for r in rs)
                for p, rs in chosen_records.items()
            )
            if not still:
                ind_used.discard(act)

    ordered = list(range(len(slots)))

    def backtrack(k: int) -> bool:
        if k == len(ordered):
            return True
        point, unit = slots[k]
        for rec in candidates.get(point, []):
            if not is_legal(point, rec):
                continue
            reserve(point, rec)
            if backtrack(k + 1):
                return True
            release(point, rec)
        return False

    if backtrack(0):
        return {p: [r["id"] for r in recs] for p, recs in chosen_records.items() if recs}
    return None


def _greedy_partial(
    slots: List[Tuple[str, int]],
    candidates: Dict[str, List[Dict[str, Any]]],
    policy: Dict[str, Dict[str, Any]],
) -> Dict[str, List[str]]:
    """Best-effort assignment used only to explain shortfalls."""
    chosen: Dict[str, List[Dict[str, Any]]] = {p: [] for p, _ in slots}
    dedicated_owner: Dict[str, str] = {}
    counts: Dict[str, int] = {
        p: int(policy.get(p, {}).get("min_count", 1)) for p in chosen
    }
    # dedicated first
    order = sorted(
        counts,
        key=lambda p: (
            0 if policy.get(p, {}).get("dedicated_evidence") else 1,
            0 if policy.get(p, {}).get("independent_practice") else 1,
            p,
        ),
    )
    for point in order:
        pol = policy.get(point, {})
        for rec in candidates.get(point, []):
            if len(chosen[point]) >= counts[point]:
                break
            act = rec.get("action_id") or rec.get("id")
            if any((r.get("action_id") or r.get("id")) == act for r in chosen[point]):
                continue
            owner = dedicated_owner.get(act)
            if owner and owner != point:
                continue
            if pol.get("dedicated_evidence"):
                if any(
                    other != point
                    and any((r.get("action_id") or r.get("id")) == act for r in rs)
                    for other, rs in chosen.items()
                ):
                    continue
                dedicated_owner[act] = point
            if pol.get("independent_practice") and not pol.get("dedicated_evidence"):
                clash = any(
                    other != point
                    and policy.get(other, {}).get("independent_practice")
                    and any((r.get("action_id") or r.get("id")) == act for r in rs)
                    for other, rs in chosen.items()
                )
                if clash:
                    continue
            chosen[point].append(rec)
    return {p: [r["id"] for r in rs] for p, rs in chosen.items()}


def evaluate_evidence(
    records: Iterable[Dict[str, Any]],
    required_points: Iterable[str],
    point_policy: Dict[str, Dict[str, Any]],
    modules: Optional[Dict[str, List[str]]] = None,
    *,
    allowed_ids: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """Evaluate requirement satisfaction from evidence records.

    Returns a structured, JSON-friendly verdict including the evidence chosen
    for every point, missing points and per-candidate rejection reasons.
    """
    records = list(records)
    required = list(dict.fromkeys(required_points))
    candidates: Dict[str, List[Dict[str, Any]]] = {}
    rejected: Dict[str, List[Dict[str, str]]] = {}
    for point in required:
        cands, rej = _eligible_candidates(records, point, allowed_ids)
        candidates[point] = cands
        rejected[point] = rej

    counts = {p: int(point_policy.get(p, {}).get("min_count", 1)) for p in required}
    # One slot per required credit unit; ordering by constraint strength.
    point_order = sorted(
        required,
        key=lambda p: (
            0 if point_policy.get(p, {}).get("dedicated_evidence") else 1,
            0 if point_policy.get(p, {}).get("independent_practice") else 1,
            p,
        ),
    )
    slots: List[Tuple[str, int]] = []
    for p in point_order:
        for unit in range(counts[p]):
            slots.append((p, unit))

    assignment = _assign_all(slots, candidates, point_policy)
    if assignment is None:
        assignment = _greedy_partial(slots, candidates, point_policy)

    points_report: Dict[str, Any] = {}
    satisfied: List[str] = []
    missing: List[str] = []
    for point in required:
        picked = assignment.get(point, [])
        need = counts[point]
        ok = len(picked) >= need
        # Explain candidates that existed but could not be allocated.
        alloc_rejected: List[Dict[str, str]] = []
        picked_set = set(picked)
        for rec in candidates.get(point, []):
            if rec["id"] in picked_set:
                continue
            act = rec.get("action_id") or rec["id"]
            reason = "not_needed"
            pol = point_policy.get(point, {})
            if any((r.get("action_id") or r.get("id")) == act for r in candidates[point] if r["id"] in picked_set):
                reason = "same_action_already_counted"
            elif pol.get("dedicated_evidence"):
                reason = "dedicated_evidence_conflict"
            elif pol.get("independent_practice"):
                reason = "independent_action_reused"
            if len(picked) < need and reason != "not_needed":
                alloc_rejected.append({"id": rec["id"], "reason": reason})
        all_rejected = rejected.get(point, []) + alloc_rejected
        pol = point_policy.get(point, {})
        points_report[point] = {
            "satisfied": ok,
            "required_count": need,
            "evidence": picked,
            "rejected": all_rejected,
            "independent_practice": bool(pol.get("independent_practice")),
            "dedicated_evidence": bool(pol.get("dedicated_evidence")),
        }
        (satisfied if ok else missing).append(point)

    modules_report: Dict[str, Any] = {}
    if modules:
        for mid, pts in modules.items():
            m_missing = [p for p in pts if p in missing]
            modules_report[mid] = {
                "complete": not m_missing and all(p in points_report for p in pts),
                "points": list(pts),
                "missing": m_missing,
            }

    return {
        "satisfied": satisfied,
        "missing": missing,
        "points": points_report,
        "modules": modules_report,
        "complete": not missing,
    }
