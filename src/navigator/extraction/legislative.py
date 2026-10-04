"""Legislative status of proposals: pending vs failed is decided here, not by the model.

Policy (M3.2):
  ENACTED   an enacted record keeps the enacted / effective-date logic; this module does
            not apply to it.
  FAILED    a source line records an explicit terminal disposition of the bill (the whole
            line is e.g. "No further action taken", "Withdrawn", "Leave to withdraw",
            "Rejected", "Defeated", "Vetoed"; a qualified line such as "No further action
            taken on the extension ..." is not a disposition of the bill), OR the bill
            belongs to a legislative session that ended before the query date without
            enactment (Massachusetts only, below).
  PENDING   a bill of a session still open on the query date, not enacted, with no
            terminal disposition. A study order by itself is NOT terminal.
A later-session refiled or similar bill is a separate legislative object: only the
bill's OWN session label (the first one on its page) is used.

Massachusetts session resolver (MA_SESSION_RESOLVER), deliberately narrow; not a
calendar engine for other legislatures. It applies only when the manifest jurisdiction
is in MA and the page shows a General Court label such as "193rd (2023 - 2024)" or
"194th (Current)". The n-th General Court sits for two calendar years, starting
2023 + 2 * (n - 193) (193rd: 2023-2024); written years must agree with that, or the
label is not used. A session counts as ended only from February 1 after its second
year (its successor convenes in early January), so a query date in the transition is
never read as "ended". "(Current)" on the page reflects retrieval, not the query date,
and is not relied on.

Outcomes (status_basis): explicit_terminal_action, session_expired,
current_session_pending, enacted, or model_status_with_evidence when no
deterministic basis exists (the v6 behaviour: the model's pending/failed with verified
evidence). A text that records an enactment while the record says pending/failed is a
conflict: the status is left undetermined (held) for review.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from navigator.extraction.temporal import find_base_dates

MA_SESSION_RESOLVER = "ma-general-court-session/v1"
_SESSION = re.compile(r"\b(?P<n>\d{3})(?:st|nd|rd|th)\s*\(\s*(?:(?P<y1>\d{4})\s*[-–]\s*(?P<y2>\d{4})|Current)\s*\)",
                      re.IGNORECASE)
_TERMINAL = re.compile(r"^\s*(?:no further action(?: taken)?|withdrawn|leave to withdraw(?: granted)?|rejected|"
                       r"defeated|vetoed)\s*\.?\s*$", re.IGNORECASE | re.MULTILINE)
_ENACTMENT_LINE = re.compile(r"^\s*(?:signed by the governor\b.*|chapter \d+ of the acts of \d{4}\b.*)$",
                             re.IGNORECASE | re.MULTILINE)


def state_of(jurisdiction: str) -> str:
    """'MA' -> 'MA'; 'Boston, MA' -> 'MA'."""
    return jurisdiction.rsplit(",", 1)[-1].strip()


def find_session(raw: str, jurisdiction: str) -> dict[str, Any] | None:
    """The bill's own Massachusetts General Court session, from the first label on the page."""
    if state_of(jurisdiction) != "MA":
        return None
    labels = list(_SESSION.finditer(raw))
    if not labels:
        return None
    m = labels[0]
    n = int(m.group("n"))
    start = 2023 + 2 * (n - 193)
    session = {"resolver": MA_SESSION_RESOLVER, "jurisdiction": jurisdiction, "general_court": n,
               "label": m.group(0), "raw_start": m.start(), "raw_end": m.end(), "years": [start, start + 1],
               "ends_by": date(start + 2, 2, 1).isoformat(), "consistent": True,
               "other_labels": [x.group(0) for x in labels[1:]]}
    if m.group("y1") and [int(m.group("y1")), int(m.group("y2"))] != session["years"]:
        session["consistent"] = False
    return session


def decide(enactment_status: str, raw: str, session: dict[str, Any] | None, as_of: date) -> dict[str, Any]:
    """Final status and its basis for a pending/failed record (module docstring)."""
    audit: dict[str, Any] = {"model_status": enactment_status, "query_date": as_of.isoformat(), "session": session,
                             "terminal_action": None, "enactment_evidence": None, "status": enactment_status,
                             "status_basis": "enacted" if enactment_status == "enacted" else None, "conflict": None}
    if enactment_status == "enacted":
        return audit
    enactment = [b["text"] for b in find_base_dates(raw)] + [m.group(0).strip() for m in _ENACTMENT_LINE.finditer(raw)]
    if enactment:
        audit.update(enactment_evidence=enactment[0], status=None, status_basis=None,
                     conflict=f"the text records an enactment ({enactment[0]!r}) but the record is {enactment_status}")
        return audit
    if terminal := _TERMINAL.search(raw):
        audit.update(terminal_action=terminal.group(0).strip(), status="failed",
                     status_basis="explicit_terminal_action")
    elif session is not None and session["consistent"]:
        if as_of >= date.fromisoformat(session["ends_by"]):
            audit.update(status="failed", status_basis="session_expired")
        elif as_of.year in session["years"]:
            audit.update(status="pending", status_basis="current_session_pending")
    if audit["status_basis"] is None:
        audit["status_basis"] = "model_status_with_evidence"
    elif audit["status"] != enactment_status:
        audit["conflict"] = (f"model said {enactment_status}; decided {audit['status']} "
                             f"({audit['status_basis']})")
    return audit
