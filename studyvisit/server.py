"""Standard-library JSON HTTP adapter for :class:`StudyVisitService`.

Run with ``python3 -m studyvisit serve``.  The API is self-describing at
``GET /``.  Partner data minimisation is enforced on the partner endpoints;
segment mandate is enforced on every evidence write that names a ``partner``.
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlsplit

from .errors import StudyVisitError
from .service import StudyVisitService

MAX_BODY = 1_048_576


def create_server(port: int = 8080, service: Optional[StudyVisitService] = None) -> ThreadingHTTPServer:
    service = service or StudyVisitService()

    class Handler(BaseHTTPRequestHandler):
        server_version = "StudyVisit/1.0"

        # ------------------------------------------------------------ helpers
        def _send(self, status: int, payload: Any):
            data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _ok(self, payload: Any, status: int = 200):
            self._send(status, payload)

        def _body(self) -> Dict[str, Any]:
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                size = 0
            if size <= 0:
                return {}
            if size > MAX_BODY:
                raise StudyVisitError("body too large", )
            raw = self.rfile.read(size)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise StudyVisitError("body must be a JSON object") from exc
            if not isinstance(data, dict):
                raise StudyVisitError("body must be a JSON object")
            return data

        def _qs(self) -> Dict[str, str]:
            q = parse_qs(urlsplit(self.path).query)
            return {k: v[-1] for k, v in q.items()}

        def log_message(self, *args):  # quiet tests
            return

        # -------------------------------------------------------------- routing
        def do_GET(self):  # noqa: N802
            try:
                self._dispatch_get()
            except StudyVisitError as e:
                self._send(e.status, e.to_payload())
            except Exception as e:  # pragma: no cover - defensive
                self._send(500, {"code": "internal_error", "message": str(e)})

        def do_POST(self):  # noqa: N802
            try:
                self._dispatch_post()
            except StudyVisitError as e:
                self._send(e.status, e.to_payload())
            except Exception as e:  # pragma: no cover
                self._send(500, {"code": "internal_error", "message": str(e)})

        def do_PATCH(self):  # noqa: N802
            try:
                self._dispatch_patch()
            except StudyVisitError as e:
                self._send(e.status, e.to_payload())
            except Exception as e:  # pragma: no cover
                self._send(500, {"code": "internal_error", "message": str(e)})

        def _dispatch_get(self):
            path = urlsplit(self.path).path.rstrip("/") or "/"
            qs = self._qs()
            if path == "/":
                return self._ok(API_INDEX)
            if path in ("/health", "/api/health"):
                return self._send(200, {"status": "ok", "program": service.state["program"]})
            if path == "/api/school/review-board":
                return self._ok(service.review_board())
            if path == "/api/certificates":
                rows = []
                for cid, c in service.state["certificates"].items():
                    if qs.get("student") and c["student"] != qs["student"]:
                        continue
                    if qs.get("status") and c["status"] != qs["status"]:
                        continue
                    rows.append({"id": cid, "student": c["student"], "status": c["status"],
                                 "issued_at": c.get("issued_at")})
                return self._ok({"certificates": rows})
            m = re.fullmatch(r"/api/certificates/([A-Za-z0-9_.-]+)/basis", path)
            if m:
                return self._ok(service.certificate_basis(m.group(1)))
            m = re.fullmatch(r"/api/certificates/([A-Za-z0-9_.-]+)", path)
            if m:
                return self._ok(service.certificate_basis(m.group(1)))
            m = re.fullmatch(r"/api/students/([A-Za-z0-9_.-]+)/verification", path)
            if m:
                return self._ok(service.evaluate_student(m.group(1)))
            m = re.fullmatch(r"/api/students/([A-Za-z0-9_.-]+)/passport", path)
            if m:
                return self._ok(service.passport(m.group(1)))
            m = re.fullmatch(r"/api/identity/([A-Za-z0-9_.-]+)", path)
            if m:
                return self._ok(service.identity_view(m.group(1)))
            m = re.fullmatch(r"/api/cohorts/([A-Za-z0-9_.-]+)/roster", path)
            if m:
                return self._ok({"cohort": m.group(1), "roster": service.roster(m.group(1))})
            if path == "/api/substitutions":
                return self._ok({"substitutions": service.list_substitutions()})
            m = re.fullmatch(r"/api/partner/exchanges/([A-Za-z0-9_.-]+)", path)
            if m:
                partner = qs.get("partner") or self.headers.get("X-Actor-Id")
                if not partner:
                    raise StudyVisitError("partner required (?partner= or X-Actor-Id)")
                return self._ok(service.partner_exchange_view(m.group(1), partner))
            if path == "/api/partner/worklist":
                partner = qs.get("partner") or self.headers.get("X-Actor-Id")
                if not partner:
                    raise StudyVisitError("partner required")
                return self._ok(service.partner_worklist(partner, cohort=qs.get("cohort")))
            self._send(404, {"code": "not_found", "path": path})

        def _dispatch_post(self):
            path = urlsplit(self.path).path.rstrip("/")
            body = self._body()
            actor = self.headers.get("X-Actor-Id") or self.headers.get("X-Actor-Role")

            m = re.fullmatch(r"/api/cohorts/([A-Za-z0-9_.-]+)/students/batch", path)
            if m:
                return self._send(201, service.enroll_students(
                    m.group(1), body.get("student_ids") or body.get("students") or [],
                    actor=actor or "teacher", batch_ref=body.get("batch_ref")))
            m = re.fullmatch(r"/api/cohorts/([A-Za-z0-9_.-]+)/adoption", path)
            if m:
                return self._ok(service.register_adoption(
                    m.group(1), modules=body.get("modules"), point_policy=body.get("point_policy"),
                    minimum_evidence=body.get("minimum_evidence"), actor=actor or "school"))
            m = re.fullmatch(r"/api/students/([A-Za-z0-9_.-]+)/reclass", path)
            if m:
                return self._ok(service.reclass_student(m.group(1), body["to_cohort"], actor=actor or "teacher"))
            if path == "/api/identity/matches":
                return self._ok(service.confirm_identity(
                    body["participant"], body["student"], matched_by=body.get("matched_by") or actor,
                    minute=body.get("minute"), allow_unlisted=bool(body.get("allow_unlisted"))))
            if path == "/api/evidence":
                result = service.submit_evidence(
                    evidence_id=body.get("evidence_id") or body.get("receipt"),
                    student=body.get("student"), participant=body.get("participant"),
                    partner=body.get("partner") or body.get("site"), source=body.get("source", "api"),
                    minute=body.get("minute"), goal=body.get("goal"), course=body.get("course"),
                    supports=body.get("supports"), action_id=body.get("action_id"),
                    evidence_kind=body.get("evidence_kind"), cohort=body.get("cohort"))
                return self._send(201 if result["created"] else 200, result)
            if path == "/api/evidence/batch":
                return self._ok(service.submit_evidence_batch(body.get("items") or []))
            if path == "/api/corrections":
                return self._ok(service.apply_correction(
                    supersedes=body["supersedes"], actual=body["actual"], student=body.get("student"),
                    minute=body.get("minute"), correction_id=body.get("correction_id"), actor=actor or "correction"))
            if path == "/api/academic/substitutions":
                return self._send(201, service.add_substitution(
                    body["course"], body.get("supports", []), cohorts=body.get("cohorts"),
                    effective_round=body.get("effective_round"), code=body.get("code"),
                    status=body.get("status", "approved"), decided_minute=body.get("decided_minute"),
                    effective_from_minute=body.get("effective_from_minute"),
                    retroactive=bool(body.get("retroactive")), actor=actor or "academic"))
            if path == "/api/certificates":
                cid = body.get("certificate") or body.get("certificate_id")
                result = service.issue_certificate(
                    cid, body["student"], body.get("evidence") or body.get("evidence_ids") or [],
                    issued_at=body.get("issued_at"), actor=actor or "school", force=bool(body.get("force")))
                return self._send(201 if result["created"] else 200, result)
            m = re.fullmatch(r"/api/certificates/([A-Za-z0-9_.-]+)/reaffirm", path)
            if m:
                return self._ok(service.reaffirm_certificate(m.group(1), actor=actor or "school",
                                                            minute=body.get("minute")))
            m = re.fullmatch(r"/api/certificates/([A-Za-z0-9_.-]+)/revoke", path)
            if m:
                return self._ok(service.revoke_certificate(m.group(1), body.get("reason", ""), actor=actor or "school"))
            m = re.fullmatch(r"/api/certificates/([A-Za-z0-9_.-]+)/receipts", path)
            if m:
                return self._ok(service.bind_receipt(
                    m.group(1), reference=body["reference"], operation=body["operation"],
                    recipient=body["recipient"], receipt=body.get("receipt", {}),
                    delivery=body.get("delivery", "applied"), late=bool(body.get("late")),
                    arrived_minute=body.get("arrived_minute")))
            if path == "/api/transfer":
                return self._ok(service.transfer(
                    body.get("subjects") or body.get("students") or [], body["to_partner"],
                    minute=body.get("minute"), actor=actor or "teacher"))
            if path == "/api/exchanges":
                return self._send(201, service.create_exchange(
                    partner=body["partner"], subjects=body["subjects"], cohort=body.get("cohort"),
                    note=body.get("note"), actor=actor or "school"))
            self._send(404, {"code": "not_found", "path": path})

        def _dispatch_patch(self):
            path = urlsplit(self.path).path.rstrip("/")
            body = self._body()
            actor = self.headers.get("X-Actor-Id") or "academic"
            m = re.fullmatch(r"/api/academic/substitutions/([A-Za-z0-9_.-]+)", path)
            if m:
                result = service.adjust_substitution(
                    m.group(1), supports=body.get("supports"), cohorts=body.get("cohorts"),
                    status=body.get("status"), change=body.get("change"),
                    revision=body.get("revision"), minute=body.get("minute"), actor=actor)
                return self._ok(result)
            self._send(404, {"code": "not_found", "path": path})

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return httpd


API_INDEX = {
    "name": "studyvisit-backend",
    "endpoints": [
        "GET  /api/health",
        "POST /api/cohorts/{cohort}/adoption",
        "POST /api/cohorts/{cohort}/students/batch",
        "GET  /api/cohorts/{cohort}/roster",
        "POST /api/students/{id}/reclass",
        "GET  /api/students/{id}/verification",
        "GET  /api/students/{id}/passport",
        "POST /api/identity/matches",
        "GET  /api/identity/{token}",
        "POST /api/evidence",
        "POST /api/corrections",
        "GET/POST /api/substitutions ; PATCH /api/academic/substitutions/{key}",
        "POST /api/academic/substitutions",
        "POST /api/certificates",
        "GET  /api/certificates",
        "GET  /api/certificates/{id}/basis",
        "GET  /api/school/review-board",
        "POST /api/certificates/{id}/reaffirm|revoke|receipts",
        "POST /api/transfer",
        "POST /api/exchanges",
        "GET  /api/partner/worklist?partner=site-b",
        "GET  /api/partner/exchanges/{id}?partner=site-b",
    ],
}
