"""Stateful backend use cases for the cross-village study programme.

The service stores plain JSON-compatible dictionaries so the whole state can be
snapshotted to disk.  Every business invariant from the brief is enforced here
or in :mod:`studyvisit.domain`.
"""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import domain
from .errors import (
    CertificationBlocked,
    Conflict,
    IdentityAmbiguous,
    NotFound,
    PartnerNotAuthorized,
    StudyVisitError,
    ValidationFailed,
)

ALL_ACTOR_ROLES = {"teacher", "academic", "school", "partner", "admin"}


class StudyVisitService:
    def __init__(self, register: Optional[Dict[str, Any]] = None, *, round_id: str = "summer"):
        self.lock = threading.RLock()
        self.round_id = round_id
        self.state: Dict[str, Any] = {
            "program": None,
            "default_adoption": {"modules": {}, "point_policy": {}},
            "adoptions": {},
            "roster": {},
            "student_cohort": {},
            "bridge": {},
            "identity": {},
            "partners": {},
            "substitutions": [],
            "evidence": {},
            "certificates": {},
            "exchanges": {},
            "audit": [],
            "counters": {"exchange": 0},
        }
        if register:
            self.load_register(register)

    # ------------------------------------------------------------------ setup
    def load_register(self, reg: Dict[str, Any]) -> None:
        with self.lock:
            adoption = domain.build_adoption(reg)
            self.state["program"] = adoption["program"]
            self.state["default_adoption"] = {
                "modules": adoption["modules"],
                "point_policy": adoption["point_policy"],
            }
            for cohort, students in (reg.get("roster") or {}).items():
                self.state["roster"].setdefault(cohort, [])
                for sid in students:
                    if sid not in self.state["roster"][cohort]:
                        self.state["roster"][cohort].append(sid)
                    self.state["student_cohort"][sid] = cohort
                # Every registered group adopts the programme requirements.
                self.state["adoptions"].setdefault(
                    cohort,
                    {
                        "modules": copy.deepcopy(adoption["modules"]),
                        "point_policy": copy.deepcopy(adoption["point_policy"]),
                    },
                )
            self.state["bridge"] = copy.deepcopy(reg.get("cohort_bridge") or {})
            self.state["partners"] = copy.deepcopy(reg.get("partners") or {})
            for sub in reg.get("substitution") or reg.get("substitutions") or []:
                self._add_substitution_unchecked(copy.deepcopy(sub), source="register")
            # Issued certificates are loaded by the fixture replay so their
            # frozen basis can be captured at the correct business minute.
            self.state["issued_seed"] = copy.deepcopy(reg.get("issued") or {})

    def _add_substitution_unchecked(self, sub: Dict[str, Any], *, source: str = "api") -> Dict[str, Any]:
        sub.setdefault("id", sub.get("code") or f"sub-{sub.get('course')}")
        sub.setdefault("status", "approved")
        sub.setdefault("history", [])
        sub["history"].append({"source": source, "status": sub["status"]})
        # Replace an existing same-course/cohort/round policy in seed reloads.
        for i, existing in enumerate(self.state["substitutions"]):
            if self._sub_identity(existing) == self._sub_identity(sub):
                sub["history"] = existing.get("history", []) + sub["history"]
                self.state["substitutions"][i] = sub
                return sub
        self.state["substitutions"].append(sub)
        return sub

    @staticmethod
    def _sub_identity(sub: Dict[str, Any]):
        return (
            sub.get("course"),
            tuple(sorted(sub.get("cohorts") or [])),
            sub.get("effective_round"),
        )

    def _adoption_for(self, cohort: Optional[str]) -> Dict[str, Any]:
        if cohort and cohort in self.state["adoptions"]:
            return self.state["adoptions"][cohort]
        return self.state["default_adoption"]

    def _points_for(self, cohort: Optional[str]):
        adoption = self._adoption_for(cohort)
        modules = adoption.get("modules") or {}
        policy = adoption.get("point_policy") or {}
        points = [p for pts in modules.values() for p in pts]
        return modules, policy, points

    def audit(self, action: str, *, actor: Optional[str] = None, **detail) -> None:
        entry = {"action": action, "actor": actor or "system", "detail": detail}
        self.state["audit"].append(entry)

    # ------------------------------------------------------------- persistence
    def to_dict(self) -> Dict[str, Any]:
        with self.lock:
            return copy.deepcopy(self.state)

    def save(self, path: os.PathLike | str) -> None:
        data = json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        path = Path(path)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, path)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StudyVisitService":
        svc = cls()
        svc.state = copy.deepcopy(data)
        svc.state.setdefault("evidence", {})
        svc.state.setdefault("certificates", {})
        svc.state.setdefault("audit", [])
        return svc

    @classmethod
    def load(cls, path: os.PathLike | str) -> "StudyVisitService":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data)

    # ----------------------------------------------------------- adoption API
    @staticmethod
    def _normalize_minimum_evidence(minimum_evidence: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """Translate the school's minimum-evidence wording into point policy."""
        policy: Dict[str, Dict[str, Any]] = {}
        for pid, cfg0 in (minimum_evidence or {}).items():
            cfg = dict(cfg0 or {})
            item: Dict[str, Any] = {}
            if "count" in cfg:
                item["min_count"] = int(cfg["count"])
            if "min_count" in cfg:
                item["min_count"] = int(cfg["min_count"])
            if "kinds" in cfg:
                item["evidence_kinds"] = list(cfg["kinds"])
            if "evidence_kinds" in cfg:
                item["evidence_kinds"] = list(cfg["evidence_kinds"])
            for flag in ("independent_practice", "dedicated_evidence"):
                if flag in cfg:
                    item[flag] = bool(cfg[flag])
            policy[pid] = item
        return policy

    def register_adoption(
        self,
        cohort: str,
        modules: Optional[Dict[str, List[str]]] = None,
        point_policy: Optional[Dict[str, Any]] = None,
        minimum_evidence: Optional[Dict[str, Any]] = None,
        *,
        actor: Optional[str] = "school",
    ) -> Dict[str, Any]:
        with self.lock:
            if not cohort:
                raise ValidationFailed("cohort required")
            base = self.state["default_adoption"]
            adoption = self.state["adoptions"].setdefault(
                cohort,
                {"modules": copy.deepcopy(base["modules"]), "point_policy": copy.deepcopy(base["point_policy"])},
            )
            if modules is not None:
                adoption["modules"] = {k: list(v) for k, v in modules.items()}
                known = {p for pts in modules.values() for p in pts}
                for pid in known:
                    adoption["point_policy"].setdefault(pid, {"min_count": 1})
            if point_policy is not None:
                for pid, cfg in point_policy.items():
                    merged = dict(adoption["point_policy"].get(pid, {}))
                    merged.update(cfg or {})
                    adoption["point_policy"][pid] = merged
            if minimum_evidence is not None:
                for pid, cfg in self._normalize_minimum_evidence(minimum_evidence).items():
                    merged = dict(adoption["point_policy"].get(pid, {}))
                    merged.update(cfg)
                    adoption["point_policy"][pid] = merged
            self.audit("register_adoption", actor=actor, cohort=cohort)
            return copy.deepcopy(adoption)

    # -------------------------------------------------------------- roster ops
    def enroll_students(
        self,
        cohort: str,
        student_ids: List[str],
        *,
        actor: Optional[str] = "teacher",
        batch_ref: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Idempotently register a (next) batch of students for a cohort."""
        with self.lock:
            if not cohort or not isinstance(student_ids, list) or not student_ids:
                raise ValidationFailed("cohort and non-empty student_ids required")
            self.state["roster"].setdefault(cohort, [])
            results = []
            for raw in student_ids:
                sid = str(raw).strip()
                if not sid:
                    raise ValidationFailed("student ids must be non-empty")
                existing_cohort = self.state["student_cohort"].get(sid)
                if existing_cohort and existing_cohort != cohort:
                    results.append(
                        {"student": sid, "status": "conflict", "cohort": existing_cohort}
                    )
                    continue
                created = sid not in self.state["roster"][cohort]
                if created:
                    self.state["roster"][cohort].append(sid)
                self.state["student_cohort"][sid] = cohort
                results.append(
                    {"student": sid, "status": "enrolled" if created else "already_enrolled", "cohort": cohort}
                )
            self.audit("enroll_batch", actor=actor, cohort=cohort, batch_ref=batch_ref, count=len(student_ids))
            return {"cohort": cohort, "batch_ref": batch_ref, "results": results}

    def roster(self, cohort: str) -> List[str]:
        with self.lock:
            return list(self.state["roster"].get(cohort, []))

    def reclass_student(self, student: str, to_cohort: str, *, actor: str = "teacher") -> Dict[str, Any]:
        """Move a student to another class/cohort without copying any sign-in."""
        with self.lock:
            old = self.state["student_cohort"].get(student)
            if old is None:
                raise NotFound(f"unknown student {student}")
            if old == to_cohort:
                return {"student": student, "cohort": to_cohort, "changed": False}
            self.state["roster"].setdefault(old, [])
            if student in self.state["roster"][old]:
                self.state["roster"][old].remove(student)
            self.state["roster"].setdefault(to_cohort, [])
            if student not in self.state["roster"][to_cohort]:
                self.state["roster"][to_cohort].append(student)
            self.state["student_cohort"][student] = to_cohort
            # Evidence has a single student_id foreign key; moving the student
            # simply follows them.  Nothing is duplicated onto another learner.
            self.audit("reclass_student", actor=actor, student=student, **{"from": old, "to": to_cohort})
            return {"student": student, "from": old, "to": to_cohort, "changed": True}

    # ------------------------------------------------------------ identity map
    def confirm_identity(
        self,
        token: str,
        student: str,
        *,
        matched_by: Optional[str] = None,
        minute: Optional[int] = None,
        cursor: Optional[int] = None,
        allow_unlisted: bool = False,
    ) -> Dict[str, Any]:
        """Resolve a bridge participant token to exactly one real student.

        A token that bridges to several candidates is never, by itself, treated
        as attendance for all of them.  Confirmation attributes each sign-in
        record once, to one student.
        """
        with self.lock:
            candidates = self.state["bridge"].get(token)
            if candidates is not None and student not in candidates and not allow_unlisted:
                raise IdentityAmbiguous(
                    f"{student} is not among bridge candidates for {token}",
                    details={"token": token, "candidates": candidates},
                )
            prior = self.state["identity"].get(token)
            if prior and prior["student"] != student:
                # Explicit identity correction: redirect, never duplicate.
                self._redirect_token(token, prior["student"], student)
            self.state["identity"][token] = {
                "student": student,
                "matched_by": matched_by,
                "minute": minute,
                "cursor": cursor,
            }
            attributed = self._attribute_token_claims(token, student)
            self.audit("confirm_identity", actor=matched_by, token=token, student=student, attributed=attributed)
            return {"token": token, "student": student, "attributed": attributed}

    def _attribute_token_claims(self, token: str, student: str) -> List[str]:
        attributed = []
        for rec in self.state["evidence"].values():
            if rec.get("subject_type") == "token" and rec.get("subject_id") == token:
                if rec.get("student_id") != student:
                    rec["student_id"] = student
                    rec["attributed_minute"] = rec.get("attributed_minute")
                    attributed.append(rec["id"])
        return attributed

    def _redirect_token(self, token: str, old_student: str, new_student: str) -> None:
        for rec in self.state["evidence"].values():
            if rec.get("subject_type") == "token" and rec.get("subject_id") == token:
                if rec.get("student_id") == old_student:
                    rec["student_id"] = new_student

    def identity_view(self, token: str) -> Dict[str, Any]:
        with self.lock:
            match = self.state["identity"].get(token)
            receipts = [
                r["id"]
                for r in self.state["evidence"].values()
                if r.get("subject_type") == "token" and r.get("subject_id") == token
            ]
            return {
                "token": token,
                "candidates": self.state["bridge"].get(token),
                "matched_student": match["student"] if match else None,
                "match": match,
                "receipts": receipts,
            }

    # ------------------------------------------------------------ substitution
    def add_substitution(
        self,
        course: str,
        supports: List[str],
        *,
        cohorts: Optional[List[str]] = None,
        effective_round: Optional[str] = None,
        code: Optional[str] = None,
        status: str = "approved",
        decided_minute: Optional[int] = None,
        effective_from_minute: Optional[int] = None,
        actor: str = "academic",
        retroactive: bool = False,
    ) -> Dict[str, Any]:
        with self.lock:
            if not course or not supports:
                raise ValidationFailed("course and non-empty supports required")
            sub = {
                "id": code or f"sub-{course}-{len(self.state['substitutions']) + 1}",
                "code": code,
                "course": course,
                "supports": list(supports),
                "cohorts": list(cohorts) if cohorts else "*",
                "effective_round": effective_round or self.round_id,
                "status": status,
                "scope_limited": status == "limited",
                "decided_minute": decided_minute,
                "effective_from_minute": effective_from_minute,
                "retroactive": retroactive,
                "history": [{"action": "approved", "actor": actor, "minute": decided_minute}],
            }
            self._add_substitution_unchecked(sub, source=actor)
            self.audit("add_substitution", actor=actor, course=course, supports=supports, cohorts=cohorts)
            self._reassess_affected_certificates(course=course)
            return copy.deepcopy(sub)

    def adjust_substitution(
        self,
        key: str,
        *,
        supports: Optional[List[str]] = None,
        cohorts: Optional[List[str]] = None,
        status: Optional[str] = None,
        change: Optional[str] = None,
        revision: Optional[str] = None,
        minute: Optional[int] = None,
        actor: str = "academic",
    ) -> Dict[str, Any]:
        """Narrow (or revoke) an approved equivalence.

        ``limited_scope`` without explicit narrower fields fixes the policy to
        its registered scope and prevents any broadening.  Explicit
        ``supports``/``cohorts`` are intersected so an adjustment can only
        shrink an approval, never silently expand it.
        """
        with self.lock:
            sub = self._find_substitution(key)
            if sub is None:
                raise NotFound(f"unknown substitution {key}")
            before = {"supports": list(sub.get("supports") or [])}
            if change == "limited_scope" or status == "limited":
                sub["status"] = "limited"
                sub["scope_limited"] = True
            if status == "revoked" or change == "revoked":
                sub["status"] = "revoked"
            if supports is not None:
                # Narrow only.  With an existing approval the new point set is
                # intersected so an adjustment can never silently broaden it;
                # only a policy with no prior point list takes the list as-is.
                current_points = sub.get("supports") or []
                if current_points:
                    wanted = set(supports)
                    sub["supports"] = [p for p in current_points if p in wanted]
                else:
                    sub["supports"] = list(supports)
            if cohorts is not None:
                current = sub.get("cohorts")
                if current in (None, "*"):
                    sub["cohorts"] = list(cohorts)
                else:
                    sub["cohorts"] = [c for c in current if c in set(cohorts)]
            if revision:
                sub.setdefault("revision_codes", []).append(revision)
                sub["revision"] = revision
            sub.setdefault("history", []).append(
                {
                    "action": change or status or "adjusted",
                    "revision": revision,
                    "minute": minute,
                    "actor": actor,
                    "before": before,
                }
            )
            self.audit("adjust_substitution", actor=actor, key=key, revision=revision, change=change)
            affected = self._reassess_affected_certificates(course=sub.get("course"))
            return {"substitution": copy.deepcopy(sub), "reassessed": affected}

    def list_substitutions(self) -> List[Dict[str, Any]]:
        with self.lock:
            return copy.deepcopy(self.state["substitutions"])

    def _find_substitution(self, key: str) -> Optional[Dict[str, Any]]:
        for sub in self.state["substitutions"]:
            if key in {sub.get("id"), sub.get("code"), sub.get("course"), sub.get("revision")}:
                return sub
            if key in (sub.get("revision_codes") or []):
                return sub
        # course may match several; return the first
        for sub in self.state["substitutions"]:
            if sub.get("course") == key:
                return sub
        return None

    def _reassess_affected_certificates(self, *, course: Optional[str] = None) -> List[str]:
        affected = []
        for cid, cert in self.state["certificates"].items():
            if course:
                # Only reassess if any basis record used that course.
                if not any(
                    self.state["evidence"].get(eid, {}).get("course") == course
                    for eid in cert.get("basis_evidence", [])
                ):
                    continue
            result = self.assess_certificate(cid, persist=True)
            affected.append({"certificate": cid, "status": result["status"], "unsupported": result["unsupported_points"]})
        return affected

    # ---------------------------------------------------------------- evidence
    def submit_evidence(
        self,
        *,
        evidence_id: str,
        student: Optional[str] = None,
        participant: Optional[str] = None,
        partner: Optional[str] = None,
        source: str = "api",
        minute: Optional[int] = None,
        cursor: Optional[int] = None,
        goal: Optional[str] = None,
        course: Optional[str] = None,
        supports: Optional[List[str]] = None,
        action_id: Optional[str] = None,
        evidence_kind: Optional[str] = None,
        cohort: Optional[str] = None,
        qualifies: bool = True,
    ) -> Dict[str, Any]:
        with self.lock:
            if not evidence_id:
                raise ValidationFailed("evidence_id required")
            existing = self.state["evidence"].get(evidence_id)
            if existing is not None:
                return {"evidence": copy.deepcopy(existing), "created": False, "idempotent": True}

            subject_type = "student" if student else "token"
            subject_id = student or participant
            if not subject_id:
                raise ValidationFailed("either student or participant required")

            cohort_hint = cohort or (self.state["student_cohort"].get(student) if student else None)

            # Resolve the raw capability claims.
            requires_substitution = False
            if supports is not None:
                claims = list(supports)
            elif goal:
                modules, _, _ = self._points_for(cohort_hint)
                claims = domain.expand_goal(modules, goal)
                if not claims:
                    # Unknown goal: still store, but it grants nothing.
                    claims = []
            elif course:
                requires_substitution = True
                template = domain.effective_substitution(
                    self.state["substitutions"], course, cohort=cohort_hint, round_id=self.round_id
                ) or self._find_substitution(course)
                claims = list((template or {}).get("supports") or [])
            else:
                claims = []

            # Partner mandate: a site may only submit its authorised segments.
            auth = {"partner": partner, "authorized": True, "uncovered": []}
            if partner and partner in self.state["partners"] and claims:
                modules, _, _ = self._points_for(cohort_hint)
                scopes = self.state["partners"][partner]
                uncovered = domain.partner_uncovered(scopes, claims, modules)
                if uncovered:
                    auth.update(authorized=False, uncovered=uncovered)
                    raise PartnerNotAuthorized(
                        f"partner {partner} is not authorised for {uncovered}",
                        details={"partner": partner, "uncovered": uncovered, "claims": claims},
                    )

            student_id = student
            if not student and participant:
                match = self.state["identity"].get(participant)
                student_id = match["student"] if match else None

            rec = {
                "id": evidence_id,
                "cursor": cursor,
                "minute": minute,
                "source": source,
                "partner": partner,
                "subject_type": subject_type,
                "subject_id": subject_id,
                "student_id": student_id,
                "course": course,
                "raw_goal": goal,
                "action_id": action_id or evidence_id,
                "evidence_kind": evidence_kind,
                "claims": claims,
                "requires_substitution": requires_substitution,
                "cohort_hint": cohort_hint,
                "authorization": auth,
                "status": "active",
                "qualifies": qualifies,
                "disqualify_reason": None,
                "actual": None,
                "corrections": [],
            }
            self.state["evidence"][evidence_id] = rec
            self.audit("submit_evidence", actor=partner or source, evidence=evidence_id, student=student_id)
            return {"evidence": copy.deepcopy(rec), "created": True}

    def submit_evidence_batch(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Submit a batch of attendance/segment records.

        Each item is processed independently so one unauthorised or invalid row
        never silently discards the rest of the batch.  Items are idempotent by
        ``evidence_id``.
        """
        results = []
        accepted = 0
        rejected = 0
        for raw in items or []:
            eid = raw.get("evidence_id") or raw.get("receipt")
            try:
                result = self.submit_evidence(
                    evidence_id=eid,
                    student=raw.get("student"),
                    participant=raw.get("participant"),
                    partner=raw.get("partner") or raw.get("site"),
                    source=raw.get("source", "batch"),
                    minute=raw.get("minute"),
                    goal=raw.get("goal"),
                    course=raw.get("course"),
                    supports=raw.get("supports"),
                    action_id=raw.get("action_id"),
                    evidence_kind=raw.get("evidence_kind"),
                    cohort=raw.get("cohort"),
                )
                results.append({"evidence_id": eid, "status": "accepted",
                                "created": result["created"], "student_id": result["evidence"]["student_id"]})
                accepted += 1
            except StudyVisitError as exc:
                results.append({"evidence_id": eid, "status": "rejected",
                                 "code": exc.code, "details": exc.details})
                rejected += 1
        return {"accepted": accepted, "rejected": rejected, "results": results}

    def apply_correction(
        self,
        *,
        supersedes: str,
        actual: str,
        student: Optional[str] = None,
        minute: Optional[int] = None,
        cursor: Optional[int] = None,
        correction_id: Optional[str] = None,
        actor: str = "correction",
    ) -> Dict[str, Any]:
        with self.lock:
            rec = self.state["evidence"].get(supersedes)
            if rec is None:
                raise NotFound(f"cannot correct unknown evidence {supersedes}")
            cid = correction_id or f"corr-{cursor or supersedes}-{minute}"
            if any(c.get("correction_id") == cid for c in rec.get("corrections", [])):
                return {"correction_id": cid, "idempotent": True, "affected_certificates": []}
            entry = {"correction_id": cid, "actual": actual, "minute": minute, "cursor": cursor, "actor": actor}
            rec.setdefault("corrections", []).append(entry)
            rec["status"] = "superseded"
            rec["actual"] = actual
            if actual in domain.NON_QUALIFYING_ACTUALS:
                rec["qualifies"] = False
                rec["disqualify_reason"] = actual
            else:
                rec["qualifies"] = True
                rec["disqualify_reason"] = None
            if student and rec.get("student_id") is None:
                rec["student_id"] = student
            self.audit("correction", actor=actor, supersedes=supersedes, actual=actual)
            affected = self._reassess_certificates_for_evidence(supersedes)
            return {"correction_id": cid, "record": copy.deepcopy(rec), "affected_certificates": affected}

    def _reassess_certificates_for_evidence(self, evidence_id: str) -> List[Dict[str, Any]]:
        out = []
        for cid, cert in self.state["certificates"].items():
            if evidence_id in cert.get("basis_evidence", []):
                out.append(self.assess_certificate(cid, persist=True))
        return out

    def _prepared_records(self, student_id: str, allowed_ids: Optional[set] = None) -> List[Dict[str, Any]]:
        cohort = self.state["student_cohort"].get(student_id)
        _, policy, _ = self._points_for(cohort)
        prepared: List[Dict[str, Any]] = []
        for rec0 in self.state["evidence"].values():
            if rec0.get("student_id") != student_id:
                continue
            if allowed_ids is not None and rec0["id"] not in allowed_ids:
                continue
            rec = copy.deepcopy(rec0)
            claims = list(rec.get("claims") or [])
            if rec.get("requires_substitution") and rec.get("course"):
                sub = domain.effective_substitution(
                    self.state["substitutions"],
                    rec["course"],
                    cohort=cohort,
                    round_id=self.round_id,
                )
                if not sub:
                    rec["claims"] = []
                    rec["substitution_error"] = "not_approved_for_cohort"
                    prepared.append(rec)
                    continue
                allowed = set(sub.get("supports") or [])
                rec["claims"] = [p for p in claims if p in allowed]
                rec["substitution_applied"] = sub.get("code") or sub.get("id") or sub.get("course")
            else:
                rec["claims"] = claims
            prepared.append(rec)
        return prepared

    # ------------------------------------------------------------- verification
    def evaluate_student(self, student_id: str) -> Dict[str, Any]:
        with self.lock:
            cohort = self.state["student_cohort"].get(student_id)
            modules, policy, points = self._points_for(cohort)
            records = self._prepared_records(student_id)
            verdict = domain.evaluate_evidence(records, points, policy, modules)
            verdict["student"] = student_id
            verdict["cohort"] = cohort
            verdict["qualified"] = verdict["complete"]
            # Pending sign-ins presented under a bridge token this student is a
            # candidate for (only meaningful before identity is confirmed).
            verdict["unresolved_receipts"] = [
                r["id"]
                for r in self.state["evidence"].values()
                if r.get("student_id") is None and self._token_candidate_of(r, student_id)
            ]
            return verdict

    def _token_candidate_of(self, rec: Dict[str, Any], student_id: str) -> bool:
        if rec.get("subject_type") != "token":
            return False
        token = rec.get("subject_id")
        return student_id in (self.state["bridge"].get(token) or [])

    def passport(self, student_id: str) -> Dict[str, Any]:
        verdict = self.evaluate_student(student_id)
        return {
            "student": student_id,
            "cohort": verdict["cohort"],
            "verified_points": verdict["satisfied"],
            "missing_points": verdict["missing"],
            "complete": verdict["complete"],
            "evidence": {p: verdict["points"][p]["evidence"] for p in verdict["satisfied"]},
        }

    def transfer(self, subjects: List[str], to_partner: str, *, minute: Optional[int] = None, actor: str = "teacher") -> Dict[str, Any]:
        with self.lock:
            if to_partner not in self.state["partners"]:
                raise ValidationFailed(f"unknown receiving partner {to_partner}")
            passports = []
            for sid in subjects:
                if sid not in self.state["student_cohort"]:
                    raise NotFound(f"unknown student {sid}")
                pp = self.passport(sid)
                pp["transferred_to"] = to_partner
                pp["minute"] = minute
                passports.append(pp)
                self.state.setdefault("station_log", []).append(
                    {"student": sid, "to_partner": to_partner, "minute": minute, "verified_points": pp["verified_points"]}
                )
            self.audit("transfer", actor=actor, to=to_partner, subjects=subjects)
            # The receiving partner's mandate is returned so callers can see the
            # only segments it may add; enforcement lives in submit_evidence.
            return {
                "to_partner": to_partner,
                "authorised_segments": self.state["partners"][to_partner],
                "passports": passports,
            }

    # ------------------------------------------------------------ certificates
    def issue_certificate(
        self,
        cert_id: str,
        student: str,
        evidence_ids: List[str],
        *,
        issued_at: Optional[int] = None,
        actor: str = "school",
        force: bool = False,
    ) -> Dict[str, Any]:
        with self.lock:
            if cert_id in self.state["certificates"]:
                prior = self.state["certificates"][cert_id]
                if prior["student"] != student:
                    raise Conflict(f"reference {cert_id} reused for another student")
                return {"certificate": copy.deepcopy(prior), "created": False, "idempotent": True}
            if student not in self.state["student_cohort"]:
                raise NotFound(f"unknown student {student}")
            evidence_ids = list(dict.fromkeys(evidence_ids))
            for eid in evidence_ids:
                if eid not in self.state["evidence"]:
                    raise ValidationFailed(f"basis evidence {eid} not found")

            verdict = self._evaluate_basis(student, evidence_ids)
            if not verdict["complete"] and not force:
                raise CertificationBlocked(
                    "current evidence does not meet the adopted requirements",
                    details={"missing": verdict["missing"], "evaluation": verdict},
                )

            cohort = self.state["student_cohort"].get(student)
            _, policy, points = self._points_for(cohort)
            snapshot = {
                "recorded_at": issued_at,
                "evidence": {
                    eid: {
                        "claims": self.state["evidence"][eid].get("claims"),
                        "status": self.state["evidence"][eid].get("status"),
                        "qualifies": self.state["evidence"][eid].get("qualifies"),
                        "minute": self.state["evidence"][eid].get("minute"),
                        "partner": self.state["evidence"][eid].get("partner"),
                        "course": self.state["evidence"][eid].get("course"),
                        "action_id": self.state["evidence"][eid].get("action_id"),
                    }
                    for eid in evidence_ids
                },
                "evaluation": verdict,
                "required_points": points,
                "point_policy": copy.deepcopy(policy),
                "substitutions": copy.deepcopy(self.state["substitutions"]),
                "sufficient_at_issue": verdict["complete"],
            }
            cert = {
                "id": cert_id,
                "student": student,
                "cohort": cohort,
                "issued_at": issued_at,
                "status": "issued",
                "basis_evidence": evidence_ids,
                "basis_snapshot": snapshot,
                "status_history": [{"status": "issued", "at": issued_at, "actor": actor}],
                "decisions": [{"decision": "issue", "at": issued_at, "actor": actor,
                               "basis": evidence_ids}],
                "review": None,
                "receipts": [],
            }
            self.state["certificates"][cert_id] = cert
            self.audit("issue_certificate", actor=actor, certificate=cert_id, student=student)
            return {"certificate": copy.deepcopy(cert), "created": True}

    def _evaluate_basis(self, student: str, evidence_ids: List[str]) -> Dict[str, Any]:
        cohort = self.state["student_cohort"].get(student)
        modules, policy, points = self._points_for(cohort)
        allowed = set(evidence_ids)
        records = self._prepared_records(student, allowed_ids=allowed)
        return domain.evaluate_evidence(records, points, policy, modules)

    def assess_certificate(self, cert_id: str, *, persist: bool = False) -> Dict[str, Any]:
        with self.lock:
            cert = self.state["certificates"].get(cert_id)
            if cert is None:
                raise NotFound(f"unknown certificate {cert_id}")
            student = cert["student"]
            cohort = cert.get("cohort") or self.state["student_cohort"].get(student)
            modules, policy, points = self._points_for(cohort)

            modules, policy, points = self._points_for(cohort)

            basis_verdict = self._evaluate_basis(student, cert["basis_evidence"])
            current_verdict = self.evaluate_student(student)

            point_support: Dict[str, Any] = {}
            unsupported: List[str] = []
            still_supported: List[str] = []
            for pid in points:
                basis_ev = basis_verdict["points"].get(pid, {}).get("evidence", [])
                all_ev = current_verdict["points"].get(pid, {}).get("evidence", [])
                if basis_ev:
                    state_kind = "supported_by_adopted_basis"
                    ev = basis_ev
                    still_supported.append(pid)
                elif all_ev:
                    state_kind = "supported_by_new_evidence"
                    ev = all_ev
                    still_supported.append(pid)
                else:
                    state_kind = "unsupported"
                    ev = []
                    unsupported.append(pid)
                point_support[pid] = {
                    "support": state_kind,
                    "support_zh": {
                        "supported_by_adopted_basis": "发证依据中的证据仍独立支撑",
                        "supported_by_new_evidence": "由发证后补交的活动支撑",
                        "unsupported": "已失去有效支撑",
                    }[state_kind],
                    "evidence": ev,
                    "module": domain.module_of_point(modules, pid),
                    "independent_practice": policy.get(pid, {}).get("independent_practice", False),
                    "dedicated_evidence": policy.get(pid, {}).get("dedicated_evidence", False),
                }

            missing_modules = sorted({
                domain.module_of_point(modules, p) or p for p in unsupported
            })
            required_actions: List[str] = []
            if unsupported:
                required_actions.append("formal_review")
                required_actions.extend(f"supplementary_activity:{m}" for m in missing_modules)

            assessment = {
                "certificate": cert_id,
                "status": cert["status"],
                "unsupported_points": unsupported,
                "still_supported_points": still_supported,
                "independent_support": {
                    p: point_support[p]["evidence"] for p in still_supported
                },
                "point_support": point_support,
                "basis_current_complete": basis_verdict["complete"],
                "student_current_complete": current_verdict["complete"],
                "can_reaffirm": not unsupported and cert["status"] == "in_review",
                # Explicit aliases for the school's correction-impact screen.
                "needs_review": bool(unsupported),
                "review_required": bool(unsupported),
                "needs_supplementary_activity": bool(unsupported),
                "supplement_required_for": unsupported,
                "missing_modules": missing_modules,
                "required_actions": required_actions,
                "recommended_action": (
                    "none"
                    if not unsupported
                    else "review_or_supplement:" + ",".join(missing_modules)
                ),
            }

            if persist and unsupported and cert["status"] in ("issued", "reaffirmed"):
                cert["status"] = "in_review"
                cert["review"] = {
                    "opened_reason": "adopted_evidence_corrected_or_policy_changed",
                    "unsupported_points": unsupported,
                    "assessment": assessment,
                }
                cert["status_history"].append({"status": "in_review", "reason": "support_lost"})
                cert["decisions"].append({"decision": "enter_review", "unsupported": unsupported})
                assessment["status"] = "in_review"
            elif persist:
                assessment["status"] = cert["status"]
                if cert.get("review") is not None:
                    cert["review"]["last_assessment"] = assessment
            return assessment

    STATUS_ZH = {
        "issued": "已签发",
        "in_review": "复议中",
        "reaffirmed": "复议后维持",
        "revoked": "已撤销",
    }

    def review_board(self) -> Dict[str, Any]:
        """School-wide correction impact: which certs still stand, which need review."""
        with self.lock:
            rows = []
            for cid in self.state["certificates"]:
                a = self.assess_certificate(cid, persist=False)
                cert = self.state["certificates"][cid]
                rows.append({
                    "certificate": cid,
                    "student": cert["student"],
                    "status": cert["status"],
                    "status_zh": self.STATUS_ZH.get(cert["status"], cert["status"]),
                    "needs_review": a["needs_review"],
                    "unsupported_points": a["unsupported_points"],
                    "still_supported_points": a["still_supported_points"],
                    "independent_support": a["independent_support"],
                    "required_actions": a["required_actions"],
                    "recommended_action": a["recommended_action"],
                })
            return {
                "needs_review": [r for r in rows if r["needs_review"]],
                "still_supported": [r for r in rows if not r["needs_review"]],
                "certificates": rows,
            }

    def reaffirm_certificate(self, cert_id: str, *, actor: str = "school", minute: Optional[int] = None) -> Dict[str, Any]:
        with self.lock:
            cert = self.state["certificates"].get(cert_id)
            if cert is None:
                raise NotFound(cert_id)
            verdict = self.evaluate_student(cert["student"])
            if not verdict["complete"]:
                raise CertificationBlocked(
                    "cannot reaffirm; requirements still missing", details={"missing": verdict["missing"]}
                )
            cert["status"] = "reaffirmed"
            cert["review"] = None
            cert["status_history"].append({"status": "reaffirmed", "at": minute, "actor": actor})
            cert["decisions"].append({"decision": "reaffirm", "at": minute, "actor": actor})
            self.audit("reaffirm_certificate", actor=actor, certificate=cert_id)
            return copy.deepcopy(cert)

    def revoke_certificate(self, cert_id: str, reason: str, *, actor: str = "school") -> Dict[str, Any]:
        with self.lock:
            cert = self.state["certificates"].get(cert_id)
            if cert is None:
                raise NotFound(cert_id)
            cert["status"] = "revoked"
            cert["status_history"].append({"status": "revoked", "reason": reason, "actor": actor})
            cert["decisions"].append({"decision": "revoke", "reason": reason, "actor": actor})
            return copy.deepcopy(cert)

    def bind_receipt(
        self,
        cert_id: str,
        *,
        reference: str,
        operation: str,
        recipient: str,
        receipt: Dict[str, Any],
        delivery: str = "applied",
        late: bool = False,
        arrived_minute: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Attach a counterparty receipt to the ORIGINAL issuance decision.

        A receipt is an acknowledgement of a send; it is never a new decision
        and must not restore a certificate that has entered review.
        """
        with self.lock:
            cert = self.state["certificates"].get(cert_id)
            if cert is None:
                raise NotFound(cert_id)
            for r in cert["receipts"]:
                if r["reference"] == reference:
                    return {"receipt": copy.deepcopy(r), "idempotent": True, "certificate_status": cert["status"]}
            bound = {
                "reference": reference,
                "operation": operation,
                "recipient": recipient,
                "receipt": receipt,
                "delivery": delivery,
                "late": late,
                "arrived_minute": arrived_minute,
                "bound_to_decision": f"{cert_id}#issued@{cert.get('issued_at')}",
                "original_issued_at": cert.get("issued_at"),
                "status_did_not_change": True,
            }
            cert["receipts"].append(bound)
            cert["decisions"].append(
                {"decision": "receipt_recorded", "reference": reference, "late": late,
                 "note": "acknowledges original issuance; does not change certificate status"}
            )
            self.audit("bind_receipt", certificate=cert_id, reference=reference, late=late,
                       certificate_status=cert["status"])
            return {"receipt": copy.deepcopy(bound), "certificate_status": cert["status"]}

    def certificate_basis(self, cert_id: str) -> Dict[str, Any]:
        with self.lock:
            cert = self.state["certificates"].get(cert_id)
            if cert is None:
                raise NotFound(cert_id)
            assessment = self.assess_certificate(cert_id, persist=False)
            return {
                "certificate": cert_id,
                "student": cert["student"],
                "cohort": cert.get("cohort"),
                "status": cert["status"],
                "status_zh": self.STATUS_ZH.get(cert["status"], cert["status"]),
                "needs_review": cert["status"] == "in_review" or assessment["needs_review"],
                "issued_at": cert.get("issued_at"),
                "adopted_basis_evidence": cert["basis_evidence"],
                "basis_was_sufficient_at_issue": cert["basis_snapshot"]["sufficient_at_issue"],
                "frozen_basis": cert["basis_snapshot"],
                "current_corroboration": assessment,
                "receipts": copy.deepcopy(cert["receipts"]),
                "decisions": copy.deepcopy(cert["decisions"]),
            }

    # --------------------------------------------------------------- exchanges
    def create_exchange(
        self,
        *,
        partner: str,
        subjects: List[str],
        cohort: Optional[str] = None,
        note: Optional[str] = None,
        actor: str = "school",
    ) -> Dict[str, Any]:
        with self.lock:
            if partner not in self.state["partners"]:
                raise ValidationFailed(f"unknown partner {partner}")
            if not subjects:
                raise ValidationFailed("subjects required")
            self.state["counters"]["exchange"] += 1
            eid = f"ex-{self.state['counters']['exchange']}"
            ex = {"id": eid, "partner": partner, "cohort": cohort, "subjects": list(subjects), "note": note}
            self.state["exchanges"][eid] = ex
            self.audit("create_exchange", actor=actor, exchange=eid, partner=partner, subjects=subjects)
            return copy.deepcopy(ex)

    def partner_exchange_view(self, exchange_id: str, partner: str) -> Dict[str, Any]:
        with self.lock:
            ex = self.state["exchanges"].get(exchange_id)
            if ex is None:
                raise NotFound(exchange_id)
            if ex["partner"] != partner:
                # Do not reveal existence/details to another partner.
                raise PartnerNotAuthorized("exchange belongs to a different partner")
            return self._build_partner_view(partner, ex["subjects"], exchange_id=exchange_id)

    def partner_worklist(self, partner: str, *, cohort: Optional[str] = None) -> Dict[str, Any]:
        with self.lock:
            if partner not in self.state["partners"]:
                raise NotFound(f"unknown partner {partner}")
            # Collect the raw subjects this partner has touched ...
            raw_subjects: List[str] = []
            for rec in self.state["evidence"].values():
                if rec.get("partner") == partner and rec.get("subject_id"):
                    raw_subjects.append(rec["subject_id"])
            for ex in self.state["exchanges"].values():
                if ex["partner"] == partner:
                    raw_subjects.extend(ex["subjects"])
            # ... then collapse a now-resolved token onto its single student so
            # the same sign-in is never listed twice for one learner.
            subjects: List[str] = []
            for raw in raw_subjects:
                match = self.state["identity"].get(raw)
                canonical = match["student"] if match else raw
                if canonical not in subjects:
                    subjects.append(canonical)
            view = self._build_partner_view(partner, subjects, cohort=cohort)
            view["partner"] = partner
            return view

    def _build_partner_view(
        self, partner: str, subjects: List[str], *, exchange_id: Optional[str] = None, cohort: Optional[str] = None
    ) -> Dict[str, Any]:
        scopes = self.state["partners"][partner]
        items = []
        for subject in subjects:
            is_token = subject in self.state["bridge"] or (
                not self.state["student_cohort"].get(subject) and str(subject).startswith("guest-token")
            )
            match = self.state["identity"].get(subject) if is_token else None
            sid = subject if not is_token else (match["student"] if match else None)
            if cohort and sid and self.state["student_cohort"].get(sid) != cohort:
                continue
            # Only THIS partner's own records are exposed in the exchange.
            own_records = [
                {
                    "receipt": r["id"],
                    "minute": r.get("minute"),
                    "goal": r.get("raw_goal"),
                    "course": r.get("course"),
                    "claims": r.get("claims"),
                    "status": r.get("status"),
                    "qualifies": r.get("qualifies"),
                }
                for r in self.state["evidence"].values()
                if r.get("partner") == partner
                and (r.get("subject_id") == subject or (sid and r.get("student_id") == sid))
            ]
            needs: List[str] = []
            if is_token and not match:
                needs.append("confirm_identity")
            token_aliases = [
                tok for tok, m in self.state["identity"].items()
                if sid and m.get("student") == sid
            ]
            for r in own_records:
                if r["status"] == "superseded" or not r["qualifies"]:
                    needs.append(f"corrected:{r['receipt']}")
            items.append(
                {
                    "subject": subject,
                    "subject_type": "participant_token" if is_token else "student",
                    "resolved_student": sid,
                    "participant_tokens": token_aliases,
                    "needs": sorted(set(needs)),
                    "records": own_records,
                    "may_submit_segments": scopes,
                }
            )
        return {
            "exchange": exchange_id,
            "partner": partner,
            "authorised_segments": scopes,
            "items": items,
        }
