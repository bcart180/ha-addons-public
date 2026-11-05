"""Smart HVAC decision core (pure Python, no HA imports).

Ported verbatim from bcart180/ha-automation-tests PR 36 (ea9153a2). Used by engine.py and tests/.
Contract: decide() never raises on bad input; anything missing -> HOLD (keep last setpoint/mode).
"""
import math

PROFILES = ("home", "away", "sleep", "guest")
DEFAULTS = {
    "home": 71.0, "guest": 71.0, "sleep": 68.0,
    "away_heat": 65.0, "away_cool": 78.0,
    "deadband": 2.0,            # +/- F around target before switching heat<->cool
    "season_heat_below": 58.0,  # outdoor tie-break inside deadband
    "season_cool_above": 68.0,
    "min_cycle_s": 900,         # anti-short-cycle: min seconds between mode changes
    "solar_max_offset": 1.5,    # max F shaved off target for full sun on a facing room
    "humid_cool_offset": 0.5,   # extra cooling when humid
    "humid_threshold": 60.0,
    "preheat_lead_min": 45,     # minutes before wake to switch sleep -> home target
    "sleep_start": "22:00", "wake": "07:00",
    "min_temp": 55.0, "max_temp": 84.0,
    "cool_lockout": 75.0,       # season guard (0.4.1): outdoor/forecast high below this -> never pick cool
    "radiant_room_cap": 80.0,   # air shutoff: bath air temp above this -> radiant off (independent of profile)
    "radiant_floor_cap": 85.0,  # floor probe above this -> radiant off (if a floor sensor exists)
}


def _hm(s):
    h, m = str(s).split(":")[:2]
    return int(h) * 60 + int(m)


def _num(x):
    try:
        if x is None or x in ("unknown", "unavailable", ""):
            return None
        v = float(x)
        return None if math.isnan(v) else v
    except (TypeError, ValueError):
        return None


def solar_gain(sun_elev, sun_az, cloud_pct, room_facing_az, room_factor):
    """0..1 solar gain for a room. 0 at night, scaled by facing, sun height and cloud cover."""
    e, a = _num(sun_elev), _num(sun_az)
    if e is None or a is None or e <= 0 or not room_factor:
        return 0.0
    c = _num(cloud_pct)
    clear = 1.0 - (min(max(c, 0.0), 100.0) / 100.0 if c is not None else 0.0)
    facing = math.cos(math.radians(a - float(room_facing_az)))
    if facing <= 0:
        return 0.0
    g = math.sin(math.radians(min(e, 90.0))) ** 0.5 * facing * clear * float(room_factor)
    return round(min(max(g, 0.0), 1.0), 3)


def in_sleep_window(minute_of_day, cfg):
    s, w = _hm(cfg["sleep_start"]), _hm(cfg["wake"])
    if s > w:
        return minute_of_day >= s or minute_of_day < w
    return s <= minute_of_day < w


def pick_profile(manual_profile, owner_home, minute_of_day, cfg):
    """manual_profile: 'auto' or one of PROFILES. Presence = the configured occupant trackers / zone.home only."""
    if manual_profile in PROFILES:
        return manual_profile
    if owner_home is None:
        return None
    if not owner_home:
        return "away"
    adv = int(_num(cfg.get("sleep_advance_min")) or 0)
    if adv > 0 and 0 < (_hm(cfg["sleep_start"]) - minute_of_day) % 1440 <= adv:
        return "sleep"  # 0.6.3: start the sleep setback early so the room has coasted down by bedtime
    if in_sleep_window(minute_of_day, cfg):
        lead = int(cfg["preheat_lead_min"])
        if (_hm(cfg["wake"]) - minute_of_day) % 1440 <= lead:
            return "home"  # pre-heat / pre-cool before wake
        return "sleep"
    return "home"


