"""Smart HVAC engine: reads HA state, runs core.decide() per zone, applies (or logs) the result.

Pure Python; the HA client is injected so tests run without Home Assistant.
Safety contract (carried from PR 36 / MA review):
  * master `enabled` false or `dry_run` true -> never writes to HA.
  * any missing input -> core holds the last setpoint/mode (fail-safe).
  * M1: a unit turned off by a person (wall, remote, vendor app) is never turned back on.
  * radiant over-temp -> radiant zone off, independent of profile, stays off until a person turns it on.
  * anti-short-cycle via core min_cycle_s.
  * manual change in HA (setpoint or mode differs from what the app last set) -> override,
    auto-resume after override_hours.
Presence comes ONLY from options.presence_entity + presence_entities (any listed occupant home).
"""
import calendar
import json
import logging
import math
import os
import time

import core
from ha_client import excluded

RADIANT_ROOM_CAP_MAX = 85.0  # hard ceiling for both radiant caps, whatever the options say (MA L2)
RADIANT_SETPOINT_MAX_DEFAULT = 85.0  # floor setpoint cap (owner request, 30 Oct)
RADIANT_AIR_SHUTOFF_DEFAULT = 80.0  # bath air shutoff stays 80
RADIANT_TIMEOUT_ENTITY = "automation.radiant_heat_timeout_90_min"
# a unit may round what the app wrote (60 -> 61 on a Celsius-native split); a reported setpoint within this
# many degrees F of the app's last write for that zone is the app's own write, not a manual change.
OWN_WRITE_TOLERANCE_F = 1.0
VACATION_ENTITY = "input_boolean.vacation_mode"  # same helper vacation_mode_handler reads
VACATION_HEAT_DEFAULT = 60.0
VACATION_COOL_DEFAULT = 82.0
HISTORY_MAX = 288  # 24 h at 5 min
# 0.4.0: HA's standard hvac_action values for a heat pump that is defrosting / warming its coil. While a
# unit reports one of these the app does not treat what it sees as a manual change, does not count it as a
# mode change (short cycle) and does not write: the unit is busy and will go back to its setpoint itself.
DEFROST_ACTIONS = ("defrosting", "preheating")
EVENT_KINDS = ("hold", "window", "vacation", "defrost", "preheat", "defrost_stuck")
# MA L1 (0.4.1): a unit stuck reporting defrosting / preheating must not freeze the app forever. After this
# long in one continuous spell the app goes back to normal logic and records a defrost_stuck event.
DEFROST_TIMEOUT_S = 30 * 60
# 0.5.1 `defrost_aware`. Shipped default is on (config.yaml, 0.8.9). A missing key still means off, so the
# 0.5.0 golden stays byte-identical when the option is absent or false. When on (MA conditions):
#  * every exemption window (reported or inferred) is capped at DEFROST_AWARE_CAP_S and at most
#    DEFROST_AWARE_MAX_PER_HOUR windows may start in any 60 min; past either limit normal short-cycle / hold
#    logic resumes, so a failing unit is never masked (past the cap it is flagged defrost_stuck as in 0.4.1).
#  * for DEFROST_GRACE_S after an exempt window ends normally, short-cycle and setpoint-chase stay skipped.
#  * the official hvac_action defrosting / preheating is always honoured (HA dev blog 2024-07-07) and counts
#    as heating-in-progress. It is not heat delivered (learned warm-up / coast do not record it).
#  * fallback inference only on heat_pump zones that are not heat_only (not radiant, not the spare heater),
#    in heat mode, with a KNOWN outdoor temp at or below DEFROST_INFER_OUTDOOR_MAX_F, when the room or a
#    reported supply temperature falls by at least DEFROST_INFER_DROP_F per 3 min. The unit must be heating,
#    or just have been heating and gone idle (these heads never report defrosting). Cool mode never infers.
#    Unknown / unavailable outdoor temp -> no inference. Debug logs only; no new entities, writes or perms.
DEFROST_AWARE_CAP_S = 15 * 60
DEFROST_AWARE_MAX_PER_HOUR = 4
DEFROST_GRACE_S = 10 * 60
DEFROST_INFER_OUTDOOR_MAX_F = 40.0
DEFROST_INFER_DROP_F = 0.5  # per 3 minutes
DEFROST_INFER_RATE_S = 180
DEFROST_SAMPLE_MAX_AGE_S = 900
EVENTS_MAX = 200
NEXT_STEP_MIN = 5
PROJECTION_TAU_MIN = 90.0  # projected room temp relaxes toward the projected setpoint with this time constant
log = logging.getLogger("smart_hvac")


def _ts(iso):
    """HA last_changed ISO string -> epoch seconds, or None."""
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _num(x):
    return core._num(x)


def _supply_f(cattr):
    """Supply-air temperature when the climate entity already reports one. Otherwise the room is the signal."""
    for key in ("supply_temperature", "supply_temp"):
        v = _num((cattr or {}).get(key))
        if v is not None:
            return v
    return None


def _step_intervals(pts, end):
    out = []
    for i, (t, on) in enumerate(pts):
        if not on:
            continue
        t2 = pts[i + 1][0] if i + 1 < len(pts) else end
        if t2 > t:
            out.append((t, t2))
    return out


def _union_intervals(intervals):
    merged = []
    for a, b in sorted(intervals):
        if b <= a:
            continue
        if not merged or a > merged[-1][1]:
            merged.append([a, b])
        else:
            merged[-1][1] = max(merged[-1][1], b)
    return [(a, b) for a, b in merged]


def _intervals_to_steps(intervals):
    pts = []
    for a, b in intervals:
        pts.append([int(a), 1])
        pts.append([int(b), 0])
    return pts


def _event_intervals(events, kind, start, end):
    """Pair start/end events into [start, end] intervals clipped to the window. An open span runs to `end`."""
    open_t = None
    iv = []
    for row in sorted(events or [], key=lambda e: e[0]):
        if len(row) < 3 or row[1] != kind:
            continue
        t, what = row[0], row[2]
        if what == "start":
            if open_t is None:
                open_t = t
        elif open_t is not None:
            iv.append((max(open_t, start), min(t, end)))
            open_t = None
        else:
            iv.append((start, min(t, end)))
    if open_t is not None:
        a = max(open_t, start)
        iv.append((a, end if end > a else a + 1))
    return [(a, b) for a, b in iv if b > a]


