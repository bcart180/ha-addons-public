"""Allow / block lists and STIR/SHAKEN parsing for CallCatch Voice.

Lists live in /data/trust_lists.json (the add-on's own /data). They are
small JSON, written with a temp file + fsync + rename, so a crash cannot
leave half a file.

Verdicts:

* ``block``  number is on the block list. The voice webhook answers <Reject>.
* ``allow``  number is on the allow list. The agent skips the screening
             questions and just takes the message.
* ``screen`` everything else (the default path, unchanged).

STIR/SHAKEN is a signal only. It is stored with the call and shown in the
inbox. It never produces ``block`` by itself.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
import threading

LISTS = ("allow", "block")
MAX_ENTRIES = 500
NOTE_MAX = 80
VERSTAT_MAX = 64
_VERSTAT_RE = re.compile(r"^[A-Za-z0-9\-]{1,64}$")
_PASSED_RE = re.compile(r"^TN-Validation-Passed-([ABC])(?:-|$)", re.I)
_FAILED_RE = re.compile(r"^TN-Validation-Failed(?:-([ABC]))?(?:-|$)", re.I)


def number_key(number) -> str:
    """Digits-only key. NANP numbers compare on their last 10 digits."""
    raw = str(number or "").strip()
    d = re.sub(r"\D", "", raw)
    if raw.startswith("+") and not raw.startswith("+1"):
        # Non-NANP: keep every digit (country code included) so a 10-digit
        # international number never collides with a US number.
        # The "+" stays in the key so it can never equal a NANP key.
        return "+" + d if 8 <= len(d) <= 15 else ""
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) < 7 or len(d) > 15:
        return ""
    return d


def e164(number) -> str:
    key = number_key(number)
    if not key:
        return ""
    if key.startswith("+"):
        return key
    return "+1" + key if len(key) == 10 else "+" + key


def clean_verstat(value) -> str:
    text = str(value or "").strip()
    return text if _VERSTAT_RE.fullmatch(text) else ""


def parse_stir(value) -> dict:
    """Map Twilio's StirVerstat onto {status, level}.

    status: passed | failed | none | absent. level: A | B | C | "".
    https://www.twilio.com/docs/voice/trusted-calling-with-shakenstir
    """
    raw = clean_verstat(value)
    if not raw:
        return {"status": "absent", "level": "", "raw": ""}
    m = _PASSED_RE.match(raw)
    if m:
        return {"status": "passed", "level": m.group(1).upper(), "raw": raw}
    m = _FAILED_RE.match(raw)
    if m:
        return {"status": "failed", "level": (m.group(1) or "").upper(), "raw": raw}
    return {"status": "none", "level": "", "raw": raw}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class TrustLists:
    def __init__(self, path: pathlib.Path):
        self.path = pathlib.Path(path)
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = {}
        except (OSError, ValueError):
            # Keep the bad file for a human; start empty (fail open = screen).
            try:
                self.path.replace(self.path.with_suffix(".corrupt"))
            except OSError:
                pass
            raw = {}
        data = {name: [] for name in LISTS}
        if isinstance(raw, dict):
            for name in LISTS:
                for entry in raw.get(name) or []:
                    if not isinstance(entry, dict):
                        continue
                    num = e164(entry.get("number"))
                    if num and all(number_key(x["number"]) != number_key(num) for x in data[name]):
                        data[name].append({
                            "number": num,
                            "note": str(entry.get("note") or "")[:NOTE_MAX],
                            "added_at": str(entry.get("added_at") or ""),
                        })
        return data

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def snapshot(self) -> dict:
        with self._lock:
            return {name: [dict(e) for e in self._data[name]] for name in LISTS}

    def lookup(self, number) -> str:
        key = number_key(number)
        if not key:
            return ""
        with self._lock:
            for name in ("block", "allow"):
                if any(number_key(e["number"]) == key for e in self._data[name]):
                    return name
        return ""

    def add(self, name: str, number, note: str = "") -> tuple[dict, str]:
        """Add to one list. Returns (entry, previous_list_or_empty).

        A number lives on at most one list, so blocking an allowed number
        moves it. ``previous`` lets the inbox offer a true undo.
        """
        if name not in LISTS:
            raise ValueError("bad-list")
        num = e164(number)
        if not num:
            raise ValueError("bad-number")
        key = number_key(num)
        with self._lock:
            already = any(number_key(e["number"]) == key for e in self._data[name])
            if not already and len(self._data[name]) >= MAX_ENTRIES:
                # Checked before the move so a full list never drops the
                # number from the other list.
                raise ValueError("list-full")
            previous = ""
            for other in LISTS:
                before = len(self._data[other])
                self._data[other] = [e for e in self._data[other] if number_key(e["number"]) != key]
                if len(self._data[other]) != before:
                    previous = other
            entry = {"number": num, "note": str(note or "")[:NOTE_MAX], "added_at": _now()}
            self._data[name].append(entry)
            self._save()
            return dict(entry), previous

    def remove(self, name: str, number) -> bool:
        if name not in LISTS:
            raise ValueError("bad-list")
        key = number_key(number)
        if not key:
            raise ValueError("bad-number")
        with self._lock:
            before = len(self._data[name])
            self._data[name] = [e for e in self._data[name] if number_key(e["number"]) != key]
            changed = len(self._data[name]) != before
            if changed:
                self._save()
            return changed


def classify(lists: TrustLists, number) -> str:
    """allow | block | screen. STIR is deliberately not an input here."""
    hit = lists.lookup(number)
    return hit or "screen"


def block_action(verdict: str, stir: dict) -> str:
    """For a block-listed caller: "reject" only with STIR passed at level A.

    Anything less (B/C, failed, none, absent) may be a spoofed copy of a real
    number, so the call is screened and the message tagged "blocked".
    STIR can only soften a block; it never creates one.
    """
    if verdict != "block":
        return "none"
    if stir.get("status") == "passed" and stir.get("level") == "A":
        return "reject"
    return "screen"