def pick_mode(indoor, target, outdoor_eff, last_mode, cfg, heat_only=False, outdoor_hi=None):
    if heat_only:
        return "heat"
    # Season guard (0.4.1): in heating weather a room above target (e.g. 71 at a 68 sleep target in October)
    # coasts down in heat mode (unit idles) instead of switching to cool.
    lock = _num(cfg.get("cool_lockout"))
    hi = _num(outdoor_hi) if _num(outdoor_hi) is not None else _num(outdoor_eff)
    if lock is not None and hi is not None and hi < lock:
        return "heat"
    db = cfg["deadband"]
    if indoor <= target - db:
        return "heat"
    if indoor >= target + db:
        return "cool"
    if outdoor_eff is not None:
        if outdoor_eff <= cfg["season_heat_below"]:
            return "heat"
        if outdoor_eff >= cfg["season_cool_above"]:
            return "cool"
    if last_mode in ("heat", "cool"):
        return last_mode
    return "heat" if (outdoor_eff is None or outdoor_eff < 63) else "cool"


def radiant_overtemp(room_temp, floor_temp=None, cfg=None):
    """True if the radiant zone must be shut off: room or floor over its cap. Missing values never trip."""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    r, f = _num(room_temp), _num(floor_temp)
    return (r is not None and r > c["radiant_room_cap"]) or (f is not None and f > c["radiant_floor_cap"])


WINDOW_ACTIONS = ("off", "fan_only", "eco")


def window_debounce(prev, raw_open, now, open_delay_s=0, close_delay_s=0):
    """0.5.0 contact-sensor window debounce (Versatile Thermostat style, sensor mode only, no slope).

    prev: {"confirmed": bool, "raw": bool|None, "since": ts} or None. raw_open: True/False, or None when
    the sensor is unreadable (keeps the last confirmed state). Returns the new dict; "event" is "opened" /
    "closed" on the cycle the confirmed state flips, else None. A change must persist for the delay.
    """
    st = dict(prev or {"confirmed": False, "raw": None, "since": now})
    st["event"] = None
    if raw_open is None:
        return st
    raw_open = bool(raw_open)
    if raw_open != st.get("raw"):
        st["raw"], st["since"] = raw_open, now
    if raw_open != st.get("confirmed"):
        delay = _num(open_delay_s if raw_open else close_delay_s) or 0
        since = _num(st.get("since"))
        if now - (now if since is None else since) >= delay:
            st["confirmed"] = raw_open
            st["event"] = "opened" if raw_open else "closed"
    return st


def window_target(action, last_mode, last_setpoint, unit_modes, cfg):
    """Mode/setpoint to apply while a window is confirmed open. Unknown action -> off. fan_only falls back
    to off when the unit does not list it. eco keeps the mode and uses the away setback for it."""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    if action == "fan_only" and "fan_only" in (unit_modes or ()):
        return "fan_only", last_setpoint
    if action == "eco" and last_mode in ("heat", "cool"):
        return last_mode, c["away_heat"] if last_mode == "heat" else c["away_cool"]
    return "off", last_setpoint