class Engine:
    def __init__(self, opts, ha, state_path=None, clock=time.time, localtime=time.localtime):
        self.opts = opts
        self.ha = ha
        self.clock = clock
        self.localtime = localtime
        self.state_path = state_path
        self.st = {"profile": "auto", "app_off": [], "last_change": {}, "last_cmd": {},
                   "override_until": {}, "history": {}, "last_result": {}, "events": {}, "active": {},
                   "defrost_since": {}, "window": {}, "window_saved": {}}
        self._load()
        self.st.setdefault("events", {})
        self.st.setdefault("active", {})
        self.st.setdefault("defrost_since", {})
        self.st.setdefault("window", {})
        self.st.setdefault("window_saved", {})
        self.prune_zones()
        for w in self.legacy_warnings():
            log.warning(w)

    # ---------- persistence ----------
    def _load(self):
        if self.state_path and os.path.exists(self.state_path):
            try:
                with open(self.state_path) as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self.st.update(loaded)
                else:
                    log.debug("state file: top level is %s, not an object; ignored", type(loaded).__name__)
            except (OSError, ValueError):
                pass
        self._sanitize_state()
        # After a restart every "off" is treated as a person's off (M1), like the pyscript version.
        self.st["app_off"] = []

    # MA L1 (0.5.1): a state file that parses but has the wrong shape must not error every cycle.
    _STATE_DICTS = ("last_change", "last_cmd", "override_until", "history", "last_result", "events",
                    "active", "defrost_since", "window", "window_saved", "defrost_aware")

    @staticmethod
    def _isnum(x):
        return isinstance(x, (int, float)) and not isinstance(x, bool)

    def _zone_ok(self, key, v):
        n = self._isnum
        if key in ("last_change", "defrost_since", "override_until"):
            return n(v)
        if key in ("last_cmd", "window", "window_saved"):
            return isinstance(v, dict)
        if key == "history":
            return isinstance(v, list) and all(isinstance(h, list) for h in v)
        if key == "events":
            return isinstance(v, list) and all(isinstance(e, list) and len(e) >= 3 and n(e[0]) for e in v)
        if key == "active":
            return isinstance(v, list) and all(isinstance(k, str) for k in v)
        if key == "defrost_aware":
            return isinstance(v, dict)
        return True

    def _sanitize_defrost_entry(self, name, v):
        """Keep well-formed samples / episodes; reset the rest of a malformed defrost_aware zone entry."""
        n = self._isnum
        samples = v.get("samples")
        if not isinstance(samples, list):
            samples = []
        good = [s for s in samples if isinstance(s, list) and len(s) in (3, 4) and n(s[0]) and n(s[1])
                and (s[2] is None or isinstance(s[2], str))
                and (len(s) == 3 or s[3] is None or n(s[3]))]
        eps = v.get("episodes")
        if not isinstance(eps, list):
            eps = []
        geps = [t for t in eps if n(t)]
        since = v.get("since")
        exempt = v.get("exempt")
        ended = v.get("ended")
        bad = (good != v.get("samples") or geps != v.get("episodes") or not (since is None or n(since))
               or not isinstance(exempt, bool) or not (ended is None or n(ended)))
        if bad:
            log.debug("state: malformed defrost_aware entry for %s reset", name)
        out = {"samples": good, "episodes": geps, "since": since if n(since) else None,
               "exempt": exempt if isinstance(exempt, bool) else False}
        if n(ended):
            out["ended"] = ended
        return out

    def _sanitize_state(self):
        st = self.st
        if not isinstance(st.get("profile"), str) or st["profile"] not in ("auto",) + core.PROFILES:
            if "profile" in st and st.get("profile") != "auto":
                log.debug("state: bad profile %r reset to auto", st.get("profile"))
            st["profile"] = "auto"
        for key in self._STATE_DICTS:
            if key in st and not isinstance(st[key], dict):
                log.debug("state: %s is %s, not an object; reset", key, type(st[key]).__name__)
                st[key] = {}
            if key not in st or key == "last_result":
                continue
            for name in list(st[key]):
                v = st[key][name]
                if not self._zone_ok(key, v):
                    log.debug("state: malformed %s entry for %s reset", key, name)
                    st[key].pop(name)
                elif key == "defrost_aware":
                    st[key][name] = self._sanitize_defrost_entry(name, v)
        lr = st["last_result"]
        if lr and not isinstance(lr.get("zones", {}), dict):
            log.debug("state: last_result.zones malformed; last_result reset")
            st["last_result"] = {}
        elif isinstance(lr.get("zones"), dict):
            for name in [k for k, v in lr["zones"].items() if not isinstance(v, dict)]:
                log.debug("state: malformed last_result zone %s dropped", name)
                lr["zones"].pop(name)

    def save(self):
        if not self.state_path:
            return
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.st, f)
        os.replace(tmp, self.state_path)

    # ---------- helpers ----------
    @property
    def enabled(self):
        return bool(self.opts.get("enabled", False))

    @property
    def dry_run(self):
        return bool(self.opts.get("dry_run", True))

    @property
    def writes_allowed(self):
        return self.enabled and not self.dry_run

    def set_profile(self, p):
        if p not in ("auto",) + core.PROFILES:
            raise ValueError("bad profile")
        self.st["profile"] = p
        self.save()

    def _cfg(self, zone):
        prof = self.opts.get("profiles", {}) or {}
        c = {k: float(v) for k, v in prof.items() if _num(v) is not None and k in core.DEFAULTS}
        for k in ("sleep_start", "wake"):
            if prof.get(k):
                c[k] = str(prof[k])
        if _num(self.opts.get("min_cycle_min")) is not None:
            c["min_cycle_s"] = float(self.opts["min_cycle_min"]) * 60
        for k in ("radiant_floor_cap", "cool_lockout"):
            if _num(self.opts.get(k)) is not None:
                c[k] = float(self.opts[k])
        c["radiant_room_cap"] = self.radiant_room_cap  # air shutoff, default 80, clamp 85
        c["radiant_setpoint_max"] = self.radiant_setpoint_max  # 0.5.0: clamps a radiant window restore
        # per-zone targets (radiant floor): comfort -> home/guest, sleep, away -> both away targets
        for src, dst in (("comfort", ("home", "guest")), ("sleep", ("sleep",)),
                         ("away", ("away_heat", "away_cool"))):
            if _num(zone.get(src)) is not None:
                for k in dst:
                    c[k] = float(zone[src])
        return c

    def _get(self, states, eid):
        if not eid:
            return None, {}
        s = states.get(eid)
        if not s or s.get("state") in (None, "unknown", "unavailable"):
            return None, (s or {}).get("attributes", {}) if s else {}
        return s.get("state"), s.get("attributes", {}) or {}

    @staticmethod
    def _unit_temp(cattr):
        """0.8.8: the unit's current_temperature as a fallback, in F. Values outside 30-110 F
        (converted from C when the unit reports `temperature_unit: C`) are treated as missing."""
        t = _num((cattr or {}).get("current_temperature"))
        if t is None:
            return None
        unit = str((cattr or {}).get("temperature_unit") or "").upper().lstrip("\u00b0")
        f = t * 9 / 5 + 32 if unit == "C" else t
        return t if 30 <= f <= 110 else None

    def _indoor_stale(self, states, z, now):
        """0.8.7: True when the zone's `indoor` sensor has not reported for `indoor_stale_min` minutes.
        Uses HA `last_reported` (2024.3+, moves on every write even when the value repeats), then
        `last_updated`. Option unset/0, no indoor sensor, or no timestamp -> False (0.8.6 behaviour)."""
        try:
            lim = float(z.get("indoor_stale_min") or 0)
        except (TypeError, ValueError):
            lim = 0
        eid = z.get("indoor")
        if lim <= 0 or not eid:
            return False
        s = states.get(eid) or {}
        ts = _ts(s.get("last_reported")) or _ts(s.get("last_updated"))
        if ts is None:
            return False
        stale = (now - ts) > lim * 60
        if stale:
            log.debug("zone %s: indoor sensor stale (%.0f min), using unit temperature", z.get("name"), (now - ts) / 60)
        return stale

    def _override_active(self, name, now):
        return self.st["override_until"].get(name, 0) > now

    def start_override(self, name, hours=None):
        h = _num(hours) or _num(self.opts.get("override_hours")) or 2.0
        self.st["override_until"][name] = self.clock() + h * 3600
        self.save()

    def clear_override(self, name):
        self.st["override_until"].pop(name, None)
        self.save()

    def resume(self, name):
        """Resume auto: drop the hold AND forget the last command so the next evaluate() restores the
        scheduled setpoint instead of mistaking the card's own write for a new manual change."""
        self.st["override_until"].pop(name, None)
        self.st["last_cmd"].pop(name, None)
        self.save()

    def note_card_write(self, name, mode=None, setpoint=None, fan_mode=None):
        """A card write went to HA: reflect it in the snapshot now (not at the next 5-min cycle) and
        record it as the last command, so the hold/resume logic compares against what was written."""
        zr = (self.st.get("last_result") or {}).get("zones", {}).get(name)
        live = zr.setdefault("live", {}) if zr is not None else {}
        cmd = dict(self.st["last_cmd"].get(name) or {"mode": live.get("mode"), "setpoint": live.get("setpoint")})
        if mode is not None:
            live["mode"] = cmd["mode"] = mode
        if setpoint is not None:
            live["setpoint"] = cmd["setpoint"] = _num(setpoint)
        if fan_mode is not None:
            live["fan_mode"] = fan_mode
        if zr is not None:
            zr["reason"], zr["action"] = "manual_override", "hold"
        self.st["last_cmd"][name] = cmd
        self.save()

    # ---------- vacation ----------
    @property
    def vacation_entity(self):
        return self.opts.get("vacation_entity") or VACATION_ENTITY

    def vacation_targets(self):
        h = _num(self.opts.get("vacation_heat"))
        c = _num(self.opts.get("vacation_cool"))
        return (h if h is not None else VACATION_HEAT_DEFAULT), (c if c is not None else VACATION_COOL_DEFAULT)

    def vacation_on(self, states=None):
        if states is None:
            states = {s["entity_id"]: s for s in self.ha.get_states()}
        v, _ = self._get(states, self.vacation_entity)
        return v == "on"

    def presence_entities(self):
        """Configured occupants: presence_entities (list) plus legacy presence_entity. Blank slots ignored."""
        ents = [e for e in (self.opts.get("presence_entities") or []) if e]
        legacy = self.opts.get("presence_entity")
        if legacy and legacy not in ents:
            ents.insert(0, legacy)
        return ents

    def occupancy(self, states):
        """True if ANY configured occupant is home; False if all known ones are away;
        None (fail-safe hold) only when no occupant entity is readable."""
        vals = [self._get(states, e)[0] for e in self.presence_entities()]
        known = [v for v in vals if v is not None]
        if not known:
            return None
        return any(v == "home" for v in known)

    @staticmethod
    def _live(cl, cattr, indoor, hum):
        """Read-only unit facts for the thermostat cards (no writes)."""
        return {"live": {"mode": cl, "setpoint": _num(cattr.get("temperature")), "indoor": indoor,
                         "humidity": _num(hum) if _num(hum) is not None else _num(cattr.get("current_humidity")),
                         "hvac_action": cattr.get("hvac_action"), "fan_mode": cattr.get("fan_mode"),
                         "fan_modes": list(cattr.get("fan_modes") or []),
                         "hvac_modes": list(cattr.get("hvac_modes") or [])}}

    @property
    def radiant_room_cap(self):
        """Air shutoff: bath air above this -> radiant off. Option `radiant_air_shutoff` (default 80, max 85).
        The legacy `radiant_room_cap` option is accepted by the schema but ignored."""
        v = _num(self.opts.get("radiant_air_shutoff"))
        return min(v if v is not None else RADIANT_AIR_SHUTOFF_DEFAULT, RADIANT_ROOM_CAP_MAX)

    @property
    def radiant_setpoint_max(self):
        """Highest floor setpoint a card / panel may request. Option `radiant_setpoint_max` (default 85, max 85)."""
        v = _num(self.opts.get("radiant_setpoint_max"))
        return min(v if v is not None else RADIANT_SETPOINT_MAX_DEFAULT, RADIANT_ROOM_CAP_MAX)

    def _defrost_grace(self, st, now):
        ended = _num(st.get("ended"))
        return ended is not None and 0 <= now - ended <= DEFROST_GRACE_S

    def _defrost_aware(self, name, z, act, cl, indoor, outdoor, cattr, now):
        """0.5.1 bounded defrost exemption (only called with defrost_aware on). Returns
        (exempt, stuck, inferred, grace). Read-only: keeps a few temperature samples in the state file.
        grace is true for DEFROST_GRACE_S after an exempt window ends, and never while a window is refused
        (hourly budget or the 15 min cap) so a stuck unit is not masked."""
        st = self.st.setdefault("defrost_aware", {}).setdefault(
            name, {"samples": [], "episodes": [], "since": None, "exempt": False})
        official = act in DEFROST_ACTIONS
        tin, tout = _num(indoor), _num(outdoor)
        supply = _supply_f(cattr)
        guess = False
        # Radiant (heat_only) and the spare heater are not heat pumps. Cool never looks like a defrost.
        if not official and z.get("heat_pump") and not z.get("heat_only") and cl == "heat":
            if tout is None:
                log.debug("defrost %s: outdoor temp unknown, no inference", name)
            elif tout > DEFROST_INFER_OUTDOOR_MAX_F:
                log.debug("defrost %s: outdoor %.1f above %.0f, no inference", name, tout,
                          DEFROST_INFER_OUTDOOR_MAX_F)
            elif tin is not None:
                fresh = [s for s in st["samples"] if 0 < now - s[0] <= DEFROST_SAMPLE_MAX_AGE_S]
                if act == "heating":
                    prev = [s for s in fresh if s[2] == "heating"]
                    baseline = prev[-1] if prev else None
                elif act == "idle" and fresh and fresh[-1][2] == "heating":
                    # These heads go idle for a defrost; they never report hvac_action defrosting.
                    baseline = fresh[-1]
                else:
                    baseline = None
                if baseline is not None:
                    t0, v0 = baseline[0], baseline[1]
                    rates = [(v0 - tin) * DEFROST_INFER_RATE_S / (now - t0)]
                    sold = _num(baseline[3]) if len(baseline) >= 4 else None
                    if supply is not None and sold is not None:
                        rates.append((sold - supply) * DEFROST_INFER_RATE_S / (now - t0))
                    guess = any(r >= DEFROST_INFER_DROP_F for r in rates)
                    log.debug("defrost %s: room %.2f F per 3 min while %s -> inferred=%s", name, rates[0], act, guess)
        if tin is not None:
            st["samples"] = [s for s in st["samples"] if now - s[0] <= DEFROST_SAMPLE_MAX_AGE_S][-5:]
            row = [now, tin, act]
            if supply is not None:
                row.append(supply)
            st["samples"].append(row)
        st["episodes"] = [t for t in st["episodes"] if now - t < 3600]
        if not (official or guess):
            if st["since"] is not None:
                log.debug("defrost %s: window ended after %ds", name, now - st["since"])
                if st.get("exempt"):
                    st["ended"] = now
            st["since"], st["exempt"] = None, False
            return False, False, False, self._defrost_grace(st, now)
        if st["since"] is None:  # a new window starts
            st["since"] = now
            st["exempt"] = len(st["episodes"]) < DEFROST_AWARE_MAX_PER_HOUR
            if st["exempt"]:
                st["episodes"].append(now)
            else:
                log.debug("defrost %s: %d windows in the last hour, not exempt (normal logic)", name,
                          len(st["episodes"]))
        if not st["exempt"]:
            return False, False, False, False
        if now - st["since"] > DEFROST_AWARE_CAP_S:
            log.debug("defrost %s: window over %ds cap, normal logic", name, DEFROST_AWARE_CAP_S)
            return False, True, False, False
        return True, False, not official, False

    def prune_zones(self):
        """MA L2 (0.4.1): drop events / active kinds / defrost timers of zones no longer in the options."""
        names = {z.get("name") for z in self.opts.get("zones", []) or []}
        dropped = set()
        for key in ("events", "active", "defrost_since", "window", "window_saved", "defrost_aware", "warmup", "coast"):
            for n in [n for n in self.st.get(key, {}) if n not in names]:
                self.st[key].pop(n, None)
                dropped.add(n)
        if dropped:
            log.info("pruned state of removed zones: %s", ", ".join(sorted(dropped)))
        return sorted(dropped)

    def legacy_warnings(self):
        """MA low (0.3.3): the legacy `radiant_room_cap` option is ignored. Say so in the log when it is set
        lower than what now applies, so nobody thinks a lowered value still caps the floor."""
        v = _num(self.opts.get("radiant_room_cap"))
        if v is None or v >= self.radiant_setpoint_max:
            return []
        return ["legacy option radiant_room_cap=%g is ignored since 0.3.3; the floor setpoint cap is "
                "radiant_setpoint_max=%g and the air shutoff is radiant_air_shutoff=%g"
                % (v, self.radiant_setpoint_max, self.radiant_room_cap)]

    # ---------- events + next change (0.4.0, read-only) ----------
    def _track(self, name, now, kinds):
        """Record start/end of hold / window / vacation / defrost / preheat for the card's event strip."""
        before = set(self.st["active"].get(name, []))
        kinds = {k for k in kinds if k in EVENT_KINDS}
        ev = self.st["events"].setdefault(name, [])
        for k in sorted(kinds - before):
            ev.append([int(now), k, "start"])
        for k in sorted(before - kinds):
            ev.append([int(now), k, "end"])
        del ev[:-EVENTS_MAX]
        self.st["active"][name] = sorted(kinds)

    def events(self, name, since):
        return [e for e in self.st["events"].get(name, []) if e[0] >= since]

    @staticmethod
    def next_change(inp, unit, cfg, result, horizon_min=1440):
        """First future time the schedule changes this zone's target, by re-running core.decide with the
        clock moved forward (weather / presence / indoor held as now). Returns
        {ts, setpoint, mode, profile, preheat, wake_ts} or None. Nothing is written."""
        if result.get("reason") not in ("ok", "no_change"):
            return None  # hold / off / window / missing input: nothing scheduled to show
        mode = result.get("mode")
        if mode in (None, "off"):
            return None
        # the sun is left out of the projection (its offset changes all day); compare against the same
        # projection for "now" so only schedule / profile changes count
        base = dict(inp, last_mode=mode, last_setpoint=None, last_mode_change_ts=0, override_active=False,
                    sun_elev=None, sun_az=None)
        m0 = int(inp.get("minute_of_day", 0))
        now = _num(inp.get("now_ts")) or 0
        r0 = core.decide(base, unit, cfg)
        sp = _num(r0.get("setpoint"))
        if r0.get("action") != "set" or sp is None:
            return None
        base["last_setpoint"] = sp
        c = dict(core.DEFAULTS)
        c.update(cfg or {})
        room = _num(inp.get("indoor_temp"))
        k = math.exp(-NEXT_STEP_MIN / PROJECTION_TAU_MIN)
        for step in range(NEXT_STEP_MIN, horizon_min + 1, NEXT_STEP_MIN):
            mod = (m0 + step) % 1440
            if room is not None:  # room drifts toward the setpoint in force instead of staying where it is now
                room = sp + (room - sp) * k
            r = core.decide(dict(base, minute_of_day=mod, now_ts=now + step * 60,
                                 indoor_temp=room if room is not None else inp.get("indoor_temp")), unit, cfg)
            if r.get("action") == "set" and _num(r.get("setpoint")) is not None and (
                    abs(_num(r["setpoint"]) - sp) >= 0.5 or r.get("mode") != mode):
                pre = (inp.get("manual_profile", "auto") == "auto" and r.get("profile") == "home"
                       and core.in_sleep_window(mod, c))
                out = {"ts": int(now + step * 60), "setpoint": _num(r["setpoint"]), "mode": r.get("mode"),
                       "profile": r.get("profile"), "preheat": bool(pre), "wake_ts": None}
                if pre:
                    out["wake_ts"] = int(now + (step + (core._hm(c["wake"]) - mod) % 1440) * 60)
                return out
        return None

    def zone(self, name):
        return next((z for z in self.opts.get("zones", []) or [] if z["name"] == name), None)

    def unit_state(self, zone):
        """Current HA state + attributes for one zone's climate entity (None if unreadable)."""
        s = next((x for x in self.ha.get_states() if x.get("entity_id") == zone.get("climate")), None)
        return self._get({zone.get("climate"): s} if s else {}, zone.get("climate"))

    HISTORY_RANGES = {"24h": 86400, "7d": 7 * 86400}
    MAX_POINTS = 300

    def history(self, zone, rng):
        """Card graph data from HA history (read-only): indoor, setpoint, heating on/off, outdoor."""
        secs = self.HISTORY_RANGES[rng]
        now = self.clock()
        start = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - secs))
        ents = [e for e in (zone.get("climate"), zone.get("indoor"), self.opts.get("weather_entity")) if e]
        raw = self.ha.get_history(ents, start) or []
        by = {}
        for series in raw:
            if series:
                by[series[0].get("entity_id")] = series

        def ts(row):
            # last_updated moves on attribute changes (setpoint, hvac_action, weather temp);
            # last_changed only on state changes. Clamp the carried-in first row to the window start.
            v = row.get("last_updated") or row.get("last_changed") or ""
            try:
                return max(calendar.timegm(time.strptime(v[:19], "%Y-%m-%dT%H:%M:%S")), now - secs)  # UTC
            except ValueError:
                return None

        indoor, setpt, heat, outdoor, defrost_raw = [], [], [], [], []
        for row in by.get(zone.get("climate"), []):
            t, a = ts(row), row.get("attributes") or {}
            if t is None:
                continue
            st = row.get("state")
            # Off is never heating (some units report a stale hvac_action=heating while off).
            # defrosting / preheating is not heat delivered: it gets its own band, not the heating band.
            act = a.get("hvac_action")
            on = st not in ("off", None) and (act == "heating" if act else st == "heat")
            heat.append([int(t), 1 if on else 0])
            defrost_raw.append([int(t), 1 if st not in ("off", None) and act in DEFROST_ACTIONS else 0])
            setpt.append([int(t), _num(a.get("temperature")) if st not in ("off", None) else None])
            if not zone.get("indoor") and _num(a.get("current_temperature")) is not None:
                indoor.append([int(t), _num(a.get("current_temperature"))])
        for row in by.get(zone.get("indoor"), []) if zone.get("indoor") else []:
            t, v = ts(row), _num(row.get("state"))
            if t is not None and v is not None:
                indoor.append([int(t), v])
        for row in by.get(self.opts.get("weather_entity"), []):
            t, v = ts(row), _num((row.get("attributes") or {}).get("temperature"))
            if t is not None and v is not None:
                outdoor.append([int(t), v])

        def thin(pts):
            pts = sorted(pts, key=lambda p: p[0])  # by time only; values may be None (off)
            if len(pts) <= self.MAX_POINTS:
                return pts
            step = len(pts) / self.MAX_POINTS
            return [pts[int(i * step)] for i in range(self.MAX_POINTS)] + [pts[-1]]

        # Inferred defrosts never show up in recorder hvac_action. Union those event spans with any
        # reported defrosting / preheating so the graph band survives a heating=0 sample in the middle.
        div = _step_intervals(defrost_raw, now) + _event_intervals(
            self.st.get("events", {}).get(zone["name"], []), "defrost", now - secs, now)
        defrost = _intervals_to_steps(_union_intervals(div))
        return {"zone": zone["name"], "range": rng, "start": int(now - secs), "end": int(now),
                "indoor": thin(indoor), "setpoint": thin(setpt), "heating": thin(heat),
                "defrost": defrost, "outdoor": thin(outdoor)}

    # ---------- main loop ----------
    def evaluate(self):
        states = {s["entity_id"]: s for s in self.ha.get_states()}
        now = self.clock()
        lt = self.localtime(now)
        home = self.occupancy(states)
        delay = (_num(self.opts.get("away_delay_min")) or 0) * 60
        if delay > 0:  # 0.6.2 departure debounce (option off = 0 = old behaviour, no state kept)
            pr = core.presence_debounce(self.st.get("presence"), home, now, delay)
            self.st["presence"] = pr
            home = pr["confirmed"] if home is not None else None  # unreadable stays a fail-safe hold
        w, wattr = self._get(states, self.opts.get("weather_entity"))
        outdoor = wattr.get("temperature") if w is not None else None
        cloud = wattr.get("cloud_coverage") if w is not None else None
        fc, _ = self._get(states, self.opts.get("forecast_entity"))
        fsum = None
        if self.opts.get("forecast_anticipation"):
            # 0.6.4: hourly forecast (read-only weather.get_forecasts, cached 30 min). A configured
            # forecast_entity still wins for the blend; the forecast high also feeds the season guard.
            try:
                fsum = self._forecast(now, lt)
                if fc is None and fsum["mean"] is not None:
                    fc = fsum["mean"]
            except Exception as e:  # noqa: BLE001 - MA #46: a learner fault = option off; mode lock still runs
                fsum = None
                self._learner_fault("forecast", e)
        sun, sattr = self._get(states, "sun.sun")
        vac = self.vacation_on(states)
        results = {}
        pending = []
        results_lead = {}
        results_adv = {}
        for z in self.opts.get("zones", []) or []:
            name = z["name"]
            cl, cattr = self._get(states, z.get("climate"))
            ind, _ = self._get(states, z.get("indoor"))
            hum, _ = self._get(states, z.get("humidity"))
            floor, _ = self._get(states, z.get("floor"))
            win, _ = self._get(states, z.get("window"))
            ind_stale = self._indoor_stale(states, z, now)
            if ind_stale:
                ind = None  # frozen room sensor: fall back to the unit's own reading, same as unavailable
            indoor = _num(ind) if _num(ind) is not None else self._unit_temp(cattr)
            setpt = cattr.get("temperature") if cl is not None else None
            if excluded(z.get("climate")):
                results[name] = {"action": "hold", "reason": "excluded_entity", "mode": None,
                                 "setpoint": None, "profile": None}
                continue
            if not z.get("enabled", True):
                results[name] = {"action": "hold", "reason": "zone_disabled", "mode": cl,
                                 "setpoint": setpt, "profile": None}
                continue
            # detect a manual change made in HA since the app's last command -> override
            act = cattr.get("hvac_action") if cl is not None else None
            aware = bool(self.opts.get("defrost_aware"))
            dact, inferred, grace = act, False, False
            if aware:
                self.st["defrost_since"].pop(name, None)
                defrost, stuck, inferred, grace = self._defrost_aware(
                    name, z, act, cl, indoor, outdoor, cattr, now)
                if inferred:
                    dact = "defrosting"
            else:
                defrost, stuck = act in DEFROST_ACTIONS, False
                if defrost:
                    since = self.st["defrost_since"].setdefault(name, now)
                    if now - since > DEFROST_TIMEOUT_S:
                        defrost, stuck = False, True  # treat as normal from here; flag it
                else:
                    self.st["defrost_since"].pop(name, None)
            last = self.st["last_cmd"].get(name)
            # Grace matches an active defrost: the reversing valve and the room drop are not a person.
            if last and cl is not None and not defrost and not grace and not self._override_active(name, now):
                if (cl != last.get("mode") and cl != "off") or (
                        _num(setpt) is not None and _num(last.get("setpoint")) is not None
                        and abs(_num(setpt) - _num(last["setpoint"])) > OWN_WRITE_TOLERANCE_F and cl != "off"):
                    self.start_override(name)
                    self.st["last_cmd"].pop(name, None)
            # the unit rounded the app's own write: decide against what the app wrote, so the setback is not
            # re-sent every cycle and leaving vacation restores the scheduled setpoint from there.
            eff_setpt = setpt
            last = self.st["last_cmd"].get(name)
            if (last and cl == last.get("mode") and _num(setpt) is not None and _num(last.get("setpoint")) is not None
                    and abs(_num(setpt) - _num(last["setpoint"])) <= OWN_WRITE_TOLERANCE_F):
                eff_setpt = last["setpoint"]
            if self.st["override_until"].get(name) and not self._override_active(name, now):
                self.st["override_until"].pop(name, None)  # auto-resume
            # vacation: radiant stays off. The app turns it off if it is on (and may turn it back on after,
            # since it was the app's off); a person's off is left alone. Cards cannot turn it on.
            if vac and z.get("heat_only"):
                self.st["window_saved"].pop(name, None)  # vacation after a window opened: never restore
                r = {"action": "hold", "mode": cl, "setpoint": setpt, "reason": "vacation_radiant_off",
                     "profile": "vacation"}
                applied = False
                if cl not in (None, "off"):
                    r["action"], r["mode"] = "set", "off"
                    if self.writes_allowed:
                        self.ha.call("climate", "set_hvac_mode", {"entity_id": z["climate"], "hvac_mode": "off"})
                        self.st["app_off"] = sorted(set(self.st["app_off"]) | {name})
                        self.st["last_change"][name] = now
                        self.st["last_cmd"][name] = {"mode": "off", "setpoint": setpt}
                        applied = True
                results[name] = dict(r, applied=applied)
                results[name].update(self._live(cl, cattr, indoor, hum))
                if applied:
                    results[name]["live"]["mode"] = "off"
                self._track(name, now, {"vacation"} | ({"hold"} if self._override_active(name, now) else set()))
                continue
            # radiant over-temp shutoff (independent of profile / override)
            if z.get("heat_only") and cl not in (None, "off") and core.radiant_overtemp(
                    indoor, _num(floor), self._cfg(z)):
                r = {"action": "set", "mode": "off", "setpoint": setpt, "reason": "overtemp_shutoff",
                     "profile": None}
                if self.writes_allowed:
                    self.ha.call("climate", "set_hvac_mode", {"entity_id": z["climate"], "hvac_mode": "off"})
                    self.st["app_off"] = [n for n in self.st["app_off"] if n != name]
                results[name] = dict(r, applied=self.writes_allowed)
                results[name].update(self._live(cl, cattr, indoor, hum))
                self._track(name, now, set())
                continue
            # 0.5.0 window delay + restore (contact sensor only). No sensor configured -> nothing changes.
            # window_action off (default) = pre-0.5.0 behaviour: nothing is saved or restored.
            win_open, restore = None, None
            if not z.get("window"):  # MA L4: sensor removed from the options -> drop its window state
                self.st["window"].pop(name, None)
                self.st["window_saved"].pop(name, None)
            else:
                ws = core.window_debounce(self.st["window"].get(name), None if win is None else win == "on", now,
                                          z.get("window_open_delay_s", 0), z.get("window_close_delay_s", 0))
                self.st["window"][name] = {k: ws[k] for k in ("confirmed", "raw", "since")}
                win_open = ws["confirmed"]
                saves = (z.get("window_action") or "off") != "off"
                if ws["event"] == "opened" and cl is not None and saves:
                    self.st["window_saved"][name] = {"mode": cl, "setpoint": _num(eff_setpt), "ts": now}
                elif ws["event"] == "closed":
                    restore = self.st["window_saved"].pop(name, None)
                saved = self.st["window_saved"].get(name)
                if saved and (vac or self._override_active(name, now) or (
                        cl == "off" and name not in self.st["app_off"])):
                    # CoS: vacation started, or someone changed the zone's mode / setpoint, after the window
                    # opened -> the saved state is stale; never restore it.
                    self.st["window_saved"].pop(name, None)
                    log.info("window: discarded saved state for %s (vacation or manual change)", name)
                if restore and (vac or self._override_active(name, now)):
                    restore = None
            inp = {
                "enabled": self.enabled or self.dry_run,
                "override_active": self._override_active(name, now),
                "manual_profile": "away" if vac else self.st.get("profile", "auto"),
                "owner_home": home,
                "minute_of_day": lt.tm_hour * 60 + lt.tm_min,
                "now_ts": now,
                "outdoor_temp": outdoor,
                "forecast_temp": fc,
                "forecast_high": fsum["high"] if fsum else None,
                "cloud_pct": cloud,
                "sun_elev": sattr.get("elevation") if sun is not None else None,
                "sun_az": sattr.get("azimuth") if sun is not None else None,
                "indoor_temp": indoor,
                "humidity": hum,
                "window_open": win_open,
                "window_action": z.get("window_action") or "off",
                "unit_modes": list(cattr.get("hvac_modes") or []),
                # MA L1: the saved state is only a desired setpoint for core.decide (clamps + mode gate apply)
                "restore": restore,
                "unit_available": cl is not None,
                "last_mode": cl,
                "last_setpoint": eff_setpt,
                "last_mode_change_ts": self.st["last_change"].get(name, 0),
                "app_turned_off": name in self.st["app_off"],
                # Active defrost, or the short grace after one ends. Off when the option is off.
                "defrosting": bool(aware and (defrost or grace)),
            }
            cfg = self._cfg(z)
            # Defrost is heating-in-progress for the guards above, but it is not heat delivered and it is
            # not passive drift. Pass a non-heating, non-idle action so neither learner records the drop.
            learn_action = "defrosting" if defrost else act
            if self.opts.get("learned_preheat") and not z.get("heat_only"):
                # 0.6.1: learn this zone's real warm-up / cool-down rate; size the pre-wake lead from it
                try:
                    if not isinstance(self.st.get("warmup"), dict):
                        self.st["warmup"] = {}  # hardening: wrong shape -> reset (like 0.5.1 state sanitizing)
                    wu = self.st["warmup"]
                    wu[name] = core.warmup_step(wu.get(name), cl, learn_action, indoor, setpt, outdoor, now)
                    fam = cl if cl in ("heat", "cool") else "heat"
                    home_t = cfg.get("home", core.DEFAULTS["home"])
                    gap = None if indoor is None else (home_t - indoor if fam == "heat" else indoor - home_t)
                    o_wake = fsum["at_wake"] if fsum and fsum["at_wake"] is not None else outdoor
                    lead = core.learned_lead(wu[name]["samples"][fam], o_wake, gap)
                    cfg["preheat_lead_min"] = lead
                    results_lead[name] = lead
                except Exception as e:  # noqa: BLE001 - MA #46: a learner fault = option off; mode lock still runs
                    cfg.pop("preheat_lead_min", None)
                    results_lead.pop(name, None)
                    self._learner_fault("warmup", e)
            smax = _num(self.opts.get("sleep_coast_max_min")) or 0
            if smax > 0 and not z.get("heat_only"):
                # 0.6.3: learn passive drift; start the sleep setback early enough to arrive by bedtime
                try:
                    if not isinstance(self.st.get("coast"), dict):
                        self.st["coast"] = {}
                    co = self.st["coast"]
                    co[name] = core.coast_step(co.get(name), cl, learn_action, indoor, setpt, outdoor, now)
                    adv = core.sleep_advance(
                        co[name]["samples"], outdoor, indoor, cfg.get("sleep", core.DEFAULTS["sleep"]), smax)
                    cfg["sleep_advance_min"] = adv
                    results_adv[name] = adv
                except Exception as e:  # noqa: BLE001 - MA #46: a learner fault = option off; mode lock still runs
                    cfg.pop("sleep_advance_min", None)
                    results_adv.pop(name, None)
                    self._learner_fault("coast", e)
            if vac:  # vacation setback on the splits: core's away path with the vacation targets
                cfg["away_heat"], cfg["away_cool"] = self.vacation_targets()
            r = core.decide(inp, z, cfg)
            if grace and not defrost and r.get("reason") == "defrosting":
                # Post-defrost grace skips the setpoint chase without leaving a Defrosting badge up.
                r = dict(r, reason="no_change")
            if defrost and r["action"] == "set" and r.get("reason") != "window_open":
                # busy defrosting / pre-heating: no write, no mode-change timestamp, no missed-setpoint retry
                r = {"action": "hold", "mode": cl, "setpoint": setpt, "reason": dact, "profile": r.get("profile")}
            elif defrost and r.get("reason") in ("no_change", "ok"):
                r = dict(r, reason=dact)
            if vac and r.get("profile") == "away":
                r["profile"] = "vacation"
                vr = {"ok": "vacation_setback", "no_change": "vacation_at_setback"}
                r["reason"] = vr.get(r["reason"], r["reason"])
            pending.append((name, z, r, cl, cattr, indoor, hum, setpt, inp, cfg, defrost, dact, stuck, aware,
                            inferred))
        # 0.6.0 shared-mode lock: one outdoor unit -> never heat and cool across the splits in one cycle.
        self._apply_mode_lock(pending, states, now)
        mode_ok = self._write_modes_safely(pending, states) if self.writes_allowed else {}
        for (name, z, r, cl, cattr, indoor, hum, setpt, inp, cfg, defrost, dact, stuck, aware,
             inferred) in pending:
            applied = False
            if r["action"] == "set" and self.writes_allowed and not mode_ok.get(name, True):
                r = dict(r, reason="mode_write_failed")  # nothing more is sent to this zone this cycle
            elif r["action"] == "set" and self.writes_allowed:
                if r["mode"] != cl:
                    self.st["last_change"][name] = now
                    if r["mode"] == "off":
                        self.st["app_off"] = sorted(set(self.st["app_off"]) | {name})
                    else:
                        self.st["app_off"] = [n for n in self.st["app_off"] if n != name]
                sp_ok = True
                if r["mode"] != "off" and r["setpoint"] is not None:
                    sp_ok, snapped = self._write_setpoint(z, states, r["mode"], r["setpoint"])
                    if snapped is not None:
                        r = dict(r, setpoint=snapped)
                if sp_ok:
                    self.st["last_cmd"][name] = {"mode": r["mode"], "setpoint": r["setpoint"]}
                    applied = True
                else:  # 0.8.1: one failed setpoint write no longer aborts the cycle (state still saved)
                    self.st["last_cmd"][name] = {"mode": r["mode"], "setpoint": None}
                    r = dict(r, reason="setpoint_write_failed")
            results[name] = dict(r, applied=applied)
            results[name].update(self._live(cl, cattr, indoor, hum))
            if applied:  # reflect what the app just wrote, not the pre-write read (card showed stale setpoint)
                results[name]["live"]["mode"] = r["mode"]
                if r["mode"] != "off" and r["setpoint"] is not None:
                    results[name]["live"]["setpoint"] = _num(r["setpoint"])
            kinds = set()
            if self._override_active(name, now):
                kinds.add("hold")
            if r.get("reason") == "window_open":
                kinds.add("window")
            if vac:
                kinds.add("vacation")
            if defrost:
                kinds.add("defrost")
            if stuck:
                kinds.add("defrost_stuck")
            if (not vac and inp["manual_profile"] == "auto" and r.get("profile") == "home"
                    and core.in_sleep_window(inp["minute_of_day"], dict(core.DEFAULTS, **cfg))):
                kinds.add("preheat")
                results[name]["preheat"] = True
            self._track(name, now, kinds)
            results[name]["defrost"] = dact if defrost else None
            results[name]["defrost_stuck"] = stuck
            if aware:
                results[name]["defrosting"] = bool(defrost)
                results[name]["defrost_inferred"] = inferred
            results[name]["next"] = None if (vac or defrost) else self.next_change(inp, z, cfg, r)
            if name in results_lead:
                results[name]["preheat_lead_min"] = results_lead[name]
            if name in results_adv:
                results[name]["sleep_advance_min"] = results_adv[name]
            h = self.st["history"].setdefault(name, [])
            h.append([int(now), indoor, _num(outdoor), _num(setpt), r.get("setpoint")])
            del h[:-HISTORY_MAX]
        rt, _ = self._get(states, self.opts.get("radiant_timeout_entity") or RADIANT_TIMEOUT_ENTITY)
        self.st["last_result"] = {"ts": now, "presence_home": home, "vacation": vac, "outdoor": _num(outdoor),
                                  "weather": w, "radiant_timeout": rt, "zones": results}
        self.save()
        return results

    def setpoint_target(self, zone):
        """0.8.6: entity that receives set_temperature (zone `setpoint_climate`, default the zone's `climate`)."""
        eid = zone.get("setpoint_climate") or zone.get("climate")
        return eid if str(eid or "").startswith("climate.") and not excluded(eid) else zone.get("climate")

    def setpoint_data(self, zone, states, mode, temperature):
        """0.8.6: (data incl. entity_id or None, snapped setpoint, reason) built from the target's capabilities."""
        eid = self.setpoint_target(zone)
        st, attrs = self._get(states, eid)
        data, t, why = core.setpoint_payload(attrs, mode or st, temperature)
        return (dict(data, entity_id=eid) if data else None), t, why

    def _write_setpoint(self, zone, states, mode, temperature):
        """Returns (ok, snapped setpoint). Unchanged = ok without a write; unsupported = not ok, no write."""
        data, t, why = self.setpoint_data(zone, states, mode, temperature)
        if data is None:
            if why == "unchanged":
                return True, t
            log.warning("setpoint not written for %s: %s", zone.get("name"), why)
            return False, t
        try:
            self.ha.call("climate", "set_temperature", data)
            return True, t
        except Exception as e:  # noqa: BLE001 - fail safe, the cycle finishes and saves state
            log.warning("setpoint write failed for %s: %s%s", data["entity_id"], type(e).__name__,
                        " %s" % e.code if hasattr(e, "code") else "")
            return False, t

    def _call_setpoint(self, entity_id, temperature):
        """One set_temperature write; False (logged, no raise) when it fails (0.8.1)."""
        try:
            self.ha.call("climate", "set_temperature", {"entity_id": entity_id, "temperature": temperature})
            return True
        except Exception as e:  # noqa: BLE001 - fail safe, the cycle finishes and saves state
            log.warning("setpoint write failed for %s: %s%s", entity_id, type(e).__name__,
                        " %s" % e.code if hasattr(e, "code") else "")
            return False

    def _call_mode(self, entity_id, mode):
        """One set_hvac_mode write; False (logged, no raise) when it fails."""
        try:
            self.ha.call("climate", "set_hvac_mode", {"entity_id": entity_id, "hvac_mode": mode})
            return True
        except Exception as e:  # noqa: BLE001 - fail safe, the caller keeps the pair consistent
            log.warning("mode write failed for %s (%s): %s", entity_id, mode, type(e).__name__)
            return False

    def _write_modes_safely(self, pending, states):
        """MA #43 F2/F3: send this cycle's mode writes so the split pair is never heat+cool, not even between two
        writes, and a failed write never leaves opposites behind.
        1) every split that leaves its direction (flip, auto, or off) is written off first;
        2) then each split's new mode is written, but only if it is consistent with what the other splits are
           running right now (as far as this cycle knows: its own read plus its own successful writes);
        3) a failed write marks the zone failed; later writes re-check against the still-live mode, and a pair
           left conflicting by failures is cleared by turning the writable split(s) off.
        Radiant (heat_only) is not on the outdoor unit and is written directly.
        Returns {zone name: True if its mode is now what the cycle decided}."""
        ok = {}
        live = {}  # entity -> mode as the cycle believes it is now (every split on the outdoor unit)
        for z in self.opts.get("zones", []) or []:
            if not z.get("heat_only"):
                cl, _ = self._get(states, z.get("climate"))
                if cl is not None:
                    live[z["climate"]] = cl
        todo = []
        for p in pending:
            name, z, r, cl = p[0], p[1], p[2], p[3]
            if r["action"] != "set" or r["mode"] == cl:
                continue
            if z.get("heat_only"):
                ok[name] = self._call_mode(z["climate"], r["mode"])
                continue
            todo.append((name, z["climate"], cl, r["mode"]))
        # 1) off first for every split whose direction changes
        for name, eid, cl, mode in todo:
            if core.mode_family(cl) and core.mode_family(cl) != core.mode_family(mode):
                if self._call_mode(eid, "off"):
                    live[eid] = "off"
                elif mode != "off":
                    ok[name] = False
            if mode == "off":
                ok[name] = live[eid] == "off"
        # 2) new modes, each only if consistent with the live pair
        for name, eid, _cl, mode in todo:
            if mode == "off" or name in ok:
                continue
            trial = dict(live, **{eid: mode})
            if core.shared_mode_conflict(trial.values()):
                log.warning("mode lock: %s not set to %s (another split could not be changed)", name, mode)
                ok[name] = False
                continue
            ok[name] = self._call_mode(eid, mode)
            if ok[name]:
                live[eid] = mode
        # 3) failures left a conflict (e.g. a split refused its off): stop every split the app could still write
        if core.shared_mode_conflict(live.values()):
            writable = [(p[0], p[1]["climate"], p[3], "off") for p in pending if not p[1].get("heat_only")]
            for name, eid, _cl, _m in sorted(writable, key=lambda w: w[0] not in ok):  # failed/forced first
                if live.get(eid) != "off" and core.mode_family(live.get(eid)) and self._call_mode(eid, "off"):
                    live[eid] = "off"
                    ok[name] = False
                    if not core.shared_mode_conflict(live.values()):
                        break
        return ok

    def _learner_fault(self, which, err):
        """MA #46: an optional learner failed -> treated as off for this cycle; logged once per learner."""
        seen = self.__dict__.setdefault("_learner_faults", set())
        if which not in seen:
            seen.add(which)
            log.warning("%s learner disabled for this cycle after an error: %s", which, type(err).__name__)

    def _apply_mode_lock(self, pending, states, now):
        """0.6.0 hard rule: the split zones share one outdoor unit, so heat and cool are never requested at the same
        time. Rewrites this cycle's results in place (pure decision in core.resolve_shared_mode). A forced zone is
        written even through a manual hold: the hardware limit outranks the hold."""
        entries, by_name = [], {}
        for p in pending:
            name, z, r, cl = p[0], p[1], p[2], p[3]
            if z.get("heat_only"):
                continue
            by_name[name] = p
            entries.append({"name": name, "live": cl, "held": r.get("action") != "set",
                            "want": r.get("mode") if r.get("action") == "set" else cl,
                            "changed_ts": _ts((states.get(z.get("climate")) or {}).get("last_changed"))})
        # split zones skipped earlier (disabled / excluded) still run on the same outdoor unit: count them as held
        for z in self.opts.get("zones", []) or []:
            if z.get("heat_only") or z["name"] in by_name:
                continue
            cl, _ = self._get(states, z.get("climate"))
            if cl is not None:
                entries.append({"name": z["name"], "live": cl, "held": True, "locked": True, "want": cl,
                                "changed_ts": _ts((states.get(z.get("climate")) or {}).get("last_changed"))})
        group, fixes = core.resolve_shared_mode(entries)
        self.shared_mode = group  # not persisted: recomputed every cycle (keeps the 0.5.x state file shape)
        for name, mode in fixes.items():
            p = by_name.get(name)
            if p is None:
                log.warning("mode lock: %s runs %s against the group (%s) but is not app-controlled", name,
                            next((e["want"] for e in entries if e["name"] == name), None), group)
                continue
            r, setpt = p[2], p[7]
            log.warning("mode lock: %s wanted %s; forced to %s (shared outdoor unit)", name, r.get("mode"), mode)
            r.update({"action": "set", "mode": mode, "reason": "mode_lock",
                      "setpoint": r.get("setpoint") if r.get("action") == "set" else _num(setpt)})

    def _forecast(self, now, lt):
        """Cached hourly forecast summary; {"mean","high","at_wake"} all None when unavailable."""
        c = getattr(self, "_fc_cache", None)
        if not c or now - c[0] > 1800:
            try:
                raw = self.ha.get_forecast(self.opts.get("weather_entity"))
            except Exception as e:  # noqa: BLE001 - forecast is optional; never block a cycle
                log.debug("forecast unavailable: %s", e)
                raw = []
            c = self._fc_cache = (now, raw)
        cfg = dict(core.DEFAULTS, **self._cfg({}))
        mod = lt.tm_hour * 60 + lt.tm_min
        wake_ts = now + ((core._hm(cfg["wake"]) - mod) % 1440) * 60
        return core.forecast_summary(c[1], now, wake_ts)

    def snapshot(self):
        return {"enabled": self.enabled, "dry_run": self.dry_run, "profile": self.st.get("profile"),
                "override_hours": _num(self.opts.get("override_hours")) or 2.0,
                # deprecated alias of radiant_air_shutoff (kept for 0.3.x clients; not used by panel/card)
                "radiant_room_cap": self.radiant_room_cap,
                "radiant_air_shutoff": self.radiant_room_cap, "radiant_setpoint_max": self.radiant_setpoint_max,
                "vacation": bool((self.st.get("last_result") or {}).get("vacation")),
                "vacation_heat": self.vacation_targets()[0], "vacation_cool": self.vacation_targets()[1],
                "radiant_floor_cap": _num(self.opts.get("radiant_floor_cap")) or core.DEFAULTS.get("radiant_floor_cap"),
                "shared_mode": getattr(self, "shared_mode", None),
                "zones": [{"name": z["name"], "label": z.get("label", z["name"]),
                           "heat_only": bool(z.get("heat_only")),
                           "override_until": self.st["override_until"].get(z["name"]),
                           "history": self.st["history"].get(z["name"], []),
                           "events": self.events(z["name"], self.clock() - 86400)}
                          for z in self.opts.get("zones", []) or []],
                "last": self.st.get("last_result", {}),
                # 0.8.0: independent spare bathroom heater (None when the add-on runs without it)
                "heater": self.heater.snapshot() if getattr(self, "heater", None) else None}
