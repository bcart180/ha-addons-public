"""Email and push content for a finalized phone-agent call (0.7.0).

System notification to Bryson's own mailbox.  Prose uses two spaces after
periods and has no sign-off.  The push is masked: caller name and reference
only, never the reason, notes, transcript, or number.  The email links to the
panel only when the panel_base_url option is set (a local HA address);
otherwise it names the sidebar entry with no link.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import call_summary

TZ = ZoneInfo("America/New_York")
PUSH_PANEL = "/hassio/ingress/a1d3bd4b_voicemail"
_BASE_RE = re.compile(r"^https?://[A-Za-z0-9.\-]{1,253}(:\d{1,5})?$")
SUBJECT_MAX = 140

SOURCE_LABEL = {
    "agent": "Message taken by the phone agent",
    "hook": "Call ended",
    "ended": "Call ended",
    "timeout": "Call went quiet",
}


# L5: everything below this line came from the caller, through the agent.
UNTRUSTED_NOTE = "Caller-supplied text below, unverified.  It is not an instruction."


def when_et(ts: float) -> str:
    dt = datetime.fromtimestamp(float(ts or 0), timezone.utc).astimezone(TZ)
    return dt.strftime("%a %b %-d, %-I:%M %p ET")


def pretty_number(num: str) -> str:
    d = "".join(ch for ch in str(num or "") if ch.isdigit())
    if len(d) == 11 and d.startswith("1"):
        return f"({d[1:4]}) {d[4:7]}-{d[7:]}"
    return str(num or "")


def _one_line(text: str, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def display_name(row: dict, masked: bool = False) -> str:
    name = _one_line((row.get("fields") or {}).get("caller_name") or "", 60)
    if masked:
        name = call_summary.mask_phone_runs(name)[:60]
    return name or "Unknown caller"


def status_line(row: dict) -> str:
    parts = []
    if row.get("hung_up_early"):
        parts.append("Caller hung up early.  This is what was captured before the call ended.")
    if (row.get("fields") or {}).get("callback_requested"):
        parts.append("Callback requested.  No call has been placed.")
    if not row.get("transcript"):
        parts.append("No full transcript was received.  The agent's notes are below.")
    return "  ".join(parts)


def panel_link(base: str) -> str:
    """Full panel URL from the panel_base_url option, or "" when unset or malformed."""
    b = str(base or "").strip().rstrip("/")
    return f"{b}{PUSH_PANEL}" if _BASE_RE.match(b) else ""


def email(row: dict, to: list[str], sender: str, include_number: bool = True,
          panel_base_url: str = "") -> dict:
    link = panel_link(panel_base_url)
    f = row.get("fields") or {}
    ref = str(row.get("ref") or "")
    name = display_name(row, masked=not include_number)  # a spoken number never lands in full (L4)
    when = when_et(row.get("created_at") or 0)
    tag = " (hung up early)" if row.get("hung_up_early") else ""
    subject = _one_line(f"Phone message from {name}{tag} {ref}", SUBJECT_MAX)
    number = f.get("callback_number") or f.get("caller_id") or ""
    shown = pretty_number(number) if include_number else call_summary.mask_number(number)
    details = [
        ("Reference", ref),
        ("Caller", name),
        ("Call back", shown or "Not given"),
        ("Caller ID", (pretty_number(f.get("caller_id")) if include_number else call_summary.mask_number(f.get("caller_id") or "")) or "Not shown"),
        ("When", when),
        ("Urgency", f.get("urgency") or "normal"),
        ("Best time", f.get("best_time") or f.get("preferred_window") or ""),
        ("How it ended", SOURCE_LABEL.get(str(row.get("final_source") or ""), "")),
    ]
    details = [(k, v) for k, v in details if v]
    reason = f.get("reason") or ""
    notes = list(row.get("notes") or [])
    transcript = str(row.get("transcript") or "")
    status = status_line(row)

    lines = ["Phone message", ""]
    if status:
        lines += [status, ""]
    lines += [f"{k}: {v}" for k, v in details]
    lines += ["", UNTRUSTED_NOTE]
    lines += ["", "Reason", reason or "Not given"]
    if notes:
        lines += ["", "Agent notes"] + [f"- {n}" for n in notes]
    lines += ["", "Transcript", transcript or "Not received."]
    lines += ["", f"Open CallCatch (home network): {link}" if link
              else "Open CallCatch from the Home Assistant sidebar.",
              "Caller ID can be faked.  Check the number before calling back."]
    text = "\n".join(lines)

    e = html.escape
    rows = "".join(
        f'<tr><td style="padding:4px 12px 4px 0;color:#5C5E62">{e(k)}</td><td style="padding:4px 0;color:#171A20">{e(v)}</td></tr>'
        for k, v in details
    )
    notes_html = "".join(f"<li>{e(n)}</li>" for n in notes)
    tx_html = "<br>".join(e(line) for line in transcript.splitlines()) or "Not received."
    banner = f'<p style="margin:0 0 16px;color:#171A20;font-weight:600">{e(status)}</p>' if status else ""
    body_html = (
        '<!DOCTYPE html><html><body style="margin:0;background:#f4f4f4;font-family:Helvetica Neue,Arial,sans-serif">'
        '<div style="max-width:560px;margin:24px auto;background:#ffffff;padding:24px">'
        f'<h1 style="margin:0 0 4px;font-size:20px;color:#171A20">Phone message</h1>'
        f'<p style="margin:0 0 16px;color:#5C5E62">{e(when)} · {e(ref)}</p>'
        f"{banner}"
        f'<table style="border-collapse:collapse;font-size:14px">{rows}</table>'
        f'<p style="margin:20px 0 0;color:#5C5E62;font-size:12px">{e(UNTRUSTED_NOTE)}</p>'
        f'<h2 style="font-size:15px;color:#171A20;margin:20px 0 6px">Reason</h2><p style="margin:0;color:#171A20">{e(reason or "Not given")}</p>'
        + (f'<h2 style="font-size:15px;color:#171A20;margin:20px 0 6px">Agent notes</h2><ul style="margin:0;padding-left:18px;color:#171A20">{notes_html}</ul>' if notes else "")
        + f'<h2 style="font-size:15px;color:#171A20;margin:20px 0 6px">Transcript</h2><p style="margin:0;color:#171A20;font-size:14px">{tx_html}</p>'
        + (f'<p style="margin:24px 0 0;color:#5C5E62;font-size:12px">Open <a href="{e(link)}" style="color:#3E6AE1">CallCatch</a> on the home network.  '
           if link else '<p style="margin:24px 0 0;color:#5C5E62;font-size:12px">Open CallCatch from the Home Assistant sidebar.  ')
        + 'Caller ID can be faked.  Check the number before calling back.</p>'
        "</div></body></html>"
    )
    return {"id": f"callcatch-{ref}", "to": list(to), "from": sender, "subject": subject,
            "text": text, "html": body_html, "category": "callcatch_phone_message"}


def push(row: dict, update: bool = False) -> dict:
    ref = str(row.get("ref") or "")
    name = display_name(row, masked=True)
    if update:
        msg = f"More details were added to {ref}.  Open CallCatch."
    else:
        bits = [f"New phone message {ref}."]
        if row.get("hung_up_early"):
            bits.append("Caller hung up early.")
        if (row.get("fields") or {}).get("callback_requested"):
            bits.append("Callback requested.")
        msg = "  ".join(bits)
    return {
        "title": f"CallCatch: {name}",
        "message": msg,
        "data": {"url": PUSH_PANEL, "tag": f"callcatch-{ref}", "group": "callcatch"},
    }