def decide(inp, unit, cfg=None):
    """Return dict(action='set'|'hold', mode, setpoint, reason, profile).

    inp keys: enabled, override_active, manual_profile, owner_home, minute_of_day, now_ts,
      outdoor_temp, forecast_temp (next-hours mean, optional), cloud_pct, sun_elev, sun_az,
      indoor_temp, humidity, window_open (None if no sensor), unit_available,
      last_mode, last_setpoint, last_mode_change_ts,
      app_turned_off (True only if the app itself last commanded this unit off).
    Rule (MA M1): the app never turns a unit on from an off it did not command, so a person's
    off at the wall, IR remote, vendor app or voice assistant is respected.
    unit keys: facing_az, solar_factor, heat_only.
    """
    c = dict(DEFAULTS)
    c.update(cfg or {})
    hold = lambda why, prof=None: {"action": "hold", "mode": inp.get("last_mode"),
                                   "setpoint": inp.get("last_setpoint"), "reason": why, "profile": prof}
    try:
        if not inp.get("enabled"):
            return hold("disabled")
        if inp.get("override_active"):
            return hold("manual_override")
        if inp.get("unit_available") is False:
            return hold("missing:unit")
        if inp.get("last_mode") == "off" and not inp.get("app_turned_off"):
            return hold("unit_off")
        indoor, outdoor = _num(inp.get("indoor_temp")), _num(inp.get("outdoor_temp"))
        if indoor is None:
            return hold("missing:indoor_temp")
        if outdoor is None:
            return hold("missing:outdoor_temp")
        prof = pick_profile(inp.get("manual_profile", "auto"), inp.get("owner_home"),
                            int(inp.get("minute_of_day", 0)), c)
        if prof is None:
            return hold("missing:presence")
        if inp.get("window_open") is True:
            mode, sp = window_target(inp.get("window_action", "off"), inp.get("last_mode"),
                                     inp.get("last_setpoint"), inp.get("unit_modes"), c)
            if mode == inp.get("last_mode") and (mode == "off" or _num(sp) == _num(inp.get("last_setpoint"))):
                return hold("window_open", prof)
            return {"action": "set", "mode": mode, "setpoint": sp, "reason": "window_open", "profile": prof}
        fc = _num(inp.get("forecast_temp"))
        outdoor_eff = outdoor if fc is None else (outdoor + fc) / 2.0
        outdoor_hi = outdoor if fc is None else max(outdoor, fc)
        if _num(inp.get("forecast_high")) is not None:  # 0.6.4: next-12 h forecast high (option on only)
            outdoor_hi = max(outdoor_hi, _num(inp["forecast_high"]))
        heat_only = bool(unit.get("heat_only"))
        if prof == "away":
            # pick season first using comfort target, then setback
            mode = pick_mode(indoor, c["home"], outdoor_eff, inp.get("last_mode"), c, heat_only, outdoor_hi)
            target = c["away_heat"] if mode == "heat" else c["away_cool"]
        else:
            target = c[prof]
            mode = pick_mode(indoor, target, outdoor_eff, inp.get("last_mode"), c, heat_only, outdoor_hi)
            gain = solar_gain(inp.get("sun_elev"), inp.get("sun_az"), inp.get("cloud_pct"),
                              unit.get("facing_az", 180), unit.get("solar_factor", 0))
            target -= gain * c["solar_max_offset"]
            hum = _num(inp.get("humidity"))
            if mode == "cool" and hum is not None and hum >= c["humid_threshold"]:
                target -= c["humid_cool_offset"]
        restored = False
        rs = inp.get("restore") or None
        if rs and prof != "away" and rs.get("mode") == mode and _num(rs.get("setpoint")) is not None:
            # 0.5.0 window restore (MA L1): the saved setpoint becomes the desired target only when the saved
            # mode is the mode decide picks now; otherwise it is dropped. Clamped below like any target.
            target, restored = _num(rs["setpoint"]), True
        if heat_only and _num(c.get("radiant_setpoint_max")) is not None:
            target = min(target, _num(c["radiant_setpoint_max"]))
        target = round(min(max(target, c["min_temp"]), c["max_temp"]) * 2) / 2.0
        last_mode = inp.get("last_mode")
        last_sp = _num(inp.get("last_setpoint"))
        # 0.8.9: the engine sets defrosting while a heat pump is defrosting and for ~10 min after.
        # That is heating-in-progress: do not hold for a short cycle, and do not chase the setpoint
        # because the room dropped. A reversing-valve mode correction still falls through.
        thawing = bool(inp.get("defrosting"))
        if mode != last_mode and last_mode in ("heat", "cool") and mode in ("heat", "cool"):
            since = _num(inp.get("now_ts")) - (_num(inp.get("last_mode_change_ts")) or 0)
            if since < c["min_cycle_s"] and not thawing:
                return hold("short_cycle_guard", prof)
        if thawing and mode == last_mode and last_sp is not None and abs(last_sp - target) >= 0.5:
            return hold("defrosting", prof)
        if mode == last_mode and last_sp is not None and abs(last_sp - target) < 0.5:
            return hold("no_change", prof)
        return {"action": "set", "mode": mode, "setpoint": target, "reason": "window_restore" if restored else "ok",
                "profile": prof}
    except Exception as e:  # fail safe
        return hold("error:%s" % type(e).__name__)


