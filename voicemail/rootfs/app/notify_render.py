"""Email and push content for a finalized phone-agent call (0.8.6).

System notification to Bryson's own mailbox.  Prose uses two spaces after
periods and has no sign-off.  The push is masked: caller name and reference
only, never the reason, notes, transcript, or number.  The email is black-
friendly for Proton on iPhone and links to the panel only when the
panel_base_url option is set (a local HA address); otherwise it names the
sidebar entry with no link.
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
    "recording": "Call ended",
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



def last4(num: str) -> str:
    d = "".join(ch for ch in str(num or "") if ch.isdigit())
    return d[-4:] if len(d) >= 4 else ""


def duration_label(seconds) -> str:
    try:
        total = int(float(seconds or 0))
    except (TypeError, ValueError):
        total = 0
    if total < 0:
        total = 0
    minutes, secs = divmod(total, 60)
    parts = []
    if minutes:
        parts.append("1 minute" if minutes == 1 else f"{minutes} minutes")
    if secs or not minutes:
        parts.append("1 second" if secs == 1 else f"{secs} seconds")
    return " ".join(parts)


def time_et_short(ts: float) -> str:
    dt = datetime.fromtimestamp(float(ts or 0), timezone.utc).astimezone(TZ)
    return dt.strftime("%-I:%M %p ET")


def _nbsp_sentences(text: str) -> str:
    """Keep two spaces after sentence punctuation in HTML."""
    out = str(text or "")
    for mark in (".", "?", "!"):
        out = out.replace(mark + "  ", mark + "  ")
    return out


def _caller_id_mark(name: str, row: dict) -> str:
    """When the name came from carrier CNAM, mark it as caller ID."""
    if (row.get("fields") or {}).get("caller_name_source") != "cnam":
        return name
    if not name or name in ("Unknown caller",) or name.startswith("Number ending "):
        return name
    if name.endswith(" (caller ID)"):
        return name
    return f"{name} (caller ID)"


def header_name(row: dict, include_number: bool = True) -> str:
    name = display_name(row, masked=not include_number)
    if name != "Unknown caller":
        return _caller_id_mark(name, row)
    num = (row.get("fields") or {}).get("callback_number") or (row.get("fields") or {}).get("caller_id") or ""
    tail = last4(num)
    return f"Number ending {tail}" if tail else "Unknown caller"

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
    if row.get("verbatim_at"):
        parts.append("The transcript is the carrier recording, word for word.")
    elif row.get("capture_failed"):
        parts.append("The carrier recording could not be transcribed.  The call is saved.")
    elif not row.get("transcript"):
        if row.get("turns"):
            parts.append("The transcript below was written by the phone agent during the call and may paraphrase.  "
                         "The recording is in the xAI console.")
        else:
            parts.append("No full transcript was received.  The agent's notes are below.")
    return "  ".join(parts)


def transcript_lines(row: dict, include_number: bool = True) -> list[str]:
    """Hook transcript if one arrived, else the agent-written turn log.

    Caller text, so phone-number runs are masked to the last 4 whenever the
    email is set to hide full numbers."""
    raw = str(row.get("transcript") or "")
    if raw:
        lines = raw.splitlines()
    else:
        lines = []
        for t in row.get("turns") or []:
            label = "Caller" if t.get("speaker") == "caller" else "Agent"
            lines.append(f"{label}: {_one_line(t.get('text') or '', 500)}")
    if not include_number:
        lines = [call_summary.mask_phone_runs(line) for line in lines]
    return [line for line in lines if line.strip()]


def summary_text(row: dict, include_number: bool = True) -> str:
    text = _one_line(row.get("summary") or "", 1200)
    return call_summary.mask_phone_runs(text) if (text and not include_number) else text


def call_started(row: dict) -> str:
    """Platform call start in Eastern, or the raw value when its format is unknown."""
    ts = float(row.get("call_start_ts") or 0)
    if ts:
        return when_et(ts)
    raw = _one_line(row.get("call_start") or "", 64)
    return f"{raw} (as sent by the platform)" if raw else ""


def console_hint(row: dict) -> str:
    """How to find this call in the xAI console voice logs.  Last 4 only."""
    caller = call_summary.mask_number((row.get("fields") or {}).get("caller_id") or "")
    if not caller:
        return ""
    started = call_started(row) or when_et(row.get("created_at") or 0)
    return f"Find it in the xAI console voice logs: caller number ending {caller.lstrip('•')}, call started {started}."


def panel_link(base: str, ref: str = "") -> str:
    """Full panel URL from the panel_base_url option, or "" when unset or malformed."""
    b = str(base or "").strip().rstrip("/")
    if not _BASE_RE.match(b):
        return ""
    url = f"{b}{PUSH_PANEL}"
    token = "".join(ch for ch in str(ref or "") if ch.isalnum() or ch in "-_")
    return f"{url}#{token}" if token else url


def _duration_of(row: dict) -> int:
    for key in ("duration_s", "duration_sec"):
        if row.get(key) not in (None, ""):
            try:
                return max(0, int(float(row[key])))
            except (TypeError, ValueError):
                return 0
    return 0


def _has_audio(row: dict) -> bool:
    return bool(row.get("recording_file") or row.get("has_audio") or row.get("verbatim_at"))


def email(row: dict, to: list[str], sender: str, include_number: bool = True,
          panel_base_url: str = "") -> dict:
    f = row.get("fields") or {}
    ref = str(row.get("ref") or "")
    link = panel_link(panel_base_url, ref)
    name = header_name(row, include_number=include_number)
    when = when_et(row.get("created_at") or row.get("call_start_ts") or 0)
    when_short = time_et_short(row.get("created_at") or row.get("call_start_ts") or 0)
    subject = _one_line(f"{name} called at {when_short}", SUBJECT_MAX)
    caller_num = f.get("caller_id") or ""
    callback_num = f.get("callback_number") or ""
    tail = last4(caller_num) or last4(callback_num)
    shown = pretty_number(callback_num or caller_num) if include_number else ""
    callback_tail = last4(callback_num)
    early = "Yes" if row.get("hung_up_early") else "No"
    length = duration_label(_duration_of(row))
    reason = f.get("reason") or ""
    notes = list(row.get("notes") or [])
    best_time = f.get("best_time") or f.get("preferred_window") or ""
    if not include_number:
        reason = call_summary.mask_phone_runs(reason)
        notes = [call_summary.mask_phone_runs(n) for n in notes]
        best_time = call_summary.mask_phone_runs(best_time)
    tx_lines = transcript_lines(row, include_number)
    summary = summary_text(row, include_number)
    status = status_line(row)

    lines = ["CallCatch", "", name]
    if not str(name).startswith("Number ending "):
        lines.append(f"Number ending {tail}" if tail else "Number not shown")
    lines += ["", f"Time: {when}", f"Duration: {length}", f"Early hangup: {early}"]
    started = call_started(row)
    if started:
        lines.append(f"Call started: {started}")
    conv = _one_line(row.get("conversation_id") or "", 100)
    if conv:
        lines.append(f"xAI conversation: {conv}")
    if shown:
        lines.append(f"Call back: {shown}")
    elif callback_tail:
        lines.append(f"Call back ending {callback_tail}")
    if best_time:
        lines.append(f"Best time: {best_time}")
    if status:
        lines += ["", status]
    lines += ["", UNTRUSTED_NOTE]
    if summary:
        lines += ["", "Summary", summary]
    if reason:
        lines += ["", "Reason", reason]
    if notes:
        lines += ["", "Notes"] + [f"- {n}" for n in notes]
    lines += ["", "Transcript"]
    lines += tx_lines or ["Not received."]
    if _has_audio(row):
        lines += ["", "The recording is saved in CallCatch.  It is not attached to this email."]
    else:
        lines += ["", "No recording was saved."]
    hint = console_hint(row)
    if hint:
        lines += ["", hint]
    lines += ["", f"Open this call in CallCatch on the home network: {link}" if link
              else "Open this call from the Home Assistant sidebar.",
              "Caller ID can be faked.  Check the number before calling back."]
    text_body = "\n".join(lines)

    e = html.escape
    def cell(k, v):
        return (f'<tr><td style="padding:6px 12px 6px 0;color:#8a8a8a;width:42%;vertical-align:top">{e(k)}</td>'
                f'<td style="padding:6px 0;color:#f2f2f2">{e(v)}</td></tr>')
    rows = cell("Time", when) + cell("Duration", length) + cell("Early hangup", early)
    if started:
        rows += cell("Call started", started)
    if conv:
        rows += cell("xAI conversation", conv)
    if shown:
        rows += cell("Call back", shown)
    elif callback_tail:
        rows += cell("Call back", f"ending {callback_tail}")
    if best_time:
        rows += cell("Best time", best_time)
    notes_html = "".join(f"<li>{e(n)}</li>" for n in notes)
    tx_html = []
    for line in tx_lines:
        label, sep, rest = line.partition(": ")
        if sep and label in ("Caller", "Assistant", "Agent", "House"):
            shown_label = "Caller" if label == "Caller" else "Assistant"
            color = "#f2f2f2" if shown_label == "Caller" else "#3E6AE1"
            tx_html.append(
                f'<p style="margin:0 0 14px;color:#f2f2f2;font-size:15px;line-height:1.45">'
                f'<span style="color:{color};font-weight:600">{e(shown_label)}</span><br>'
                f'{_nbsp_sentences(e(rest))}</p>'
            )
        else:
            tx_html.append(
                f'<p style="margin:0 0 14px;color:#f2f2f2;font-size:15px;line-height:1.45">'
                f'{_nbsp_sentences(e(line))}</p>'
            )
    transcript_html = "".join(tx_html) or '<p style="margin:0;color:#f2f2f2">Not received.</p>'
    banner = (f'<p style="margin:0 0 16px;color:#f2f2f2;font-size:14px">{e(status)}</p>' if status else "")
    audio_note = ("The recording is saved in CallCatch.  It is not attached to this email."
                  if _has_audio(row) else "No recording was saved.")
    link_html = (f'<a href="{e(link)}" style="color:#3E6AE1;text-decoration:none">Open this call in CallCatch</a>'
                 ' <span style="color:#8a8a8a">on the home network.</span>'
                 if link else "Open this call from the Home Assistant sidebar.")
    body_html = (
        '<!DOCTYPE html><html><body style="margin:0;background:#000000;font-family:-apple-system,Helvetica Neue,Arial,sans-serif;color:#f2f2f2">'
        '<div style="max-width:560px;margin:0 auto;background:#000000;padding:22px 20px 32px">'
        '<div style="font-size:11px;color:#8a8a8a;letter-spacing:0.10em;text-transform:uppercase">CallCatch</div>'
        f'<h1 style="margin:8px 0 4px;font-size:24px;line-height:1.25;color:#f2f2f2;font-weight:600">{e(name)}</h1>'
        f'<p style="margin:0 0 18px;color:#c8c8c8;font-size:14px">{"Number ending " + e(tail) if tail else "Number not shown"}</p>'
        f'<table role="presentation" style="border-collapse:collapse;width:100%;font-size:14px;margin:0 0 22px">{rows}</table>'
        f"{banner}"
        f'<p style="margin:0 0 14px;color:#8a8a8a;font-size:12px;line-height:1.4">{e(UNTRUSTED_NOTE)}</p>'
        + (f'<h2 style="margin:0 0 8px;font-size:13px;color:#8a8a8a;letter-spacing:0.08em;text-transform:uppercase;font-weight:600">Summary</h2>'
           f'<p style="margin:0 0 22px;color:#f2f2f2;font-size:15px;line-height:1.45">{_nbsp_sentences(e(summary))}</p>' if summary else "")
        + (f'<h2 style="margin:0 0 8px;font-size:13px;color:#8a8a8a;letter-spacing:0.08em;text-transform:uppercase;font-weight:600">Reason</h2>'
           f'<p style="margin:0 0 22px;color:#f2f2f2;font-size:15px;line-height:1.45">{_nbsp_sentences(e(reason))}</p>' if reason else "")
        + (f'<h2 style="margin:0 0 8px;font-size:13px;color:#8a8a8a;letter-spacing:0.08em;text-transform:uppercase;font-weight:600">Notes</h2>'
           f'<ul style="margin:0 0 22px;padding-left:18px;color:#f2f2f2">{notes_html}</ul>' if notes else "")
        + '<h2 style="margin:0 0 8px;font-size:13px;color:#8a8a8a;letter-spacing:0.08em;text-transform:uppercase;font-weight:600">Transcript</h2>'
        + transcript_html
        + f'<p style="margin:22px 0 10px;color:#c8c8c8;font-size:14px;line-height:1.45">{_nbsp_sentences(e(audio_note))}</p>'
        + (f'<p style="margin:0 0 16px;color:#8a8a8a;font-size:12px;line-height:1.4">{e(hint)}</p>' if hint else "")
        + f'<p style="margin:0 0 22px;font-size:14px;line-height:1.45">{link_html}</p>'
        + '<p style="margin:0;color:#5a5a5a;font-size:12px;line-height:1.4">Caller ID can be faked.&nbsp; Check the number before calling back.</p>'
        "</div></body></html>"
    )
    return {"id": f"callcatch-{ref}", "to": list(to), "from": sender, "subject": subject,
            "text": text_body, "html": body_html, "category": "callcatch_phone_message"}


def push(row: dict, update: bool = False) -> dict:
    ref = str(row.get("ref") or "")
    name = _caller_id_mark(display_name(row, masked=True), row)
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



def gdit_name_source(row: dict) -> str:
    """Identity lives under fields (apply_caller_identity); top-level is ignored."""
    f = row.get("fields") if isinstance(row.get("fields"), dict) else {}
    return str(f.get("caller_name_source") or "")


def gdit_display_name(row: dict) -> str:
    """Contact name only.  CNAM, spoken, and unusable names become "A caller"."""
    import contacts_sync

    f = row.get("fields") if isinstance(row.get("fields"), dict) else {}
    if gdit_name_source(row) != "contact":
        return "A caller"
    name = str(f.get("caller_name") or "").strip()
    if not contacts_sync.name_is_usable(name):
        return "A caller"
    return name


def gdit_email(row: dict) -> dict:
    """Redacted work-hours copy.  No transcript, summary, number, audio, or link."""
    from gdit_copy import GDIT_RECIPIENT

    name = gdit_display_name(row)
    when = time_et_short(row.get("created_at") or 0)
    subject = f"{name} called at {when}"[:SUBJECT_MAX]
    text = f"{name} called at {when}.\n\nSee Proton for the full message."
    safe_name = html.escape(name)
    safe_when = html.escape(when)
    body_html = (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
        '<body style="margin:0;background:#ffffff;color:#111111;'
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif\">"
        '<div style="max-width:520px;margin:0 auto;padding:24px 18px">'
        f'<p style="margin:0 0 14px;font-size:16px;line-height:1.45">{safe_name} called at {safe_when}.&nbsp;</p>'
        '<p style="margin:0;font-size:16px;line-height:1.45">See Proton for the full message.</p>'
        "</div></body></html>"
    )
    ref = str(row.get("ref") or "")
    return {
        "id": f"callcatch-gdit-{ref}",
        "to": [GDIT_RECIPIENT],
        "from": "",
        "subject": subject,
        "text": text,
        "html": body_html,
        "category": "callcatch_gdit_notice",
    }
