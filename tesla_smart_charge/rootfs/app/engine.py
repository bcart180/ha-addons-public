"""Tesla Smart Charge engine: reads HA, plans with core.plan, commands BLE first then cloud."""
import logging
import re
import time
from datetime import datetime, timedelta

import core

log = logging.getLogger("tesla_smart_charge")

DEFAULTS = {"dry_run": True, "dry_run_safety_off": True, "check_interval_min": 10, "tick_s": 30,
            "confirm_s": 20, "battery_kwh": 75, "charge_efficiency": 0.9, "target_soc": 80,
            "departure_time": "07:00", "finish_buffer_min": 30, "min_amps": 5, "max_amps": 32,
            "fallback_voltage": 240,
            "stale_minutes": 30, "wake_max_per_window": 5,
            "charge_on_max_per_window": 5, "taper_soc": 90, "taper_factor": 1.5, "rates": [],
            "default_price": None, "currency": "$", "peak_schedule": [], "notify_not_charging_min": 20,
            "handle_temp_max_f": 150, "ramp_up_max_a": 4, "derate_margin_a": 4, "derate_checks": 2}
HARD_MAX_AMPS = 48
HELPER_RE = re.compile(r"^input_boolean\.[a-z0-9_]+$")
HELPER_KEYS = ("enabled_entity", "charge_now_entity", "pause_entity", "trip_entity")
SAFETY_STOPS = ("wall_connector_fault", "handle_too_hot",
                "stale_state", "peak", "plug_state_unavailable", "soc_or_target_unavailable")
ENTITY_KEYS = ("charge_switch", "charge_current", "charge_limit", "wake_button", "ble_charge_switch",
               "ble_charge_current", "ble_charge_limit", "ble_wake_button", "charge_now_entity",
               "pause_entity", "trip_entity")


class Backoff:
    """Per-window cap + exponential backoff on the monotonic clock (minutes 10/20/40/80/120 cap)."""
    STEPS_MIN = (10, 20, 40, 80, 120)

    def __init__(self, cap):
        self.cap = int(cap)
        self.reset()

    def reset(self):
        self.count, self.next_at = 0, None

    def take(self, mono_now):
        if self.count >= self.cap:
            return False
        if self.next_at is not None and mono_now < self.next_at:
            return False
        self.next_at = mono_now + 60 * self.STEPS_MIN[min(self.count, len(self.STEPS_MIN) - 1)]
        self.count += 1
        return True


