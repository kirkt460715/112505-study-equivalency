"""Replay the synthetic fixtures into the backend and build the canonical report.

Timeline (business minutes)::

    5  obs-18 attendance (site-a / observe)
    8  do-14 sign-in under guest-token-x (site-b / alt-art)
   10  equiv-6 narrows alt-art scope
   15  guest-token-x -> l-2
   16  ref-9 independent reflection
   17  cert-7 issued on [obs-18, do-14, ref-9]
   28  obs-18 corrected to departed_early  -> cert-7 enters review

The late publish receipt is bound to the original ``cert-7@17`` decision and
must not move the certificate out of review.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .service import StudyVisitService

CERT_REFERENCE = "cert-7-publish-certificate-school-18-r1"


def load_bundle(folder: Path | str):
    folder = Path(folder)
    register = json.loads((folder / "registers.json").read_text(encoding="utf-8"))
    events = [
        json.loads(line)
        for line in (folder / "events.ndjson").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    receipts = json.loads((folder / "receipts.json").read_text(encoding="utf-8"))
    return register, events, receipts


def apply_event(svc: StudyVisitService, event: Dict[str, Any]) -> Dict[str, Any]:
    source = event.get("source")
    p = event.get("payload", {}) or {}
    minute = event.get("minute")
    cursor = event.get("cursor")

    if source == "attendance":
        return svc.submit_evidence(
            evidence_id=p["receipt"],
            student=p.get("student"),
            participant=p.get("participant"),
            partner=p.get("site"),
            source=source,
            minute=minute,
            cursor=cursor,
            goal=p.get("goal"),
            course=p.get("course"),
        )
    if source == "school":
        return svc.submit_evidence(
            evidence_id=p["receipt"],
            student=p.get("student"),
            partner="school",
            source=source,
            minute=minute,
            cursor=cursor,
            goal=p.get("goal"),
        )
    if source == "identity":
        return svc.confirm_identity(
            p["participant"], p["student"], matched_by=p.get("matched_by"),
            minute=minute, cursor=cursor,
        )
    if source == "academic":
        change = p.get("change")
        if change in ("limited_scope", "revoked"):
            return svc.adjust_substitution(
                p["course"],
                change=change, revision=p.get("revision"), minute=minute,
            )
        return svc.add_substitution(
            p["course"], p.get("supports", []), cohorts=p.get("cohorts"),
            code=p.get("revision"), decided_minute=minute,
        )
    if source == "correction":
        return svc.apply_correction(
            supersedes=p["supersedes"], actual=p["actual"], student=p.get("student"),
            minute=minute, cursor=cursor,
        )
    raise ValueError(f"unknown event source: {source}")


def replay_canonical(folder: Path | str = "fixtures", *, bind_receipt: bool = True) -> StudyVisitService:
    register, events, receipts = load_bundle(folder)
    return replay_bundle(register, events, receipts, bind_receipt=bind_receipt)


def replay_bundle(
    register: Dict[str, Any],
    events: List[Dict[str, Any]],
    receipts: Optional[List[Dict[str, Any]]] = None,
    *,
    bind_receipt: bool = True,
) -> StudyVisitService:
    """Replay a register + paged event stream into a fresh service."""
    svc = StudyVisitService(register)

    issued_seed = register.get("issued", {})
    # Merged business timeline: events and issuance decisions ordered by minute.
    timeline: List[tuple] = []
    for ev in events:
        timeline.append((ev.get("minute", 0), 0, ev.get("cursor", 0), "event", ev))
    for cid, cert in issued_seed.items():
        timeline.append((cert.get("recorded_at", 0), 1, 0, "issue", (cid, cert)))
    timeline.sort(key=lambda x: (x[0], x[1], x[2]))

    for _, _, _, kind, item in timeline:
        if kind == "event":
            apply_event(svc, item)
        else:
            cid, cert = item
            svc.issue_certificate(
                cid, cert["student"], cert["evidence"], issued_at=cert.get("recorded_at")
            )

    # The site-b exchange that contains the unresolved-then-matched sign-in.
    if "site-b" in svc.state["partners"]:
        svc.create_exchange(partner="site-b", subjects=["guest-token-x"], cohort="school-18",
                            note="village exchange: alt-art / hands-on")

    if bind_receipt and receipts:
        bind_fixture_receipt(svc, receipts)
    return svc


def sync_from_gateway(client, *, receipts: Optional[List[Dict[str, Any]]] = None,
                      bind_receipt: bool = False) -> StudyVisitService:
    """Load register and page every event through the documented HTTP API."""
    register = client.registers()
    events = client.all_events()
    return replay_bundle(register, events, receipts, bind_receipt=bind_receipt)


def bind_fixture_receipt(svc: StudyVisitService, receipts: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    example = next(
        (r for r in receipts if r.get("operation") == "publish-certificate"
         and r.get("recipient") == "school-18"),
        receipts[0] if receipts else None,
    )
    if not example:
        return None
    cert_id = example["receipt"].get("certificate", "cert-7")
    if cert_id not in svc.state["certificates"]:
        return None
    return svc.bind_receipt(
        cert_id,
        reference=CERT_REFERENCE,
        operation=example["operation"],
        recipient=example["recipient"],
        receipt=example["receipt"],
        delivery=example.get("delivery", "applied"),
        late=True,
        arrived_minute=29,
    )


def build_report(svc: StudyVisitService) -> Dict[str, Any]:
    students: Dict[str, Any] = {}
    for cohort, roster in svc.state["roster"].items():
        for sid in roster:
            verdict = svc.evaluate_student(sid)
            students[sid] = {
                "cohort": cohort,
                "status": "complete" if verdict["complete"] else "incomplete",
                "qualified": verdict["complete"],
                "satisfied": verdict["satisfied"],
                "missing": verdict["missing"],
                "modules": verdict["modules"],
                "points": verdict["points"],
            }

    identity = {token: svc.identity_view(token) for token in svc.state["bridge"]}

    certificates = {cid: svc.certificate_basis(cid) for cid in svc.state["certificates"]}

    report = {
        "program": svc.state["program"],
        "round": svc.round_id,
        "students": students,
        "identity": identity,
        "substitutions": svc.list_substitutions(),
        "certificates": certificates,
        "review_board": svc.review_board(),
        "partner_views": {},
    }

    partner_views = {
        "site-b_worklist": svc.partner_worklist("site-b"),
    }
    if svc.state["exchanges"]:
        eid = next(iter(svc.state["exchanges"]))
        partner = svc.state["exchanges"][eid]["partner"]
        partner_views["exchange"] = svc.partner_exchange_view(eid, partner)
    report["partner_views"] = partner_views
    return report