# 0.6.0 shared-mode lock (hard rule, owner 31 Oct): both mini splits run off ONE outdoor unit, which cannot heat
# one head while it cools the other. Every split zone (heat_only false) is in one group; at most one of heat / cool
# may be requested across the group at any time. dry is a cooling cycle, so it counts as cool. off is always
# allowed (the head simply stops). fan_only is passed through (no refrigerant mode) but flagged in DOCS as not
# vendor-confirmed for this outdoor unit.
MODE_FAMILY = {"heat": "heat", "cool": "cool", "dry": "cool", "heat_cool": "auto", "auto": "auto"}
DIRECTIONS = ("heat", "cool")


def mode_family(mode):
    """heat / cool for the refrigerant direction a mode needs; "auto" for auto / heat_cool (the unit may pick
    either direction on its own, so it can never sit next to another running split); None for off, fan_only,
    unknown."""
    return MODE_FAMILY.get(mode)


def shared_mode_conflict(modes):
    """True when the given split modes could ask the outdoor unit for both heat and cool: two different directions,
    or an auto / heat_cool split next to any other running split (MA #43 F1)."""
    fams = [f for f in (mode_family(m) for m in modes) if f]
    return len(set(fams)) > 1 or ("auto" in fams and len(fams) > 1)


def resolve_shared_mode(entries):
    """Pick the one refrigerant direction for the split group and the per-zone corrections.

    entries: list of dicts {name, want, held, live, changed_ts}
      want  = the mode the zone will end up in this cycle (decision mode if it writes, else its live mode)
      held  = True when the zone is not being driven by the app this cycle (manual hold, person's off, ...)
      changed_ts = when the zone's live mode last changed (newest person action wins a held-vs-held conflict)
      locked = True for a split the app cannot write (zone disabled / entity excluded): it decides first
    Returns (group, fixes): group = "heat" | "cool" | None ("auto" only for a lone auto split); fixes = {name:
    forced_mode} for zones whose want is the opposite direction, or auto / heat_cool beside another running split
    (auto is never a group direction: it is forced to the group's heat / cool). off / fan_only are never touched.
    Precedence: 1) no conflict -> nothing to fix. 2) held zones agree -> app-driven zones follow them (a person's
    choice wins over the app). 3) held zones disagree (e.g. Siri on the raw entity) -> the newest change wins.
    4) only app-driven zones disagree -> the direction already running live, else heat (season guard default).
    """
    fams = {e["name"]: mode_family(e.get("want")) for e in entries}
    if not shared_mode_conflict([e.get("want") for e in entries]):
        g = next((f for f in fams.values() if f), None)
        return g, {}
    dirs = {n: f for n, f in fams.items() if f in DIRECTIONS}
    locked = {dirs[e["name"]] for e in entries if e.get("locked") and e["name"] in dirs}
    held = [e for e in entries if e.get("held") and e["name"] in dirs]
    held_fams = {dirs[e["name"]] for e in held}
    if len(locked) == 1:
        group = locked.pop()  # a split the app cannot write (disabled / excluded) decides the direction
    elif len(held_fams) == 1:
        group = held_fams.pop()
    elif held:
        group = dirs[max(held, key=lambda e: _num(e.get("changed_ts")) or 0)["name"]]
    else:
        wanted = set(dirs.values())
        live = {mode_family(e.get("live")) for e in entries} & set(DIRECTIONS)
        if len(wanted) == 1:
            group = wanted.pop()  # only auto splits disagree with it
        else:
            group = live.pop() if len(live) == 1 else "heat"
    fixes = {e["name"]: group for e in entries if fams[e["name"]] and fams[e["name"]] != group}
    return group, fixes


# 0.6.1 learned warm-up (option learned_preheat, off by default). Each zone learns how fast it actually heats
# (or cools) from its own completed recovery runs, as °F per hour against the outdoor temp at the start, and the
# pre-heat lead before wake is sized from that instead of the fixed preheat_lead_min. Evidence (31 Oct, 53°F out):
# the fixed 45 min lead started 5:45 AM and the first room reached 71 only around 8:30 AM.
LEAD_MIN, LEAD_MAX, LEAD_FALLBACK = 15, 180, 45
RATE_MIN, RATE_MAX = 0.3, 8.0
MIN_SAMPLES = 3