class Engine:
    def __init__(self, opts, ha, clock=None, sleep=None, mono=None):
        self.o = dict(DEFAULTS, **{k: v for k, v in opts.items() if v not in (None,)})
        self.ha = ha
        for k in HELPER_KEYS:  # defense in depth: the schema also enforces this
            v = self.o.get(k)
            if v and not HELPER_RE.match(str(v)):
                log.error("%s must look like input_boolean.name; ignoring it", k)
                self.o[k] = ""
        self.o["max_amps"] = max(1, min(int(self.o["max_amps"]), HARD_MAX_AMPS))
        if int(self.o["min_amps"]) > self.o["max_amps"]:
            log.warning("min_amps %s > max_amps %s: using max_amps for both (max wins)",
                        self.o["min_amps"], self.o["max_amps"])
            self.o["min_amps"] = self.o["max_amps"]
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.mono = mono or time.monotonic
        self.last_wake_mono = None  # L4: never reset; hard minimum between any two wake presses
        self.plug_definite = None  # last definite plug reading (True/False); unavailable/unknown ignored
        self.peak_definite = None  # last definite peak reading; unknown peak never makes an edge
        self.sleep = sleep or time.sleep
        self.prev = {}
        self.last_plan = None  # monotonic seconds of last full plan (L-b)
        self.last_amps_cmd = None
        self.window_start = None
        self.notified_window = None
        self.status = {}
        self.plug_on_edge = False
        self.transport = None  # last command path: ble | cloud | cloud_unconfirmed | none
        self.last_error = None
        self.checked = False
        self.session_dep = None  # deadline of the current plugged-in session (survives the next-day rollover)
        self.derate_seen = 0  # consecutive plans with actual current far below the set current
        self.fault_notified = None
        self.wake_bo = Backoff(self.o["wake_max_per_window"])
        self.on_bo = Backoff(self.o["charge_on_max_per_window"])
        # L-a: going live without a charge-limit entity could start on a restored stale SoC with no ceiling.
        self.limit_missing = not self.o["dry_run"] and not (
            self.o.get("charge_limit") or self.o.get("ble_charge_limit"))
        if self.limit_missing:
            log.error("dry_run is false but no charge_limit/ble_charge_limit is set: "
                      "starts are refused, stops still work")

    # ---------- inputs ----------
    def _st(self, states, key):
        eid = self.o.get(key) or ""
        return (states.get(eid) or {}).get("state") if eid else None

    def _attr(self, states, key, attr):
        eid = self.o.get(key) or ""
        return ((states.get(eid) or {}).get("attributes") or {}).get(attr) if eid else None

    def amp_cap(self, states):
        """The car's own current ceiling (number entity `max` attribute). It drops (e.g. to 6 A) when the
        Wall Connector derates; requesting more than this is rejected by the integration."""
        caps = [core.num(self._attr(states, k, "max")) for k in ("charge_current", "ble_charge_current")]
        caps = [c for c in caps if c is not None and c >= 1]
        return int(min(caps)) if caps else None

    def fault_state(self, states):
        o = self.o
        status = self._st(states, "wall_connector_status") if o.get("wall_connector_status") else None
        hf = None
        if o.get("handle_temp_sensor"):
            hf = core.temp_f(self._st(states, "handle_temp_sensor"),
                             self._attr(states, "handle_temp_sensor", "unit_of_measurement"))
        return core.wc_fault(status, hf, o.get("handle_temp_max_f")), hf

    def derated(self, states, soc, charging):
        """Actual current far below the set current, outside the taper zone, for derate_checks plans in a row."""
        if not self.o.get("actual_current_sensor") or charging is not True:
            self.derate_seen = 0
            return False
        act = core.num(self._st(states, "actual_current_sensor"))
        setp = core.num(self._st(states, "charge_current"))
        taper = self.o.get("taper_soc")
        in_taper = soc is not None and taper is not None and soc >= taper
        if act is None or setp is None or in_taper or act > setp - float(self.o["derate_margin_a"]):
            self.derate_seen = 0
            return False
        self.derate_seen += 1
        return self.derate_seen >= int(self.o["derate_checks"])

    def _flag(self, states, key):
        eid = self.o.get(key) or ""
        return bool(eid) and core.is_on(self._st(states, key))

    def _stale(self, states, key, now):
        """True if the entity's last_reported/last_updated is older than stale_minutes. Missing stamp = fresh."""
        eid = self.o.get(key) or ""
        st = states.get(eid) or {}
        ts = st.get("last_reported") or st.get("last_updated")
        if not ts:
            return False
        try:
            t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            n = now if now.tzinfo else now.astimezone()
            if t - n > timedelta(seconds=60):
                return True  # L-c: a stamp more than 60 s in the future is not trusted
            return (n - t) > timedelta(minutes=int(self.o["stale_minutes"]))
        except (TypeError, ValueError):
            return True

    SELF_CHECK_KEYS = ("charge_switch", "charge_current", "battery_level", "plugged_in", "charge_limit",
                       "charging_sensor", "online_sensor", "wake_button", "home_entity", "voltage_sensor",
                       "ble_charge_switch", "ble_charge_current", "ble_charge_limit", "ble_wake_button",
                       "peak_sensor", "wall_connector_status", "handle_temp_sensor", "actual_current_sensor",
                       "target_soc_entity", "departure_entity", "max_amps_entity", "enabled_entity",
                       "charge_now_entity", "pause_entity", "trip_entity")

    def self_check(self, states):
        """Startup report: which configured entities exist in HA and which are missing; setup mode."""
        found, missing = [], []
        for k in self.SELF_CHECK_KEYS:
            eid = self.o.get(k)
            if eid:
                (found if eid in states else missing).append("%s=%s" % (k, eid))
        need = [k for k in ("charge_switch", "charge_current", "battery_level", "plugged_in") if not self.o.get(k)]
        mode = "ble+cloud" if self.o.get("ble_charge_switch") else "cloud-only"
        log.info("self-check: mode=%s found=%d missing=%d", mode, len(found), len(missing))
        for m in missing:
            log.warning("self-check: entity not found in HA: %s", m)
        for k in need:
            log.warning("self-check: required option %s is empty; add-on stays idle", k)
        if not (self.o.get("peak_sensor") or self.o.get("peak_schedule")):
            log.warning("self-check: no peak_sensor or peak_schedule; every hour counts as off-peak")
        if not self.o.get("charge_now_entity"):
            log.info("self-check: no charge_now_entity; the card's Charge now chip is hidden")
        return {"mode": mode, "found": found, "missing": missing, "required_empty": need}

    def configured(self):
        return all(self.o.get(k) for k in ("charge_switch", "charge_current", "battery_level", "plugged_in"))

    def inputs(self, states, now):
        o = self.o
        trip = self._flag(states, "trip_entity")
        target = core.num(self._st(states, "target_soc_entity")) if o.get("target_soc_entity") else o["target_soc"]
        dep_hhmm = o["departure_time"]
        departure = None
        if o.get("departure_entity"):
            departure = core.as_local(now, self._st(states, "departure_entity"))
        if trip:
            target = o.get("trip_target_soc") or target
            if o.get("trip_departure_time"):
                departure = core.next_departure(now, o["trip_departure_time"])
        departure = departure or core.next_departure(now, dep_hhmm)
        max_amps = o["max_amps"]
        if o.get("max_amps_entity"):
            m = core.num(self._st(states, "max_amps_entity"))
            if m is not None:
                max_amps = int(max(o["min_amps"], min(m, o["max_amps"], HARD_MAX_AMPS)))
        plug = self._st(states, "plugged_in")
        plugged = None if core.num(plug) is None and str(plug).lower() in core.BAD else core.is_on(plug)
        soc = core.num(self._st(states, "battery_level"))
        last_soc, last_plugged = soc, plugged
        plug_fresh = not self._stale(states, "plugged_in", now)
        stale = self._stale(states, "battery_level", now) or self._stale(states, "plugged_in", now)
        if stale:
            soc, plugged = None, None  # stale = unknown: never start or raise
        peak = False
        if o.get("peak_sensor"):
            ps = self._st(states, "peak_sensor")
            # Unknown peak sensor = assume peak (fail safe).
            peak = True if str(ps).lower() in core.BAD else core.is_on(ps)
        peak = peak or core.in_schedule(now, o.get("peak_schedule"))
        home = None
        if o.get("home_entity"):
            home = core.is_on(self._st(states, "home_entity"))
        # Unknown charging state = None (not False): stops are still sent (only a definite False skips them).
        raw_ch = self._st(states, "charging_sensor" if o.get("charging_sensor") else "charge_switch")
        charging = None if str(raw_ch).strip().lower() in core.BAD else core.is_on(raw_ch)
        if plugged is False:
            self.session_dep = None
        departure = core.session_departure(self.session_dep, departure, now)
        fault, handle_f = self.fault_state(states)
        return {
            "in_session": self.session_dep is not None and departure == self.session_dep,
            "fault": fault, "handle_f": handle_f, "amp_cap": self.amp_cap(states),
            "derated": self.derated(states, soc, charging), "ramp_up_max_a": int(o["ramp_up_max_a"]),
            "now": now, "soc": soc, "stale": stale, "last_soc": last_soc,
            "last_plugged": last_plugged if plug_fresh else None,
            "target": core.num(target), "plugged": plugged, "home": home, "peak": peak,
            "enabled": (not o.get("enabled_entity")) or self._flag(states, "enabled_entity"),
            "charge_now": self._flag(states, "charge_now_entity"), "pause": self._flag(states, "pause_entity"),
            "trip": trip, "charging": charging, "amps_now": self._st(states, "charge_current"),
            "volts": core.clamp_voltage(self._st(states, "voltage_sensor"), o["fallback_voltage"]),
            "battery_kwh": float(o["battery_kwh"]), "efficiency": float(o["charge_efficiency"]),
            "departure": departure, "finish_buffer_min": o["finish_buffer_min"], "min_amps": o["min_amps"],
            "max_amps": max_amps, "schedule": o.get("peak_schedule"),
            "taper_soc": o.get("taper_soc"), "taper_factor": float(o.get("taper_factor") or 1.0),
        }

    # ---------- commands ----------
    def _call(self, domain, service, data):
        try:
            self.ha.call(domain, service, data)
            return True
        except Exception as e:  # noqa: BLE001 - log and fall back
            log.warning("%s.%s %s failed: %s", domain, service, data.get("entity_id"), e)
            self.last_error = "%s.%s %s: %s" % (domain, service, data.get("entity_id"), str(e)[:120])
            return False

    def _confirm(self, check):
        """Poll every 2 s for up to confirm_s seconds."""
        for i in range(max(1, int(self.o["confirm_s"]) // 2) + 1):
            try:
                if check(self.ha.get_states()):
                    return True
            except Exception as e:  # noqa: BLE001
                log.debug("confirm read failed: %s", e)
            if i < int(self.o["confirm_s"]) // 2:
                self.sleep(2)
        return False

    def _ble_then_cloud(self, ble_key, cloud_key, domain, service, extra, check):
        """Try BLE entity, wait for confirmation, else the cloud entity. Returns path used or None."""
        path = None
        if self.o.get(ble_key):
            if self._call(domain, service, dict(entity_id=self.o[ble_key], **extra)) and self._confirm(check):
                path = "ble"
            else:
                log.info("BLE %s.%s unconfirmed after %ss; falling back to cloud", domain, service,
                         self.o["confirm_s"])
        if path is None and self.o.get(cloud_key) and self._call(domain, service,
                                                                 dict(entity_id=self.o[cloud_key], **extra)):
            path = "cloud" if self._confirm(check) else "cloud_unconfirmed"
        self.transport = path or "none"
        return path

    def _charging(self, states):
        key = "charging_sensor" if self.o.get("charging_sensor") else "charge_switch"
        return core.is_on(self._st(states, key))

    def set_switch(self, on):
        svc = "turn_on" if on else "turn_off"
        return self._ble_then_cloud("ble_charge_switch", "charge_switch", "switch", svc, {},
                                    lambda st: self._charging(st) == on)

    def set_amps(self, amps):
        def ok(st):
            return core.num(self._st(st, "charge_current")) == float(amps) or core.num(
                self._st(st, "ble_charge_current")) == float(amps)
        return self._ble_then_cloud("ble_charge_current", "charge_current", "number", "set_value",
                                    {"value": int(amps)}, ok)

    def set_limit(self, pct, states):
        if not (self.o.get("charge_limit") or self.o.get("ble_charge_limit")):
            return None
        cur = core.num(self._st(states, "charge_limit"))
        if cur is not None and int(cur) == int(pct):
            return "unchanged"
        return self._ble_then_cloud("ble_charge_limit", "charge_limit", "number", "set_value",
                                    {"value": int(pct)},
                                    lambda st: core.num(self._st(st, "charge_limit")) == float(int(pct)))

    WAKE_MIN_GAP_S = 600

    def wake(self, now=None):
        """Every wake press goes through here: hard 10-min minimum gap (monotonic, survives backoff resets)."""
        if not self.o.get("online_sensor"):
            return False
        if core.is_on(self._st(self.ha.get_states(), "online_sensor")):
            return False
        if self.last_wake_mono is not None and self.mono() - self.last_wake_mono < self.WAKE_MIN_GAP_S:
            return False
        if not (self.o.get("ble_wake_button") or self.o.get("wake_button")):
            return False  # L-e: nothing to press, keep the budget
        if not self._wake_budget(now or self.clock()):
            return False
        self.last_wake_mono = self.mono()
        for key in ("ble_wake_button", "wake_button"):
            if self.o.get(key):
                self._call("button", "press", {"entity_id": self.o[key]})
                if self._confirm(lambda st: core.is_on(self._st(st, "online_sensor"))):
                    return

    # ---------- loop ----------
    def edges(self, states, now):
        """Return reasons to re-plan right now (peak-off edge, helper toggles, plug change)."""
        cur = {
            "peak": self.inputs_peak(states, now),
            "charge_now": self._flag(states, "charge_now_entity"),
            "pause": self._flag(states, "pause_entity"),
            "plugged": self._st(states, "plugged_in"),
            "trip": self._flag(states, "trip_entity"),
        }
        why = [k for k in cur if k in self.prev and self.prev[k] != cur[k]]
        # L4: edges only from definite readings. unavailable/unknown never creates a window or plug edge.
        peak_def = self.peak_definite_now(states, now)
        peak_off_edge = self.peak_definite is True and peak_def is False
        if self.peak_definite is None and peak_def is False and self.window_start is None:
            self.window_start = now  # started inside an off-peak window
        if peak_def is not None:
            self.peak_definite = peak_def
        raw = str(cur["plugged"]).strip().lower()
        plug_def = None if raw in core.BAD else core.is_on(raw)
        self.plug_on_edge = self.plug_definite is False and plug_def is True
        if plug_def is not None:
            self.plug_definite = plug_def
        self.prev = cur
        return why, peak_off_edge

    def peak_definite_now(self, states, now):
        """True/False from definite readings only; None if the peak sensor is unknown (still treated as peak
        for charging decisions, but it can never open a new window)."""
        sched = core.in_schedule(now, self.o.get("peak_schedule"))
        if self.o.get("peak_sensor"):
            ps = str(self._st(states, "peak_sensor")).strip().lower()
            if ps in core.BAD:
                return True if sched else None
            return core.is_on(ps) or sched
        return sched

    def inputs_peak(self, states, now):
        o = self.o
        p = False
        if o.get("peak_sensor"):
            ps = self._st(states, "peak_sensor")
            p = True if str(ps).lower() in core.BAD else core.is_on(ps)
        return p or core.in_schedule(now, o.get("peak_schedule"))

    def tick(self):
        now = self.clock()
        states = self.ha.get_states()
        if not self.checked:
            self.checked = True
            self.setup = self.self_check(states)
        if not self.configured():
            self.publish("not_configured", {})
            return None
        why, peak_off_edge = self.edges(states, now)
        if peak_off_edge:
            self.window_start = now
            self.notified_window = None
            if self.o.get("pause_entity") and self._flag(states, "pause_entity"):
                # Pause lasts until the next off-peak window: clear it at the window edge.
                self._clear("pause", states)
                states = self.ha.get_states()
        mono = self.mono()
        due = self.last_plan is None or (mono - self.last_plan) >= 60 * int(self.o["check_interval_min"])
        if not (due or why or peak_off_edge):
            return None
        self.last_plan = mono
        s = self.inputs(states, now)
        if peak_off_edge or self.plug_on_edge:
            self.reset_wake_backoff()  # L4: only a new off-peak window or a real off->on plug-in
            self.on_bo.reset()
        p = core.guard(core.plan(s), s)
        if p["action"] == "start" and p["reason"] not in ("charge_now", "late_plug_in"):
            self.session_dep = s["departure"]
        if s["stale"] and p["action"] != "idle":
            p.update(action="stop" if p["action"] == "stop" else "hold", reason="stale_state", amps=None)
        if s.get("fault"):
            # Fail safe: a Wall Connector fault or hot handle always STOPS, whatever the plan said
            # (charge-now, late plug-in, stale hold, past deadline, idle, disabled).
            p.update(action="stop", reason=s["fault"], amps=None)
        self.apply(p, s, states)
        self.check_notify(p, s, now)
        self.fault_notify(p, s)
        attrs = self.summary(p, s)
        attrs["trigger"] = ",".join(why + (["peak_end"] if peak_off_edge else [])) or "interval"
        self.publish(p["action"], attrs)
        log.info("plan %s amps=%s reason=%s soc=%s target=%s peak=%s dry_run=%s", p["action"], p["amps"],
                 p["reason"], s["soc"], s["target"], s["peak"], self.o["dry_run"])
        return p

    def _clear(self, flag, states):
        key = {"charge_now": "charge_now_entity", "pause": "pause_entity", "trip": "trip_entity"}[flag]
        if self.o.get(key) and self._flag(states, key) and not self.o["dry_run"]:
            self._call("input_boolean", "turn_off", {"entity_id": self.o[key]})

    def apply(self, p, s, states):
        dry = bool(self.o["dry_run"])
        for f in p["clear"]:
            self._clear(f, states)
        if p["action"] == "hold" and p["reason"] == "stale_state":
            if not dry and self.stale_wake_allowed(s):
                self.wake(s["now"])  # waking may refresh stale readings; no charge command
            return
        if p["action"] == "stop":
            if s["charging"] is False:
                return
            if dry and not (self.o.get("dry_run_safety_off", True) and p["reason"] in SAFETY_STOPS):
                return
            self.set_switch(False)
        elif p["action"] == "start":
            if dry:
                return  # dry run never turns charging ON or changes amps
            if self.limit_missing:
                log.warning("start refused: configure charge_limit or ble_charge_limit before dry_run: false")
                return
            self.wake(s["now"])  # same cap/backoff/min-gap as the stale path
            self.set_limit(s["target"], states)
            if core.amps_changed(p["amps"], s["amps_now"]):
                self.set_amps(p["amps"])
            if s["charging"] is not True:
                if self.on_bo.take(self.mono()):
                    (log.debug if self.on_bo.count > 1 else log.info)("charge on (attempt %d)", self.on_bo.count)
                    self.set_switch(True)
                else:
                    log.debug("charge-on retry held by backoff/cap (%d sent this window)", self.on_bo.count)

    @property
    def wake_count(self):
        return self.wake_bo.count

    def reset_wake_backoff(self):
        self.wake_bo.reset()

    def stale_wake_allowed(self, s):
        """L3: wake a stale car only when it is worth it, with exponential backoff and a per-window cap."""
        if s.get("last_plugged") is not True:
            return False  # plug must be known plugged-in (fresh reading)
        if s.get("home") is False:
            return False
        if s.get("peak") and not s.get("charge_now"):
            return False
        if s.get("last_soc") is None or s.get("target") is None or s["last_soc"] >= s["target"]:
            return False
        return True

    def _wake_budget(self, now):
        """Per-window cap + exponential backoff, shared by every wake path. Consumes one wake when True."""
        return self.wake_bo.take(self.mono())

    def check_notify(self, p, s, now):
        mins = int(self.o["notify_not_charging_min"])
        if not mins or p["action"] != "start" or s["charging"] or self.window_start is None:
            return
        if self.notified_window == self.window_start or now - self.window_start < timedelta(minutes=mins):
            return
        self.notified_window = self.window_start
        try:
            self.ha.notify("Tesla Smart Charge",
                           "Plugged in at %s%% but not charging %d min after the off-peak window opened."
                           % (s["soc"], mins))
        except Exception as e:  # noqa: BLE001
            log.warning("notify failed: %s", e)

    def fault_notify(self, p, s):
        """One notification per fault episode (clears when the fault clears)."""
        if p["reason"] not in ("wall_connector_fault", "handle_too_hot"):
            self.fault_notified = None
            return
        if self.fault_notified == p["reason"]:
            return
        self.fault_notified = p["reason"]
        msg = ("Wall Connector reports a fault; charging stopped."
               if p["reason"] == "wall_connector_fault" else
               "Wall Connector handle reads %.0f F; charging stopped. Inspect the handle."
               % (s["handle_f"] or 0))
        try:
            self.ha.notify("Tesla Smart Charge", msg)
        except Exception as e:  # noqa: BLE001
            log.warning("notify failed: %s", e)

    def mode(self, p, s):
        if not s.get("enabled", True):
            return "disabled"
        if s.get("charge_now"):
            return "charge_now"
        if s.get("pause"):
            return "paused"
        if s.get("trip"):
            return "trip"
        return "smart"

    def summary(self, p, s):
        """Rich status for the card: plan, ETA, cost, mode, transport, next action, last error."""
        o, now = self.o, s["now"]
        wall, equiv = (0.0, 0.0)
        if s.get("soc") is not None and s.get("target") is not None:
            wall, equiv = core.taper_equiv_kwh(s["soc"], s["target"], s["battery_kwh"], s["efficiency"],
                                               s.get("taper_soc"), s.get("taper_factor", 1.0))
        amps = p.get("amps")
        start = now
        if p["action"] != "start" and s.get("peak") and not s.get("charge_now"):
            start = core.next_offpeak(now, o.get("peak_schedule")) if o.get("peak_schedule") else None
        h = core.hours_at(amps, s["volts"], equiv) if amps else None
        eta = (start + timedelta(hours=h)) if (start and h) else None
        finish_by = s["departure"] - timedelta(minutes=int(s.get("finish_buffer_min", 0)))
        cost = core.est_cost(start or now, h, wall, o.get("rates"), o.get("default_price")) if h else None
        if p["action"] == "start":
            nxt = "charging at %s A" % amps if s.get("charging") else "start charging at %s A" % amps
        elif p["action"] == "stop":
            nxt = {"wall_connector_fault": "Wall Connector fault: stopped", "handle_too_hot": "handle hot: stopped",
                   "peak": "wait for off-peak", "target_reached": "done", "paused_until_next_window":
                   "paused until next off-peak window", "charge_now_complete": "done"}.get(p["reason"], "stop")
        elif p["action"] == "hold":
            nxt = {"past_finish_time": "deadline passed: no more adjustments"}.get(
                p["reason"], "waiting for fresh car data")
        else:
            nxt = {"unplugged": "plug in to plan", "not_home": "away from home", "disabled": "disabled"}.get(
                p["reason"], "idle")
        iso = (lambda d: d.isoformat() if d else None)
        return {
            "reason": p["reason"], "mode": self.mode(p, s), "amps": amps, "plan_amps": amps,
            "behind": p["behind"], "soc": s["soc"], "target": s["target"], "plugged": s["plugged"],
            "charging": s["charging"], "peak": s["peak"], "kwh_needed": round(wall, 2),
            "start_at": iso(start), "finish_by": iso(finish_by), "eta_finish": iso(eta),
            "departure": iso(s["departure"]), "est_cost": cost, "currency": o.get("currency", "$"),
            "volts": s["volts"], "max_amps": s["max_amps"],
            "amp_cap": s.get("amp_cap"),
            "fault": s.get("fault"), "derated": bool(s.get("derated")), "handle_temp_f": s.get("handle_f"),
            "transport": self.transport,
            "setup_mode": "ble+cloud" if o.get("ble_charge_switch") else "cloud-only",
            "next_action": nxt, "last_error": self.last_error, "dry_run": bool(o["dry_run"]),
            "charge_now_entity": o.get("charge_now_entity") or None, "pause_entity": o.get("pause_entity") or None,
            "trip_entity": o.get("trip_entity") or None, "enabled_entity": o.get("enabled_entity") or None,
        }

    def publish(self, state, attrs):
        self.status = dict(attrs, state=state)
        try:
            self.ha.set_status(state, dict(attrs, friendly_name="Tesla Smart Charge"))
        except Exception as e:  # noqa: BLE001
            log.debug("status publish failed: %s", e)

    def allowed_entities(self):
        return [self.o.get(k) for k in ENTITY_KEYS]
