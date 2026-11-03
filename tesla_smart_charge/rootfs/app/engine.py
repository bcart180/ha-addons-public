"""Tesla Smart Charge engine: reads HA, plans with core.plan, commands BLE first then cloud."""
import logging
import time
from datetime import datetime, timedelta

import core

log = logging.getLogger("tesla_smart_charge")

DEFAULTS = {"dry_run": True, "dry_run_safety_off": True, "check_interval_min": 10, "tick_s": 30,
            "confirm_s": 20, "battery_kwh": 75, "charge_efficiency": 0.9, "target_soc": 80,
            "departure_time": "07:00", "finish_buffer_min": 30, "min_amps": 5, "max_amps": 32,
            "fallback_voltage": 240,
            "stale_minutes": 30, "wake_max_per_window": 5, "peak_schedule": [], "notify_not_charging_min": 20}
HARD_MAX_AMPS = 48
SAFETY_STOPS = ("stale_state", "peak", "plug_state_unavailable", "soc_or_target_unavailable")
ENTITY_KEYS = ("charge_switch", "charge_current", "charge_limit", "wake_button", "ble_charge_switch",
               "ble_charge_current", "ble_charge_limit", "ble_wake_button", "charge_now_entity",
               "pause_entity", "trip_entity")


class Engine:
    def __init__(self, opts, ha, clock=None, sleep=None):
        self.o = dict(DEFAULTS, **{k: v for k, v in opts.items() if v not in (None,)})
        self.ha = ha
        self.o["max_amps"] = max(1, min(int(self.o["max_amps"]), HARD_MAX_AMPS))
        if int(self.o["min_amps"]) > self.o["max_amps"]:
            log.warning("min_amps %s > max_amps %s: using max_amps for both (max wins)",
                        self.o["min_amps"], self.o["max_amps"])
            self.o["min_amps"] = self.o["max_amps"]
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.sleep = sleep or time.sleep
        self.prev = {}
        self.last_plan = None  # monotonic-ish: datetime of last full plan
        self.last_amps_cmd = None
        self.window_start = None
        self.notified_window = None
        self.status = {}
        self.wake_count = 0
        self.next_wake_at = None

    # ---------- inputs ----------
    def _st(self, states, key):
        eid = self.o.get(key) or ""
        return (states.get(eid) or {}).get("state") if eid else None

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
            return (n - t) > timedelta(minutes=int(self.o["stale_minutes"]))
        except (TypeError, ValueError):
            return True

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
        charging = None
        if o.get("charging_sensor"):
            charging = core.is_on(self._st(states, "charging_sensor"))
        else:
            charging = core.is_on(self._st(states, "charge_switch"))
        return {
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
        }

    # ---------- commands ----------
    def _call(self, domain, service, data):
        try:
            self.ha.call(domain, service, data)
            return True
        except Exception as e:  # noqa: BLE001 - log and fall back
            log.warning("%s.%s %s failed: %s", domain, service, data.get("entity_id"), e)
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
        if self.o.get(ble_key):
            if self._call(domain, service, dict(entity_id=self.o[ble_key], **extra)) and self._confirm(check):
                return "ble"
            log.info("BLE %s.%s unconfirmed after %ss; falling back to cloud", domain, service, self.o["confirm_s"])
        if self.o.get(cloud_key) and self._call(domain, service, dict(entity_id=self.o[cloud_key], **extra)):
            return "cloud" if self._confirm(check) else "cloud_unconfirmed"
        return None

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

    def wake(self):
        if not self.o.get("online_sensor"):
            return
        if core.is_on(self._st(self.ha.get_states(), "online_sensor")):
            return
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
        peak_off_edge = self.prev.get("peak") is True and cur["peak"] is False
        if "peak" not in self.prev and not cur["peak"]:
            self.window_start = now  # started inside an off-peak window
        self.prev = cur
        return why, peak_off_edge

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
        due = self.last_plan is None or (now - self.last_plan) >= timedelta(minutes=int(self.o["check_interval_min"]))
        if not (due or why or peak_off_edge):
            return None
        self.last_plan = now
        s = self.inputs(states, now)
        if not s["stale"] or peak_off_edge or "plugged" in why:
            self.reset_wake_backoff()  # fresh data, new window or plug change
        p = core.plan(s)
        if s["stale"] and p["action"] != "idle":
            p.update(action="stop" if p["action"] == "stop" else "hold", reason="stale_state", amps=None)
        self.apply(p, s, states)
        self.check_notify(p, s, now)
        self.publish(p["action"], {"reason": p["reason"], "amps": p["amps"], "behind": p["behind"],
                                   "soc": s["soc"], "target": s["target"], "peak": s["peak"],
                                   "departure": s["departure"].isoformat(), "dry_run": bool(self.o["dry_run"]),
                                   "trigger": ",".join(why + (["peak_end"] if peak_off_edge else [])) or "interval"})
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
                self.wake()  # waking may refresh stale readings; no charge command
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
            self.wake()
            self.set_limit(s["target"], states)
            if core.amps_changed(p["amps"], s["amps_now"]):
                self.set_amps(p["amps"])
            if not s["charging"]:
                self.set_switch(True)

    WAKE_BACKOFF_MIN = (10, 20, 40, 80, 120)

    def reset_wake_backoff(self):
        self.wake_count, self.next_wake_at = 0, None

    def stale_wake_allowed(self, s):
        """L3: wake a stale car only when it is worth it, with exponential backoff and a per-window cap."""
        now = s["now"]
        if s.get("last_plugged") is not True:
            return False  # plug must be known plugged-in (fresh reading)
        if s.get("home") is False:
            return False
        if s.get("peak") and not s.get("charge_now"):
            return False
        if s.get("last_soc") is None or s.get("target") is None or s["last_soc"] >= s["target"]:
            return False
        if self.wake_count >= int(self.o["wake_max_per_window"]):
            return False
        if self.next_wake_at is not None and now < self.next_wake_at:
            return False
        delay = self.WAKE_BACKOFF_MIN[min(self.wake_count, len(self.WAKE_BACKOFF_MIN) - 1)]
        self.wake_count += 1
        self.next_wake_at = now + timedelta(minutes=delay)
        return True

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

    def publish(self, state, attrs):
        self.status = dict(attrs, state=state)
        try:
            self.ha.set_status(state, dict(attrs, friendly_name="Tesla Smart Charge"))
        except Exception as e:  # noqa: BLE001
            log.debug("status publish failed: %s", e)

    def allowed_entities(self):
        return [self.o.get(k) for k in ENTITY_KEYS]
