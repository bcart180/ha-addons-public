"""Spare bathroom heater zone (0.8.0): smart plug + plain space heater + temperature sensor.

Fully independent of the mini splits: it never reads or writes any climate entity, so the shared-mode lock
(splits never in opposite modes) is untouched. The only HA writes it can make are switch.turn_on / turn_off on
the ONE configured heater switch, a persistent notification (plus an optional notify.* service), and a published
mirror state for climate.spare_bathroom.

Safety (all enforced here, every tick):
  * hard cap: off at >= 80 F, latched until the room is below the setpoint
  * 2 h max continuous run, then a 20 min cooldown (log only; it resumes on its own)
  * > 8 h on in the last 24 h: lockout + notify (cleared by turning the zone off and back to heat)
  * temp sensor unavailable / unknown / non-numeric / stale (spare_bathroom.stale_minutes, default 75): off now + notify
  * no further 1 F rise (or setpoint reached) within 45 min of the last checkpoint while on: off + latch (log only).
    0.9.6: that latch clears itself once a sensor reading reported AFTER the trip is >= 1 F above the temperature
    at the last checkpoint (the heater did warm the room; the trip came from a slow-reporting sensor). Log only
    when it clears. Detection itself is unchanged; with no such reading it stays latched until mode off -> heat.
    0.9.7: notify only if it is still latched after a later post-trip reading that did not rise 1 F, or if the
    latch has no trip info so it cannot auto-clear.
  * plug on while it should be off: forced off + notify; power draw while off: relay fault + notify
  * door / window open (optional sensor): paused (off) + notify
  * off at startup, shutdown and on any exception
Only real problems send a persistent notification (plus an optional notify.* service): a no-rise latch that
did not auto-clear, a stale or missing sensor, the 80 F cap, a plug on when it should be off, a relay fault,
the 8 h / 24 h lockout, a clock jump, an unreadable state file, a door left open, or a low-temp warning.
Routine events (a no-rise trip that later clears, the auto-clear itself, a 2 h cooldown) are logged only.
Kitten room: holds the setpoint (68 F default) 24/7; no presence, eco or away logic. The optional motion sensor is
display / logging only and never changes heating. Low-temp alerts: warning below 62 F for 10 min, critical below
58 F repeating every 30 min until recovered; each alert names any active lockout / fail-off cause.
Normal control: on at setpoint - 1, off at setpoint + 0.5, min 5 min on and 5 min off (safety offs skip min-on).
Turning ON needs master enabled and dry_run off; turning OFF is always allowed (off is the safe state).
"""
import json
import logging
import os
import re
import time
from datetime import datetime

log = logging.getLogger("smart_hvac.heater")

ENTITY = "climate.spare_bathroom"
NAME = "spare_bathroom"
SP_DEFAULT, SP_MIN, SP_MAX = 68.0, 55.0, 75.0
ON_BELOW, OFF_ABOVE = 1.0, 0.5
MIN_ON_S = MIN_OFF_S = 300
HARD_CAP = 80.0
MAX_RUN_S, COOLDOWN_S = 2 * 3600, 20 * 60
DAY_LIMIT_S, DAY_S = 8 * 3600, 24 * 3600
STALE_MIN_DEFAULT, STALE_MIN_LO, STALE_MIN_HI = 75, 15, 120  # 0.8.2: Zooz ZSE11 reports hourly or on 1 F change
STALE_S = STALE_MIN_DEFAULT * 60
FUTURE_SKEW_S = 60  # timestamps further ahead than this are rejected
CLOCK_JUMP_S = 60  # 0.8.5 (SA M1b): wall clock back > 60 s, or off from monotonic elapsed by > 60 s = clock jump
NO_RISE_S, NO_RISE_F = 45 * 60, 1.0
POWER_FAULT_W = 5.0
BAD = ("unavailable", "unknown", "none", "")
RX = {"heater_switch": r"^switch\.[a-z0-9_]+$", "temp_sensor": r"^sensor\.[a-z0-9_]+$",
      "power_sensor": r"^sensor\.[a-z0-9_]+$", "door_sensor": r"^binary_sensor\.[a-z0-9_]+$",
      "notify_service": r"^notify\.[a-z0-9_]+$", "motion_sensor": r"^binary_sensor\.[a-z0-9_]+$",
      "last_seen_sensor": r"^sensor\.[a-z0-9_]+$"}
