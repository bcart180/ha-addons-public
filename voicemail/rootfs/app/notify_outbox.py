"""Durable notification outbox (0.7.0): email + iPhone push per phone message.

Jobs live in /data/notify_outbox.json.  Each job id is ``<kind>:<ref>``, and
``enqueue`` refuses an id it already holds, so one call yields one email job
and one push job no matter how many triggers fire.  ``pump_once`` sends due
jobs and retries failures with exponential backoff.  A job that keeps failing
is parked as ``dead`` (visible in /api/diag counts) instead of being dropped.

Senders are async callables ``send(job) -> (ok: bool, code: str, retry: bool)``.
They never raise into the pump, and their ``code`` is a short status word,
never a body, URL, or token.
"""
from __future__ import annotations

import copy
import json
import pathlib
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from inbox_store import atomic_write_bytes, exclusive_lock
from notify_render import PUSH_PANEL

KINDS = ("email", "push", "push_update", "push_notice")
# push_notice is the one masked "N CallCatch notices expired" push (R3-L1); its
# ref is EX-<unix seconds> because it belongs to no single call.
ID_RE = re.compile(r"^(?:(email|push|push_update):CC-[A-Z0-9]{5}|push_notice:EX-[0-9]{10})$")
NOTICE_KIND = "push_notice"
BASE_DELAY_S = 30
MAX_DELAY_S = 30 * 60
MAX_ATTEMPTS = 12
# R4-L1: a retryable 409 (AgentMail "conflict": the same Idempotency-Key in flight, or
# reused in a way the body didn't name) is retried at most this many times, then parked.
MAX_CONFLICT_RETRIES = 3
CONFLICT_CODE = "http-409"
KEEP_DONE_S = 14 * 24 * 3600
MAX_JOBS = 1000

Sender = Callable[[dict], Awaitable[tuple[bool, str, bool]]]


def now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def backoff(attempts: int) -> int:
    return min(MAX_DELAY_S, BASE_DELAY_S * (2 ** max(0, attempts - 1)))


