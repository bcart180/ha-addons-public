"""Optional content-free GDIT presence notice (0.8.8).

Off unless ``gdit_work_copy`` is on.  A notice is considered only after the
Proton notice (sent, or would-send while dry_run is on), and only when
``person.bryson`` is in the Home Assistant Work zone (``zone.work``).  The
body is content-free: missed call, voicemail left yes/no, and the 24-hour
ET time.  No name, number, transcript, audio, or link.  The redacted
transcript copy path is retired.  Logs are reason codes only; names,
numbers, and the work address are never logged.
"""
from __future__ import annotations


WORK_PERSON = "person.bryson"
WORK_ZONE_ID = "zone.work"
# person.<id> state when inside zone.work, whose friendly name is "Work".
# Comparison is case-insensitive.  "home", "not_home", and other zones do not pass.
WORK_ZONE_STATES = frozenset({"work"})
# Single source for the pinned work recipient.  Do not duplicate this value
# in docs, changelog, or other modules.
GDIT_RECIPIENT = "bryson.carter@gdit.com"
GDIT_STALE_S = 15 * 60

# Reason codes.  Logs and tests only ever carry one of these.
REASONS = frozenset({
    "ok",
    "disabled",
    "no_proton",
    "work_unknown",
    "not_at_work",
    "dry_run",
    "stale",
})


def work_gate(enabled: bool, state: str | None) -> str:
    """Return a reason code.  ``ok`` means person.bryson is in zone.work.

    Unknown, unavailable, empty, or an unreachable Home Assistant read is
    fail-closed (``work_unknown``).  A known state that is not the Work
    zone, including ``home`` and ``not_home``, is ``not_at_work``.
    """
    if not enabled:
        return "disabled"
    if state is None:
        return "work_unknown"
    normalized = str(state).strip().casefold()
    if normalized in ("", "unknown", "unavailable", "none"):
        return "work_unknown"
    if normalized in WORK_ZONE_STATES:
        return "ok"
    return "not_at_work"


def decide(enabled: bool, proton: str, state: str | None) -> str:
    """Pick the single reason code for a GDIT notice attempt."""
    if proton not in ("sent", "would-send"):
        return "no_proton"
    gate = work_gate(enabled, state)
    if gate != "ok":
        return gate
    if proton == "would-send":
        return "dry_run"
    return "ok"


def job_is_stale(job: dict | None, now: float | None = None) -> bool:
    """True when a GDIT job is older than GDIT_STALE_S (about 15 minutes)."""
    import time

    created = float((job or {}).get("created_at") or 0)
    clock = time.time() if now is None else float(now)
    if created <= 0:
        return True
    return (clock - created) > GDIT_STALE_S


def voicemail_left(row: dict | None) -> bool:
    """True when the call left a recording."""
    return bool((row or {}).get("has_audio"))
