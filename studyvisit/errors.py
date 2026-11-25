"""Domain errors.  Each error carries a stable ``code`` and an HTTP status."""


class StudyVisitError(Exception):
    code = "bad_request"
    status = 400

    def __init__(self, message=None, *, details=None):
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details = details or {}

    def to_payload(self):
        body = {"code": self.code, "message": self.message}
        if self.details:
            body["details"] = self.details
        return body


class ValidationFailed(StudyVisitError):
    code = "invalid_request"
    status = 400


class NotFound(StudyVisitError):
    code = "not_found"
    status = 404


class PartnerNotAuthorized(StudyVisitError):
    code = "partner_not_authorized"
    status = 403


class IdentityAmbiguous(StudyVisitError):
    code = "identity_ambiguous"
    status = 409


class Conflict(StudyVisitError):
    code = "conflict"
    status = 409


class ReferenceReused(StudyVisitError):
    code = "reference_reused"
    status = 409


class CertificationBlocked(StudyVisitError):
    code = "certification_blocked"
    status = 422


class ReceiptUnavailable(StudyVisitError):
    code = "receipt_unavailable"
    status = 502
