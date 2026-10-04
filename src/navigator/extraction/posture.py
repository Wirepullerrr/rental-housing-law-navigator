"""Legal source posture: what kind of source a document is, and what that implies.

Internal audit metadata only; the official RuleRecord schema is unchanged.

    codified_current_law         a current code section or consolidated law text
    enacted_session_law          an enacted act or chaptered bill as passed (session law)
    pending_bill                 bill or proposal text not yet enacted
    failed_bill                  a proposal shown as rejected, struck, vetoed or withdrawn
    bill_status_or_summary_page  a legislature's status/summary/history page for a bill
    official_explanatory_page    an official explanation or announcement of a law
    unknown

The model declares a posture with one verbatim passage showing it. The posture is
ESTABLISHED only if that passage verifies in the raw source; and a declared
codified_current_law is not established when the raw text itself records an
enactment date (find_base_dates), which marks session-law material. Otherwise the
established posture is unknown. Nothing is inferred from the URL.

Consequence (normalize.derive_status): only established codified current law lets an
enacted rule with no dated evidence be read as in force on every query date. For any
other posture such a rule is held for temporal resolution instead of being published
as in_force. The declared posture is also compared with each rule's enactment_status;
a mismatch is a review reason, never a silent correction.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from navigator.extraction.models import DocumentPosture
from navigator.extraction.quotes import verify_text
from navigator.extraction.source_view import SourceView

CODIFIED = "codified_current_law"
UNKNOWN = "unknown"
# enactment_status each posture implies for its records (None: any status may appear).
EXPECTED_STATUS = {CODIFIED: "enacted", "enacted_session_law": "enacted", "pending_bill": "pending",
                   "failed_bill": "failed", "bill_status_or_summary_page": None, "official_explanatory_page": None,
                   UNKNOWN: None}


def establish_posture(declared: Any, raw: str, view: SourceView, base_dates: list[dict[str, Any]]) -> dict[str, Any]:
    """Audit dict: declared posture, its evidence check, the established posture and why."""
    audit: dict[str, Any] = {"declared": None, "evidence": None, "evidence_check": None, "established": UNKNOWN,
                             "basis": None}
    if declared is None:
        audit["basis"] = "the response declares no document posture"
        return audit
    try:
        doc = DocumentPosture.model_validate(declared)
    except ValidationError:
        audit["basis"] = "the declared document posture is malformed"
        return audit
    audit.update(declared=doc.posture, evidence=doc.evidence)
    if doc.posture == UNKNOWN:
        audit["basis"] = "declared unknown"
        return audit
    if doc.evidence is None:
        audit["basis"] = f"declared {doc.posture} without evidence"
        return audit
    check = verify_text(doc.evidence, raw, view)
    audit["evidence_check"] = check.model_dump()
    if check.status == "failed":
        audit["basis"] = f"declared {doc.posture}, but its evidence is not source text ({check.reason})"
        return audit
    if doc.posture == CODIFIED and base_dates:
        audit["basis"] = (f"declared {CODIFIED}, but the text records an enactment date "
                          f"({base_dates[0]['text']!r}), which marks session-law material")
        return audit
    audit.update(established=doc.posture, basis="declared posture with verified evidence")
    return audit


def no_date_in_force(posture: dict[str, Any]) -> bool:
    return posture.get("established") == CODIFIED