def learned_rate(samples, outdoor):
    """samples: [[outdoor_f, rate_f_per_h], ...]. Linear fit on outdoor when it varies, else the median.
    Returns °F/h clamped to RATE_MIN..RATE_MAX, or None with fewer than MIN_SAMPLES usable samples."""
    pts = [(float(o), float(r)) for o, r in (samples or []) if _num(o) is not None and _num(r) is not None
           and _num(r) > 0]
    if len(pts) < MIN_SAMPLES:
        return None
    rates = sorted(r for _, r in pts)
    med = rates[len(rates) // 2]
    xs = [o for o, _ in pts]
    mx = sum(xs) / len(xs)
    var = sum((x - mx) ** 2 for x in xs)
    o = _num(outdoor)
    if o is None or var < 2.0 * len(xs):
        est = med
    else:
        my = sum(r for _, r in pts) / len(pts)
        b = sum((x - mx) * (r - my) for x, r in pts) / var
        # never extrapolate far outside what was seen
        o = min(max(o, min(xs) - 5), max(xs) + 5)
        est = my + b * (o - mx)
    return min(max(est, RATE_MIN), RATE_MAX)


def learned_lead(samples, outdoor, gap_f):
    """Minutes before wake to start recovery: gap / rate, +10% and 5 min margin, clamped 15..180.
    Falls back to 45 (the 0.5.x fixed lead) until the zone has enough samples."""
    rate = learned_rate(samples, outdoor)
    if rate is None:
        return LEAD_FALLBACK
    gap = _num(gap_f)
    if gap is None or gap <= 0:
        return LEAD_MIN
    return int(round(min(max(gap / rate * 60 * 1.1 + 5, LEAD_MIN), LEAD_MAX)))


def warmup_step(rec, mode, action, indoor, setpoint, outdoor, now, min_rise=1.0, min_minutes=15):
    """Track one recovery run per zone. rec: {"ep": {...} | None, "samples": {"heat": [], "cool": []}}.
    A run starts when the unit is actively heating (cooling) toward a setpoint at least 1°F away and ends when the
    room is within 0.5°F of it; a run is dropped if the mode / setpoint changes or the unit stops. Returns rec."""
    rec = rec if isinstance(rec, dict) else {}
    s = rec.get("samples") if isinstance(rec.get("samples"), dict) else {}
    rec["samples"] = {m: [x for x in (s.get(m) or []) if isinstance(x, list) and len(x) == 2][-20:]
                      for m in ("heat", "cool")}
    ep = rec.get("ep") if isinstance(rec.get("ep"), dict) else None
    ind, sp = _num(indoor), _num(setpoint)
    fam = mode if mode in ("heat", "cool") else None
    gap = None if (fam is None or ind is None or sp is None) else (sp - ind if fam == "heat" else ind - sp)
    if ep and (ep.get("mode") != fam or _num(ep.get("sp")) != sp
               or _num(ep.get("ts")) is None or _num(ep.get("t0")) is None):
        ep = None  # MA #46: a corrupt run record is dropped, never a KeyError
    if ep and gap is not None and gap <= 0.5:
        mins = (now - ep["ts"]) / 60.0
        rise = abs(ind - ep["t0"])
        if mins >= min_minutes and rise >= min_rise:
            rate = min(max(rise / (mins / 60.0), RATE_MIN), RATE_MAX)
            o = _num(ep.get("out"))
            if o is not None:
                rec["samples"][fam] = (rec["samples"][fam] + [[round(o, 1), round(rate, 2)]])[-20:]
        ep = None
    if ep is None and gap is not None and gap >= 1.0 and action in ("heating", "cooling"):
        ep = {"mode": fam, "sp": sp, "t0": ind, "ts": now, "out": _num(outdoor)}
    rec["ep"] = ep
    return rec


def presence_debounce(prev, raw, now, away_delay_s=0):
    """0.6.2: arrivals count at once; a departure counts only after every known occupant has been away for
    away_delay_s without a break (a phone dropping off Wi-Fi for a few minutes is not "away").
    prev: {"confirmed": bool | None, "away_since": ts | None} or None. raw: True / False / None (unknown).
    Returns the new record; record["confirmed"] is what the engine uses. None stays None (fail-safe hold)."""
    p = prev if isinstance(prev, dict) else {}
    conf = p.get("confirmed") if p.get("confirmed") in (True, False) else None
    since = _num(p.get("away_since"))
    if raw is None:
        return {"confirmed": conf, "away_since": since}  # nobody readable: keep the last answer
    if raw is True or away_delay_s <= 0 or conf is None:
        return {"confirmed": bool(raw), "away_since": None if raw else (since or now)}
    if conf is False:
        return {"confirmed": False, "away_since": since or now}
    since = since or now
    return {"confirmed": now - since < away_delay_s, "away_since": since}


# 0.6.3 sleep coast (option sleep_coast_max_min, 0 = off). The house loses heat slowly (31 Oct: 73.9 -> 70.8 in
# 7 h at 53-58°F outside, ~0.4°F/h), so a 68 sleep target set at 10 PM is barely reached before morning. The
# app learns each split's passive drift rate (unit idle, room off target) and lowers to the sleep target early
# enough to arrive by bedtime, capped by the option. It never runs the unit against the drift: the shared-mode
# lock and the season guard still pick the mode.
COAST_MIN_RATE, COAST_MAX_RATE = 0.1, 6.0


def coast_step(rec, mode, action, indoor, setpoint, outdoor, now, min_change=1.0, min_minutes=30):
    """Learn passive drift: unit in heat (cool) but idle with the room above (below) the setpoint.
    rec: {"ep": {...} | None, "samples": [[outdoor, rate], ...]} (last 20). Returns rec."""
    rec = rec if isinstance(rec, dict) else {}
    rec["samples"] = [x for x in (rec.get("samples") if isinstance(rec.get("samples"), list) else [])
                      if isinstance(x, list) and len(x) == 2][-20:]
    ep = rec.get("ep") if isinstance(rec.get("ep"), dict) else None
    ind, sp = _num(indoor), _num(setpoint)
    drifting = (mode in ("heat", "cool") and action in ("idle", None) and ind is not None and sp is not None
                and ((ind - sp) if mode == "heat" else (sp - ind)) >= 0.5)
    if ep and (_num(ep.get("ts")) is None or _num(ep.get("t0")) is None):
        ep = None  # MA #46: corrupt run record -> drop it
    if ep and (not drifting or ep.get("mode") != mode):
        mins = ((_num(ep.get("ts1")) or ep["ts"]) - ep["ts"]) / 60.0  # up to the last drifting reading
        change = abs(ep["t0"] - (ep.get("t1") if ep.get("t1") is not None else ep["t0"]))
        if mins >= min_minutes and change >= min_change and _num(ep.get("out")) is not None:
            rate = min(max(change / (mins / 60.0), COAST_MIN_RATE), COAST_MAX_RATE)
            rec["samples"] = (rec["samples"] + [[round(ep["out"], 1), round(rate, 2)]])[-20:]
        ep = None
    if drifting:
        ep = ep or {"mode": mode, "t0": ind, "ts": now, "out": _num(outdoor)}
        ep["t1"], ep["ts1"] = ind, now
    rec["ep"] = ep
    return rec


def sleep_advance(samples, outdoor, indoor, sleep_target, max_min):
    """Minutes before sleep_start to switch to the sleep target: (room - target) / coast rate, 0..max_min.
    Needs MIN_SAMPLES drift runs; the rate fit reuses learned_rate with the coast clamps."""
    mx = int(_num(max_min) or 0)
    ind, t = _num(indoor), _num(sleep_target)
    if mx <= 0 or ind is None or t is None or ind - t <= 0:
        return 0
    pts = [x for x in (samples or []) if isinstance(x, list) and len(x) == 2]
    if len(pts) < MIN_SAMPLES:
        return 0
    rate = learned_rate(pts, outdoor)
    if rate is None:  # MA #46: corrupt / unusable samples -> no advance (option-off behaviour)
        return 0
    rate = min(max(rate, COAST_MIN_RATE), COAST_MAX_RATE)
    return int(round(min((ind - t) / rate * 60, mx)))


def forecast_summary(fc, now, wake_ts=None, hours=6):
    """0.6.4: hourly forecast [{datetime, temperature}, ...] -> {"mean": next `hours` h mean, "high": next 12 h
    max, "at_wake": temp of the hour nearest wake_ts (within 24 h)} with None where unknown."""
    from datetime import datetime
    pts = []
    for f in fc or []:
        if not isinstance(f, dict):
            continue
        t = _num(f.get("temperature"))
        try:
            ts = datetime.fromisoformat(str(f.get("datetime")).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            continue
        if t is not None and now - 3600 <= ts <= now + 86400:
            pts.append((ts, t))
    near = [t for ts, t in pts if ts <= now + hours * 3600]
    hi = [t for ts, t in pts if ts <= now + 12 * 3600]
    at = None
    if wake_ts is not None and pts:
        ts, t = min(pts, key=lambda p: abs(p[0] - wake_ts))
        at = t if abs(ts - wake_ts) <= 5400 else None
    return {"mean": round(sum(near) / len(near), 1) if near else None,
            "high": max(hi) if hi else None, "at_wake": at}


# ---- 0.8.6: set_temperature payload from the entity's own capabilities ----
FEAT_TARGET_TEMPERATURE = 1        # ClimateEntityFeature.TARGET_TEMPERATURE
FEAT_TARGET_TEMPERATURE_RANGE = 2  # ClimateEntityFeature.TARGET_TEMPERATURE_RANGE


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def snap_setpoint(t, attrs):
    """Clamp t to the entity's min_temp/max_temp and round it to target_temp_step (half up).
    Missing attributes are skipped (no clamp / no rounding)."""
    attrs = attrs or {}
    lo, hi, step = _f(attrs.get("min_temp")), _f(attrs.get("max_temp")), _f(attrs.get("target_temp_step"))
    if step is not None and step > 0:
        t = math.floor(t / step + 0.5) * step
        t = round(t, 4)
    if lo is not None and t < lo:
        t = lo
    if hi is not None and t > hi:
        t = hi
    return float(t)


def setpoint_payload(attrs, mode, t):
    """Build the climate.set_temperature data (without entity_id) for one setpoint t in the HA display unit.
    Returns (data or None, snapped t, reason). data None = do not write (reason says why).
      - TARGET_TEMPERATURE (bit 1): {"temperature": t}
      - only TARGET_TEMPERATURE_RANGE (bit 2): heat -> target_temp_low=t, cool/dry -> target_temp_high=t;
        the other bound keeps its current value, or (unknown / invalid) sits one step inside min/max, because
        an edge value can fail the device's native range after unit conversion (e.g. 88 F -> 31.11 C > 31.0 C).
      - neither bit: no write. No supported_features attribute at all = legacy single setpoint.
    Unchanged value (already the entity's current setpoint) = no write (reason "unchanged")."""
    attrs = attrs or {}
    t = snap_setpoint(float(t), attrs)
    raw = attrs.get("supported_features")
    feats = FEAT_TARGET_TEMPERATURE if raw is None else int(_f(raw) or 0)
    if feats & FEAT_TARGET_TEMPERATURE:
        if _f(attrs.get("temperature")) == t:
            return None, t, "unchanged"
        return {"temperature": t}, t, "ok"
    if feats & FEAT_TARGET_TEMPERATURE_RANGE:
        lo_b, hi_b = _f(attrs.get("min_temp")), _f(attrs.get("max_temp"))
        step = _f(attrs.get("target_temp_step")) or 1.0
        cur_lo, cur_hi = _f(attrs.get("target_temp_low")), _f(attrs.get("target_temp_high"))
        if mode in ("cool", "dry"):
            if cur_hi == t:
                return None, t, "unchanged"
            low = cur_lo if cur_lo is not None and cur_lo <= t else None
            if low is None:
                low = min(t, (lo_b + step) if lo_b is not None else t)
            return {"target_temp_low": low, "target_temp_high": t}, t, "ok"
        if mode == "heat":
            if cur_lo == t:
                return None, t, "unchanged"
            high = cur_hi if cur_hi is not None and cur_hi >= t else None
            if high is None:
                high = max(t, (hi_b - step) if hi_b is not None else t)
            return {"target_temp_low": t, "target_temp_high": high}, t, "ok"
        return None, t, "range_mode_unsupported"
    return None, t, "no_setpoint_feature"
