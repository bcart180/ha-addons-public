"""Spare bathroom heater zone (0.8.0): smart plug + plain space heater + temperature sensor.

Fully independent of the mini splits: it never reads or writes any climate entity, so the shared-mode lock
(splits never in opposite modes) is untouched. The only HA writes it can make are switch.turn_on / turn_off on
the ONE configured heater switch, a persistent notification (plus an optional notify.* service), and a published
mirror state for climate.spare_bathroom.

Safety (all enforced here, every tick):
  * hard cap: off at >= 80 F, latched until the room is below the setpoint
  * 2 h max continuous run, then a 20 min cooldown
  * > 8 h on in the last 24 h: lockout + notify (cleared by turning the zone off and back to heat)
  * temp sensor unavailable / unknown / non-numeric / stale 15 min: off now + notify
  * no 1 F rise after 45 min of heating: off + notify (latched like the lockout)
  * plug on while it should be off: forced off + notify; power draw while off: relay fault + notify
  * door / window open (optional sensor): paused (off)
  * off at startup, shutdown and on any exception
Every safety item sends a phone alert (persistent notification + optional notify.* service).
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
STALE_S = 15 * 60
NO_RISE_S, NO_RISE_F = 45 * 60, 1.0
POWER_FAULT_W = 5.0
BAD = ("unavailable", "unknown", "none", "")
RX = {"heater_switch": r"^switch\.[a-z0-9_]+$", "temp_sensor": r"^sensor\.[a-z0-9_]+$",
      "power_sensor": r"^sensor\.[a-z0-9_]+$", "door_sensor": r"^binary_sensor\.[a-z0-9_]+$",
      "notify_service": r"^notify\.[a-z0-9_]+$", "motion_sensor": r"^binary_sensor\.[a-z0-9_]+$"}
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
    return out


class SpareHeater:
    def __init__(self, opts, ha, state_path=None, clock=time.time):
        self.opts, self.ha, self.path, self.clock = opts or {}, ha, state_path, clock
        self.cfg = config(self.opts)
        self.configured = bool(self.cfg["heater_switch"] and self.cfg["temp_sensor"])
        self.st = {"mode": "heat", "setpoint": SP_DEFAULT}
        self._load()
        # runtime bookkeeping (in memory; a restart starts with the plug off, which is the safe state)
        self.on_since = None        # when the plug was last seen/commanded on
        self.off_since = None       # when it was last turned off (min-off)
        self.cooldown_until = 0.0
        self.cap_latched = False
        self.latched = None         # "lockout" | "no_rise" | "relay_fault": off until mode off -> heat
        self.runs = []              # [(start, end)] on-intervals, pruned to 24 h
        self.rise_ref = None        # (ts, temp) when this heating run started
        self.commanded = False      # what we last asked for
        self.notified = set()
        self.low_since = None       # first tick below low_warn_f
        self.crit_last = None       # last critical alert time
        self.last_motion = None     # display only
        self.last = {"reason": "not_configured" if not self.configured else "starting", "on": False}

    # ---------- persistence (mode + setpoint only) ----------
    def _load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path) as f:
                d = json.load(f)
            if d.get("mode") in ("heat", "off"):
                self.st["mode"] = d["mode"]
            sp = _num(d.get("setpoint"))
            if sp is not None:
                self.st["setpoint"] = min(SP_MAX, max(SP_MIN, sp))
        except (OSError, ValueError, AttributeError):
            pass

    def _save(self):
        if not self.path:
            return
        try:
            with open(self.path, "w") as f:
                json.dump(self.st, f)
        except OSError:
            pass

    @property
    def can_heat(self):
        return bool(self.opts.get("enabled")) and not self.opts.get("dry_run", True)

    # ---------- card writes ----------
    def set_mode(self, mode):
        if mode not in ("heat", "off"):
            raise ValueError("bad mode")
        if mode == "off":
            self.latched = None  # off -> heat is the human reset for lockout / no-rise / relay fault
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

    def _notify(self, key, msg):
        if key in self.notified:
            return
        self.notified.add(key)
        log.warning("spare bathroom: %s", msg)
        try:
            self.ha.heater_notify("Spare bathroom heater", msg, self.cfg["notify_service"])
        except Exception as e:  # notify failure never blocks the off path
            log.warning("notify failed: %s", type(e).__name__)

    def _cause(self):
        r = (self.last or {}).get("reason")
        c = self.latched or ("hard_cap" if self.cap_latched else None) or (
            r if r in ("sensor_unavailable", "cooldown", "door_open", "exception", "dry_run", "app_off", "mode_off")
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
            self._end_run(self.clock())
            self.last = dict(self.last, on=False, reason=why)

    def _end_run(self, now):
        if self.on_since is not None:
            self.runs.append((self.on_since, now))
            self.off_since = now
        self.on_since = None
        self.rise_ref = None

    def _on_seconds_24h(self, now):
        self.runs = [(a, b) for a, b in self.runs if b > now - DAY_S]
        tot = sum(b - max(a, now - DAY_S) for a, b in self.runs)
        if self.on_since is not None:
            tot += now - max(self.on_since, now - DAY_S)
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

    def _tick(self):
        now = self.clock()
        states = {s["entity_id"]: s for s in self.ha.get_states() or []}
        sw = states.get(self.cfg["heater_switch"]) or {}
        is_on = sw.get("state") == "on"
        ts = states.get(self.cfg["temp_sensor"]) or {}
        temp = _num(ts.get("state")) if str(ts.get("state", "")).lower() not in BAD else None
        seen = _ts(ts.get("last_reported") or ts.get("last_updated") or "")
        stale = seen is None or now - seen >= STALE_S
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
            self.last_motion = now
        reason, want = self._decide(now, temp, stale, is_on, sp, states)
        if want and not self.can_heat:
            want, reason = False, "dry_run" if self.opts.get("enabled") else "app_off"

        if want and not is_on:
            self._switch(True)
            self.on_since = now
            self.rise_ref = (now, temp)
        elif not want and (is_on or self.commanded):
            self._switch(False)
            self._end_run(now)
        on = want
        self.last = {"reason": reason, "on": on, "temp": temp, "setpoint": sp, "mode": self.st["mode"],
                     "on_24h_min": round(self._on_seconds_24h(now) / 60), "latched": self.latched,
                     "cooldown_until": self.cooldown_until or None, "ts": now,
                     "motion": mo.get("state") if mo else None, "last_motion": self.last_motion}
        self._low_alerts(now, temp)
        self.publish()
        return self.last

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
        if self.latched:
            return self.latched, False
        if self.st["mode"] != "heat":
            return "mode_off", False
        if self._on_seconds_24h(now) > DAY_LIMIT_S:
            self.latched = "lockout"
            self._notify("lockout", "Heater ran over 8 h in 24 h. Locked out: set Off then Heat to reset.")
            return "lockout", False
        if is_on and self.on_since is not None and now - self.on_since >= MAX_RUN_S:
            self.cooldown_until = now + COOLDOWN_S
            self.notified.discard("cooldown")
            self._notify("cooldown", "Heater ran 2 h straight. 20 min cooldown, then it resumes.")
            return "cooldown", False
        if now < self.cooldown_until:
            return "cooldown", False
        if is_on and self.rise_ref and now - self.rise_ref[0] >= NO_RISE_S and \
                (self.rise_ref[1] is None or temp < self.rise_ref[1] + NO_RISE_F):
            self.latched = "no_rise"
            self._notify("no_rise", "No 1 F rise after 45 min of heating. Heater off: check it is plugged in and on.")
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