# Low-temp alerts (kitten room). Option keys and defaults; values are clamped to these ranges.
LOW = {"low_warn_f": (62.0, 50.0, 70.0), "low_warn_min": (10, 1, 120),
       "low_crit_f": (58.0, 45.0, 65.0), "low_crit_repeat_min": (30, 5, 240)}


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _ts(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _aware_ts(iso):
    """Epoch seconds for a timezone-aware ISO timestamp; naive or unparsable -> None."""
    if iso is None:
        return None
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.timestamp() if d.tzinfo is not None and d.utcoffset() is not None else None


def config(opts):
    """Valid entity ids from opts['spare_bathroom'] (anything else is treated as unset)."""
    raw = (opts or {}).get("spare_bathroom") or {}
    out = {k: (raw.get(k) or "").strip() for k in RX}
    for k, rx in RX.items():
        if out[k] and not re.match(rx, out[k]):
            out[k] = ""
    out["label"] = str(raw.get("label") or "Spare bathroom")[:40]
    for k, (d, lo, hi) in LOW.items():
        v = _num(raw.get(k))
        out[k] = d if v is None else min(hi, max(lo, v))
    v = _num(raw.get("stale_minutes"))
    out["stale_minutes"] = STALE_MIN_DEFAULT if v is None else min(STALE_MIN_HI, max(STALE_MIN_LO, v))
    return out


class SpareHeater:
    def __init__(self, opts, ha, state_path=None, clock=time.time, mono=None):
        self.opts, self.ha, self.path, self.clock = opts or {}, ha, state_path, clock
        # 0.8.5 (SA M1b): every in-process duration (2 h max run, satisfied / min on-off, 8 h / 24 h, no_rise,
        # cooldown) runs on a monotonic timeline anchored to the wall clock once at startup, so a wall clock jump
        # mid-run cannot stretch or shrink a timer. Tests that inject only a fake wall clock reuse it as monotonic.
        self.mono = mono or (time.monotonic if clock is time.time else clock)
        self._w0, self._m0 = clock(), self.mono()
        self._prev = None           # (wall, mono) at the previous tick, for clock-jump detection
        self.cfg = config(self.opts)
        self.configured = bool(self.cfg["heater_switch"] and self.cfg["temp_sensor"])
        self.st = {"mode": "heat", "setpoint": SP_DEFAULT}
        # runtime bookkeeping. 0.8.4 (SA R1): the safety fields are persisted in heater.json and restored on load,
        # so a restart never re-arms a latched / cooling-down heater or forgets the 24 h run history.
        self.on_since = None        # when the plug was last seen/commanded on
        self.off_since = None       # when it was last turned off (min-off)
        self.cooldown_until = 0.0
        self.run_start = None       # 0.8.4 (SA L2): start of the current run for the 2 h max-run timer
        self.carry = None           # (run_start, off_ts): run interrupted by a restart, resumed if back on in 20 min
        self.cap_latched = False
        self.latched = None         # "lockout" | "no_rise" | "relay_fault": off until mode off -> heat
        self.nr_trip = None         # 0.9.6: (wall ts, checkpoint temp) of the no_rise trip, for the auto-clear
        self._seen_now = None       # 0.9.6: this tick's temp-sensor report time (wall), None = unknown
        self.runs = []              # [(start, end)] on-intervals, pruned to 24 h
        self.rise_ref = None        # (ts, temp) last progress checkpoint (run start, then each 1 F rise)
        self.commanded = False      # what we last asked for
        self.notified = set()
        self.low_since = None       # first tick below low_warn_f
        self.crit_last = None       # last critical alert time
        self.last_motion = None     # display only
        self.last = {"reason": "not_configured" if not self.configured else "starting", "on": False}
        self._written = None
        self._load()

    # ---------- persistence (0.8.4: mode, setpoint + safety state) ----------
    def _state(self):
        rr = self.rise_ref
        return {"mode": self.st["mode"], "setpoint": self.st["setpoint"], "latched": self.latched,
                "cap_latched": self.cap_latched, "cooldown_until": self.cooldown_until,
                "rise_ref": list(rr) if rr else None, "on_since": self.on_since, "off_since": self.off_since,
                "runs": [list(x) for x in self.runs], "run_start": self.run_start,
                "carry": list(self.carry) if self.carry else None,
                "nr_trip": list(self.nr_trip) if self.nr_trip else None}

    def _load(self):
        """Restore persisted state. Missing file = first run (defaults). Unreadable / corrupt file = fail safe:
        plug stays off (startup safe_off) and a 20 min cooldown starts. Bad individual fields are dropped."""
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path) as f:
                d = json.load(f)
            if not isinstance(d, dict):
                raise ValueError("not an object")
        except (OSError, ValueError, UnicodeDecodeError):
            self.cooldown_until = self._now() + COOLDOWN_S
            self.last = dict(self.last, reason="state_unreadable")
            if self.configured:
                self._notify("state_unreadable", "Heater state file unreadable. Plug off, 20 min cooldown.")
            return
        if d.get("mode") in ("heat", "off"):
            self.st["mode"] = d["mode"]
        sp = _num(d.get("setpoint"))
        if sp is not None and not isinstance(d.get("setpoint"), bool):
            self.st["setpoint"] = min(SP_MAX, max(SP_MIN, sp))
        if d.get("latched") in ("lockout", "no_rise", "relay_fault"):
            self.latched = d["latched"]
        self.cap_latched = d.get("cap_latched") is True
        # 0.8.4 (SA M1): sanitize timestamps against now; anything > 60 s in the future or invalid is dropped
        now = self._now()

        def ok(v):
            v = _num(v)
            return v if v is not None and v > 0 and v <= now + FUTURE_SKEW_S else None

        cu = _num(d.get("cooldown_until"))
        if cu is not None and cu > 0:
            self.cooldown_until = min(cu, now + COOLDOWN_S)  # far-future cooldown clamped to 20 min
        rr = d.get("rise_ref")
        if isinstance(rr, list) and len(rr) == 2 and ok(rr[0]) is not None:
            self.rise_ref = (ok(rr[0]), _num(rr[1]))
        for k in ("on_since", "off_since", "run_start"):
            setattr(self, k, ok(d.get(k)))
        c = d.get("carry")
        if isinstance(c, list) and len(c) == 2 and ok(c[0]) is not None and ok(c[1]) is not None:
            self.carry = (ok(c[0]), ok(c[1]))
        nt = d.get("nr_trip")
        if self.latched == "no_rise" and isinstance(nt, list) and len(nt) == 2 and ok(nt[0]) is not None \
                and _num(nt[1]) is not None and not isinstance(nt[1], bool):
            self.nr_trip = (ok(nt[0]), _num(nt[1]))
        runs = d.get("runs")
        if isinstance(runs, list):
            self.runs = [(ok(x[0]), ok(x[1])) for x in runs if isinstance(x, list) and len(x) == 2
                         and ok(x[0]) is not None and ok(x[1]) is not None and ok(x[1]) >= ok(x[0])]
        # SA L2: a run interrupted by a restart keeps its start, so a crash loop cannot reset the 2 h timer
        if self.on_since is not None:
            start = min(t for t in (self.run_start, self.on_since) if t is not None)
            if now - start >= MAX_RUN_S:
                self.cooldown_until = max(self.cooldown_until, now + COOLDOWN_S)
                self.carry = None
            else:
                self.carry = (start, now)
        self._written = self._state()

    def _save(self):
        """Atomic write (tmp + fsync + os.replace) whenever the persisted state changed."""
        if not self.path:
            return
        cur = self._state()
        if cur == self._written:
            return
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(cur, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            try:  # SA L3: fsync the directory so the rename is durable
                fd = os.open(os.path.dirname(os.path.abspath(self.path)), os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError:
                pass
            self._written = cur
        except OSError as e:
            log.warning("heater state save failed: %s", type(e).__name__)

    @property
    def can_heat(self):
        """0.8.1: ON needs the global gate (enabled + dry_run off) AND the heater-only gate
        (spare_bathroom.enabled true + spare_bathroom.dry_run false) AND both ids set."""
        sb = (self.opts.get("spare_bathroom") or {}) if isinstance(self.opts.get("spare_bathroom"), dict) else {}
        global_ok = bool(self.opts.get("enabled")) and self.opts.get("dry_run", True) is False
        heater_ok = sb.get("enabled", False) is True and sb.get("dry_run", True) is False
        return global_ok and heater_ok and self.configured

    # ---------- card writes ----------
    def set_mode(self, mode):
        if mode not in ("heat", "off"):
            raise ValueError("bad mode")
        if mode == "off":
            self.latched = None  # off -> heat is the human reset for lockout / no-rise / relay fault
            self.nr_trip = None
            self.notified.clear()
        self.st["mode"] = mode
        self._save()

    def set_setpoint(self, t):
        t = _num(t)
        if t is None or isinstance(t, bool):
            raise ValueError("bad setpoint")
        self.st["setpoint"] = min(SP_MAX, max(SP_MIN, round(t * 2) / 2))
        self._save()

    # ---------- HA helpers ----------
    def _switch(self, on):
        self.ha.heater_switch(self.cfg["heater_switch"], on)
        self.commanded = on

    def _log(self, msg):
        log.warning("spare bathroom: %s", msg)

    def _notify(self, key, msg):
        if key in self.notified:
            return
        self.notified.add(key)
        self._log(msg)
        try:
            self.ha.heater_notify("Spare bathroom heater", msg, self.cfg["notify_service"])
        except Exception as e:  # notify failure never blocks the off path
            log.warning("notify failed: %s", type(e).__name__)

    def _cause(self):
        r = (self.last or {}).get("reason")
        c = self.latched or ("hard_cap" if self.cap_latched else None) or (
            r if r in ("sensor_unavailable", "cooldown", "clock_jump", "door_open", "exception", "dry_run",
                       "app_off", "mode_off")
            else None)
        return " Cause: %s." % c.replace("_", " ") if c else ""

    def _low_alerts(self, now, temp):
        if temp is None:
            return
        c = self.cfg
        if temp >= c["low_warn_f"]:
            if self.low_since is not None or self.crit_last is not None:
                self.notified.discard("low_warn")
                self.low_since = self.crit_last = None
            return
        if self.low_since is None:
            self.low_since = now
        if now - self.low_since >= c["low_warn_min"] * 60:
            self._notify("low_warn", "Kitten room is %.1f F (below %.0f F for %d min).%s"
                         % (temp, c["low_warn_f"], c["low_warn_min"], self._cause()))
        if temp < c["low_crit_f"]:
            if self.crit_last is None or now - self.crit_last >= c["low_crit_repeat_min"] * 60:
                self.crit_last = now
                self.notified.discard("low_crit")
                self._notify("low_crit", "CRITICAL: kitten room is %.1f F (below %.0f F).%s"
                             % (temp, c["low_crit_f"], self._cause()))
        else:
            self.crit_last = None

    def safe_off(self, why="safe_off"):
        """Startup / shutdown / exception: plug off, no questions asked."""
        if not self.configured:
            return
        try:
            self._switch(False)
        finally:
            self._end_run(self._now())
            self.last = dict(self.last, on=False, reason=why)
            self._save()

    def _end_run(self, now):
        if self.on_since is not None:
            self.runs.append((min(self.on_since, now), now))  # never a negative run
            self.off_since = now
        self.on_since = None
        self.run_start = None
        self.rise_ref = None

    def _on_seconds_24h(self, now):
        self.runs = [(a, b) for a, b in self.runs if b > now - DAY_S]
        tot = sum(max(0.0, b - max(a, now - DAY_S)) for a, b in self.runs)
        if self.on_since is not None:
            tot += max(0.0, now - max(self.on_since, now - DAY_S))
        return tot

    # ---------- main loop ----------
    def tick(self):
        if not self.configured:
            self.last = {"reason": "not_configured", "on": False}
            return self.last
        try:
            return self._tick()
        except Exception as e:  # any bug or HA error: plug off
            log.warning("heater tick failed: %s", type(e).__name__)
            try:
                self.safe_off("exception")
            except Exception:
                pass
            return self.last

    def _now(self):
        """Monotonic timeline in epoch seconds (wall time at startup + monotonic elapsed)."""
        return self._w0 + (self.mono() - self._m0)

    def _clock_jump(self, wall, m):
        """0.8.5 (SA M1b): True when the wall clock went back > 60 s since the last tick, or its step differs
        from the monotonic step by > 60 s (either direction)."""
        prev, self._prev = self._prev, (wall, m)
        if prev is None:
            return False
        dw, dm = wall - prev[0], m - prev[1]
        return dw < -CLOCK_JUMP_S or abs(dw - dm) > CLOCK_JUMP_S

    def _tick(self):
        wall, m = self.clock(), self.mono()
        now = self._w0 + (m - self._m0)
        jumped = self._clock_jump(wall, m)
        states = {s["entity_id"]: s for s in self.ha.get_states() or []}
        sw = states.get(self.cfg["heater_switch"]) or {}
        is_on = sw.get("state") == "on"
        ts = states.get(self.cfg["temp_sensor"]) or {}
        temp = _num(ts.get("state")) if str(ts.get("state", "")).lower() not in BAD else None
        seen = self._seen(ts, states, wall)  # HA timestamps are wall clock
        stale = seen is None or wall - seen >= self.cfg["stale_minutes"] * 60
        self._seen_now = seen
        sp = self.st["setpoint"]

        # observed-state bookkeeping
        if is_on and self.on_since is None:
            self.on_since = now
        if not is_on and self.on_since is not None:
            self._end_run(now)

        # plug on without us asking (manual press, automation, relay): force off + notify
        if is_on and not self.commanded:
            self._switch(False)
            self._end_run(now)
            self._notify("unexpected_on", "Plug was on while it should be off. Turned it off.")
            is_on = False
        # power draw while off = relay fault (latched)
        pw = _num((states.get(self.cfg["power_sensor"]) or {}).get("state")) if self.cfg["power_sensor"] else None
        if (not is_on and not self.commanded and pw is not None and pw > POWER_FAULT_W
                and (self.off_since is None or now - self.off_since >= 60)):
            self.latched = "relay_fault"
            self._notify("relay_fault", "Relay fault: plug reports off but draws %.0f W. Unplug the heater." % pw)

        mo = (states.get(self.cfg["motion_sensor"]) or {}) if self.cfg["motion_sensor"] else {}
        if mo.get("state") == "on":  # display only: never read by _decide
            self.last_motion = wall
        if jumped:  # 0.8.5 (SA M1b): plug off now + 20 min cooldown, whatever else is going on
            self.cooldown_until = max(self.cooldown_until, now + COOLDOWN_S)
            self.notified.discard("clock_jump")
            self._notify("clock_jump", "System clock jumped. Heater off, 20 min cooldown.")
            reason, want = "clock_jump", False
        else:
            reason, want = self._decide(now, temp, stale, is_on, sp, states)
        self._save()  # latches / cooldown persisted before any switch write
        if want and not self.can_heat:
            sb = self.opts.get("spare_bathroom") if isinstance(self.opts.get("spare_bathroom"), dict) else {}
            if not (self.opts.get("enabled") and (sb or {}).get("enabled") is True):
                reason = "app_off"
            else:
                reason = "dry_run"
            want = False

        if want and not is_on:
            self._switch(True)
            self.on_since = now
            c = self.carry
            self.run_start = c[0] if c and 0 <= now - c[1] < COOLDOWN_S else now
            self.carry = None
            self.rise_ref = (now, temp)
        elif not want and (is_on or self.commanded):
            self._switch(False)
            self._end_run(now)
        on = want
        self.last = {"reason": reason, "on": on, "temp": temp, "setpoint": sp, "mode": self.st["mode"],
                     "on_24h_min": round(self._on_seconds_24h(now) / 60), "latched": self.latched,
                     "cooldown_until": self.cooldown_until or None, "ts": wall,
                     "motion": mo.get("state") if mo else None, "last_motion": self.last_motion}
        self._low_alerts(now, temp)
        self._save()
        self.publish()
        return self.last

    def _seen(self, ts, states, now):
        """0.8.2: the temperature entity's own last_reported / last_updated is the authority. The optional
        last-seen sensor can only TIGHTEN it (min of the two), never make a reading fresher. Timestamps must be
        timezone-aware and not more than 60 s in the future; anything else is ignored. None = stale."""
        own = [t for t in (_aware_ts(ts.get("last_reported")), _aware_ts(ts.get("last_updated")))
               if t is not None and t <= now + FUTURE_SKEW_S]
        if not own:
            return None
        seen = max(own)
        if self.cfg.get("last_seen_sensor"):
            ls = states.get(self.cfg["last_seen_sensor"]) or {}
            t = _aware_ts(ls.get("state")) if str(ls.get("state", "")).lower() not in BAD else None
            if t is not None and t <= now + FUTURE_SKEW_S:
                seen = min(seen, t)
        return seen

    def _decide(self, now, temp, stale, is_on, sp, states):
        """(reason, want_on). Safety checks first; each returns off."""
        if temp is None or stale:
            self._notify("sensor", "Temperature sensor unavailable or stale. Heater off.")
            return "sensor_unavailable", False
        self.notified.discard("sensor")
        if temp >= HARD_CAP:
            self.cap_latched = True
            self._notify("cap", "Room reached %.0f F (cap %d). Heater off." % (temp, HARD_CAP))
        if self.cap_latched:
            if temp < sp:
                self.cap_latched = False
                self.notified.discard("cap")
            else:
                return "hard_cap", False
        if self.latched == "no_rise":
            self._no_rise_autoclear(temp)
            if self.latched == "no_rise":
                self._no_rise_maybe_notify(temp)
        if self.latched:
            return self.latched, False
        if self.st["mode"] != "heat":
            return "mode_off", False
        if self._on_seconds_24h(now) > DAY_LIMIT_S:
            self.latched = "lockout"
            self._notify("lockout", "Heater ran over 8 h in 24 h. Locked out: set Off then Heat to reset.")
            return "lockout", False
        rs = self.run_start if self.run_start is not None else self.on_since
        if is_on and rs is not None and now - min(rs, self.on_since or rs) >= MAX_RUN_S:
            self.cooldown_until = now + COOLDOWN_S
            self.notified.discard("cooldown")
            self._log("Heater ran 2 h straight. 20 min cooldown, then it resumes.")
            return "cooldown", False
        if now < self.cooldown_until:
            return "cooldown", False
        # 0.8.3 (SA M2): progress checkpoint. Each further >= 1 F rise restarts the 45 min window, so a reading
        # that rises once and then sticks still trips no_rise 45 min after the last rise.
        # 0.8.4 (SA L1): reaching the setpoint resets the checkpoint ONCE (on crossing), it does not exempt the
        # run: a reading frozen at / above the setpoint while on still trips no_rise after 45 min.
        if is_on and self.rise_ref and self.rise_ref[1] is not None and temp is not None and \
                (temp >= self.rise_ref[1] + NO_RISE_F or (temp >= sp > self.rise_ref[1])):
            self.rise_ref = (now, temp)
        if is_on and self.rise_ref and now - self.rise_ref[0] >= NO_RISE_S and \
                (self.rise_ref[1] is None or temp < self.rise_ref[1] + NO_RISE_F):
            if temp >= sp:  # SA: at / above setpoint a frozen reading is not a fault: plain off, no latch
                return "holding", False
            self.latched = "no_rise"
            self.nr_trip = (self.clock(), self.rise_ref[1]) if self.rise_ref[1] is not None else None
            self._log("No 1 F rise after 45 min of heating. Heater off: check it is plugged in and on.")
            return "no_rise", False
        door = self.cfg["door_sensor"]
        if door and (states.get(door) or {}).get("state") != "off":  # open, unknown or missing = paused
            self._notify("door", "Door/window open: heater paused.")
            return "door_open", False
        self.notified.discard("door")
        # normal hysteresis + min on / min off
        if is_on:
            if temp >= sp + OFF_ABOVE and self.on_since is not None and now - self.on_since >= MIN_ON_S:
                return "satisfied", False
            return "heating", True
        if temp <= sp - ON_BELOW:
            if self.off_since is not None and now - self.off_since < MIN_OFF_S:
                return "min_off", False
            return "heating", True
        return "idle", False

    def _no_rise_autoclear(self, temp):
        """0.9.6: clear a no_rise latch when a reading reported after the trip shows the room rose >= 1 F over
        the trip's checkpoint temperature. Only fresh (non-stale) readings get here. Never turns the plug on itself:
        normal control resumes, so it heats again only at setpoint - 1 and every other safety still applies.
        0.9.7: clearing is logged only; it does not create a notification."""
        t = self.nr_trip
        if not t or temp is None or self._seen_now is None or self._seen_now <= t[0]:
            return
        if temp < t[1] + NO_RISE_F:
            return
        log.info("spare bathroom: no_rise latch auto-cleared: reading %.1f F reported after the trip is >= %.1f F "
                 "above the %.1f F checkpoint", temp, NO_RISE_F, t[1])
        self.latched = None
        self.nr_trip = None
        self.notified.discard("no_rise")

    def _no_rise_maybe_notify(self, temp):
        """0.9.7: notify only when the no_rise latch is still a real problem. A fresh post-trip reading that
        did not rise 1 F means the heater did not warm the room. A legacy latch with no trip info cannot
        auto-clear, so it notifies now. A trip that is still waiting for the next reading stays quiet."""
        if self.latched != "no_rise":
            return
        msg = "No 1 F rise after 45 min of heating. Heater off: check it is plugged in and on."
        t = self.nr_trip
        if not t:
            self._notify("no_rise", msg)
            return
        if self._seen_now is None or self._seen_now <= t[0] or temp is None:
            return
        if temp < t[1] + NO_RISE_F:
            self._notify("no_rise", msg)

    def publish(self):
        """Mirror state for the card + watchdog heartbeat (attribute 'heartbeat' changes every tick)."""
        L = self.last
        attrs = {"friendly_name": self.cfg["label"], "hvac_modes": ["heat", "off"], "min_temp": SP_MIN,
                 "max_temp": SP_MAX, "target_temp_step": 0.5, "temperature": self.st["setpoint"],
                 "current_temperature": L.get("temp"), "hvac_action": "heating" if L.get("on") else (
                     "off" if self.st["mode"] == "off" else "idle"),
                 "reason": L.get("reason"), "last_motion": self.last_motion,
                 "on_24h_min": L.get("on_24h_min"), "heartbeat": L.get("ts"),
                 "unit_of_measurement": "°F", "supported_features": 1}
        try:
            self.ha.publish_state(ENTITY, self.st["mode"], attrs)
        except Exception as e:
            log.debug("publish failed: %s", type(e).__name__)

    def snapshot(self):
        return {"name": NAME, "entity": ENTITY, "label": self.cfg["label"], "configured": self.configured,
                "mode": self.st["mode"], "setpoint": self.st["setpoint"], "min": SP_MIN, "max": SP_MAX,
                "last_motion": self.last_motion, "motion_sensor": bool(self.cfg["motion_sensor"]),
                "low_warn_f": self.cfg["low_warn_f"], "low_crit_f": self.cfg["low_crit_f"], "last": self.last}