class Outbox:
    def __init__(self, path: pathlib.Path):
        self.path = path
        self.lock_path = path.with_suffix(".lock")

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"jobs": {}}
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), dict):
            return {"jobs": {}}
        return data

    def _write(self, data: dict) -> None:
        atomic_write_bytes(self.path, json.dumps(data, sort_keys=True).encode("utf-8"))

    def _prune(self, data: dict, clock: float) -> None:
        jobs = data["jobs"]
        for jid in list(jobs):
            job = jobs[jid]
            if job.get("status") == "sent" and clock - float(job.get("done_at") or clock) > KEEP_DONE_S:
                del jobs[jid]
        if len(jobs) > MAX_JOBS:
            sent = sorted((j for j in jobs if jobs[j].get("status") == "sent"),
                          key=lambda j: float(jobs[j].get("done_at") or 0))
            for jid in sent[: len(jobs) - MAX_JOBS]:
                del jobs[jid]

    def enqueue(self, kind: str, ref: str, payload: dict) -> bool:
        """True when a new job was added.  False for a duplicate id."""
        jid = f"{kind}:{ref}"
        if kind not in KINDS or not ID_RE.fullmatch(jid):
            raise ValueError("bad-job")
        clock = now_ts()
        with exclusive_lock(self.lock_path):
            data = self._read()
            self._prune(data, clock)
            if jid in data["jobs"]:
                return False
            data["jobs"][jid] = {
                "id": jid,
                "kind": kind,
                "ref": ref,
                "payload": payload,
                "status": "pending",
                "attempts": 0,
                "next_at": clock,
                "created_at": clock,
                "last_code": "",
                "done_at": 0,
            }
            self._write(data)
            return True

    def get(self, jid: str) -> dict | None:
        with exclusive_lock(self.lock_path):
            job = self._read()["jobs"].get(jid)
            return copy.deepcopy(job) if isinstance(job, dict) else None

    def counts(self) -> dict:
        out = {"pending": 0, "sent": 0, "dead": 0, "dry_run": 0, "expired": 0}
        with exclusive_lock(self.lock_path):
            for job in self._read()["jobs"].values():
                status = str(job.get("status") or "")
                if status in out:
                    out[status] += 1
        return out

    def suppress_pending(self, clock: float | None = None, status: str = "dry_run",
                         created_before: float | None = None) -> list[str]:
        """Close pending jobs without sending: ``dry_run`` (switch explicitly
        on) or ``expired`` (held too long while the switch was unreadable).

        Terminal on purpose, so turning dry_run off later never releases a
        backlog of stale notices.  The message itself stays in CallCatch."""
        if status not in ("dry_run", "expired"):
            raise ValueError("bad-status")
        now = clock if clock is not None else now_ts()
        done: list[str] = []
        with exclusive_lock(self.lock_path):
            data = self._read()
            for jid, job in data["jobs"].items():
                if job.get("status") != "pending":
                    continue
                if status == "expired" and job.get("kind") == NOTICE_KIND:
                    continue  # the expiry notice itself never expires (no notice-about-the-notice loop)
                if created_before is not None and float(job.get("created_at") or 0) >= created_before:
                    continue
                job["status"] = status
                job["done_at"] = now
                job["payload"] = {}
                done.append(jid)
            if done:
                self._write(data)
        return done

    def note_expired(self, n: int, clock: float | None = None) -> str:
        """R3-L1: fold ``n`` newly expired jobs into ONE pending masked push.

        If an expiry notice is still pending (the switch is still unreadable),
        its count grows instead of adding a second job.  It goes through the
        same gate as every other job: sent on the first ``send`` tick, closed
        as ``dry_run`` if the switch is explicitly on.  No name, number or ref."""
        if n <= 0:
            return ""
        now = clock if clock is not None else now_ts()
        with exclusive_lock(self.lock_path):
            data = self._read()
            job = next((j for j in data["jobs"].values()
                        if j.get("kind") == NOTICE_KIND and j.get("status") == "pending"), None)
            if job is None:
                # R4-I1: a closed notice from the same second keeps its id, so step to
                # the next free second instead of dropping this count.
                base = int(now)
                jid = ref = ""
                for step in range(len(data["jobs"]) + 1):
                    cand_ref = f"EX-{(base + step) % 10**10:010d}"
                    cand = f"{NOTICE_KIND}:{cand_ref}"
                    if ID_RE.fullmatch(cand) and cand not in data["jobs"]:
                        jid, ref = cand, cand_ref
                        break
                if not jid:  # unreachable: N existing ids cannot occupy N+1 distinct candidates
                    raise RuntimeError("notice-id-exhausted")
                job = {"id": jid, "kind": NOTICE_KIND, "ref": ref, "payload": {}, "status": "pending",
                       "attempts": 0, "next_at": now, "created_at": now, "last_code": "", "done_at": 0,
                       "count": 0}
                data["jobs"][jid] = job
            job["count"] = int(job.get("count") or 0) + int(n)
            job["payload"] = expired_push(job["count"])
            self._write(data)
            return str(job["id"])

    def _claim_due(self, clock: float, limit: int) -> list[dict]:
        with exclusive_lock(self.lock_path):
            data = self._read()
            due = [
                copy.deepcopy(j) for j in data["jobs"].values()
                if j.get("status") == "pending" and float(j.get("next_at") or 0) <= clock
            ]
        due.sort(key=lambda j: float(j.get("created_at") or 0))
        return due[:limit]

    def _record(self, jid: str, ok: bool, code: str, retry: bool, clock: float) -> dict | None:
        with exclusive_lock(self.lock_path):
            data = self._read()
            job = data["jobs"].get(jid)
            if not isinstance(job, dict) or job.get("status") != "pending":
                return None
            job["attempts"] = int(job.get("attempts") or 0) + 1
            job["last_code"] = re.sub(r"[^a-z0-9_\-]", "", str(code or "").lower())[:40]
            if not ok and job["last_code"] == CONFLICT_CODE:
                job["conflicts"] = int(job.get("conflicts") or 0) + 1
            if ok:
                job["status"] = "sent"
                job["done_at"] = clock
                job["payload"] = {}  # message body no longer needed on disk
            elif (not retry or job["attempts"] >= MAX_ATTEMPTS
                  or int(job.get("conflicts") or 0) > MAX_CONFLICT_RETRIES):
                job["status"] = "dead"
                job["done_at"] = clock
            else:
                job["next_at"] = clock + backoff(job["attempts"])
            self._write(data)
            return copy.deepcopy(job)

    async def pump_once(self, senders: dict[str, Sender], clock: float | None = None, limit: int = 10) -> list[dict]:
        now = clock if clock is not None else now_ts()
        results = []
        for job in self._claim_due(now, limit):
            sender = senders.get("push" if job["kind"] in ("push_update", NOTICE_KIND) else job["kind"])
            if sender is None:
                continue  # backend not configured yet: stays pending, no attempt used
            try:
                ok, code, retry = await sender(job)
            except Exception as exc:  # noqa: BLE001 - a sender must not stop the pump
                ok, code, retry = False, type(exc).__name__, True
            rec = self._record(job["id"], ok, code, retry, now)
            if rec is not None:
                results.append(rec)
        return results


def expired_push(n: int) -> dict:
    """Masked by construction: a count and a fixed sentence, nothing from a call."""
    n = max(1, int(n))
    word = "notice" if n == 1 else "notices"
    return {
        "title": "CallCatch",
        "message": f"{n} CallCatch {word} expired while the dry-run switch was unreadable.  Open CallCatch.",
        "data": {"url": PUSH_PANEL, "tag": "callcatch-expired", "group": "callcatch"},
    }


def redacted(job: dict) -> dict[str, Any]:
    """Diag view: no payload."""
    return {k: job.get(k) for k in ("id", "kind", "status", "attempts", "last_code")}
