"""Pure planning logic for Tesla Smart Charge. No I/O; everything here is unit-tested."""
import math
from datetime import datetime, timedelta

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
BAD = ("", "unknown", "unavailable", "none", "null")


def num(v):
    """float or None for unknown/unavailable/NaN/inf."""
    if v is None or str(v).strip().lower() in BAD:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def is_on(v):
    return str(v).strip().lower() in ("on", "true", "home", "charging", "starting")


def hhmm(s):
    h, m = str(s).split(":")
    h, m = int(h), int(m)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError("bad time %r" % s)
    return h, m


def parse_days(spec):
    """'mon-fri', 'sat,sun', 'all' -> set of weekday ints."""
    spec = str(spec or "all").strip().lower()
    if spec in ("all", "*", "daily", ""):
        return set(range(7))
    out = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = (DAYS.index(x.strip()[:3]) for x in part.split("-", 1))
            i = a
            while True:
                out.add(i)
                if i == b:
                    break
                i = (i + 1) % 7
        elif part:
            out.add(DAYS.index(part[:3]))
    return out


def in_schedule(now, schedule):
    """True if `now` (aware/naive local datetime) falls in any peak block. Blocks may cross midnight;
    the block's day is the day it starts."""
    for blk in schedule or []:
        days = parse_days(blk.get("days"))
        sh, sm = hhmm(blk["start"])
        eh, em = hhmm(blk["end"])
        start_today = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
        end_today = now.replace(hour=eh, minute=em, second=0, microsecond=0)
        if (sh, sm) == (eh, em):
            continue
        if (sh, sm) < (eh, em):
            if now.weekday() in days and start_today <= now < end_today:
                return True
        else:  # crosses midnight
            if now.weekday() in days and now >= start_today:
                return True
            if (now.weekday() - 1) % 7 in days and now < end_today:
                return True
    return False


def next_departure(now, dep_hhmm):
    h, m = hhmm(dep_hhmm)
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return t if t > now else t + timedelta(days=1)


def offpeak_hours(now, until, schedule, step_min=5):
    """Hours between now and `until` that are NOT in the peak schedule (sampled every step_min)."""
    if until <= now:
        return 0.0
    if not schedule:
        return (until - now).total_seconds() / 3600
    total, t, step = 0.0, now, timedelta(minutes=step_min)
    while t < until:
        nxt = min(t + step, until)
        if not in_schedule(t, schedule):
            total += (nxt - t).total_seconds()
        t = nxt
    return total / 3600


def taper_equiv_kwh(soc, target, battery_kwh, efficiency, taper_soc=None, taper_factor=1.0):
    """(wall kWh, time-equivalent kWh). Above taper_soc the car accepts less than the commanded current, so each
    kWh there takes taper_factor times as long. Planning on the time-equivalent finishes on time; cost uses wall kWh."""
    lo, hi = max(0.0, float(soc)), max(0.0, float(target))
    if hi <= lo:
        return 0.0, 0.0
    wall = (hi - lo) / 100.0 * battery_kwh / efficiency
    if taper_soc is None or taper_factor <= 1.0 or hi <= taper_soc:
        return wall, wall
    above = (hi - max(lo, taper_soc)) / 100.0 * battery_kwh / efficiency
    return wall, wall + above * (taper_factor - 1.0)


def required_amps(soc, target, battery_kwh, efficiency, volts, hours, taper_soc=None, taper_factor=1.0):
    """Lowest whole amps that add (target-soc)% of battery_kwh (wall-side, /efficiency) in `hours`,
    taper-aware above taper_soc. Returns (amps, wall_kwh)."""
    wall, equiv = taper_equiv_kwh(soc, target, battery_kwh, efficiency, taper_soc, taper_factor)
    if wall <= 0:
        return 0, 0.0
    hours = max(hours, 0.25)
    return int(math.ceil(equiv * 1000.0 / (volts * hours))), wall


def hours_at(amps, volts, equiv_kwh):
    """Hours to deliver equiv_kwh at `amps` (0 amps -> None)."""
    if not amps or amps <= 0:
        return None
    return equiv_kwh * 1000.0 / (volts * amps)


def price_at(t, rates, default_price):
    """Price per kWh at local time t from TOU `rates` [{days,start,end,price}], else default_price (None ok)."""
    for r in rates or []:
        if in_schedule(t, [r]):
            return float(r["price"])
    return default_price


def est_cost(start, hours, wall_kwh, rates, default_price, step_min=5):
    """Session cost estimate: wall kWh spread evenly over [start, start+hours], priced per TOU block."""
    if not wall_kwh or hours is None or hours <= 0:
        return 0.0 if not wall_kwh else None
    n = max(1, int(math.ceil(hours * 60 / step_min)))
    total = 0.0
    for i in range(n):
        p = price_at(start + timedelta(minutes=i * step_min), rates, default_price)
        if p is None:
            return None
        total += p * wall_kwh / n
    return round(total, 2)


def next_offpeak(now, schedule, horizon_h=48, step_min=5):
    """First time >= now outside the peak schedule (now if not in peak)."""
    t = now
    end = now + timedelta(hours=horizon_h)
    while t < end:
        if not in_schedule(t, schedule):
            return t
        t = (t + timedelta(minutes=step_min)).replace(second=0, microsecond=0)
    return None


def clamp_voltage(v, fallback):
    v = num(v)
    return v if v is not None and 180 <= v <= 260 else float(fallback)


def plan(s):
    """Decide what to do. `s` is a dict of already-read inputs:
      now, soc, target, plugged, home, peak, enabled, charge_now, pause, charging, amps_now,
      volts, battery_kwh, efficiency, departure, finish_buffer_min, min_amps, max_amps, schedule
    Returns {action: start|stop|hold|idle, amps, reason, clear: [...flags to clear], behind}.
    Fail-safe: never 'start' during peak unless charge_now."""
    out = {"action": "idle", "amps": None, "reason": "", "clear": [], "behind": False}
    if not s.get("enabled", True):
        out["reason"] = "disabled"
        return out
    if s.get("plugged") is False:
        if s.get("charge_now"):
            out["clear"].append("charge_now")
        if s.get("trip"):
            out["clear"].append("trip")
        out["reason"] = "unplugged"
        return out
    if s.get("plugged") is None:
        # Unknown plug state: never start; stop if peak and not charge_now.
        out.update(action="stop" if s.get("peak") and not s.get("charge_now") else "hold",
                   reason="plug_state_unavailable")
        return out
    if s.get("home") is False:
        out["reason"] = "not_home"
        return out
    soc, target = s.get("soc"), s.get("target")
    if soc is None or target is None:
        out.update(action="stop" if s.get("peak") and not s.get("charge_now") else "hold",
                   reason="soc_or_target_unavailable")
        return out
    if s.get("charge_now"):
        if soc >= target:
            out.update(action="stop", reason="charge_now_complete")
            out["clear"].append("charge_now")
            return out
        out.update(action="start", amps=int(s["max_amps"]), reason="charge_now")
        return out
    if s.get("pause"):
        out.update(action="stop", reason="paused_until_next_window")
        return out
    if s.get("peak"):
        out.update(action="stop", reason="peak")
        return out
    if soc >= target:
        out.update(action="stop", reason="target_reached")
        return out
    now, dep = s["now"], s["departure"]
    finish = dep - timedelta(minutes=int(s.get("finish_buffer_min", 0)))
    hours = offpeak_hours(now, finish, s.get("schedule"))
    if hours <= 0:
        out.update(action="start", amps=int(s["max_amps"]), reason="past_finish_time", behind=True)
        return out
    amps, kwh = required_amps(soc, target, s["battery_kwh"], s["efficiency"], s["volts"], hours,
                              s.get("taper_soc"), s.get("taper_factor", 1.0))
    # max_amps always wins over min_amps (M1).
    final = min(max(int(s["min_amps"]), amps), int(s["max_amps"]))
    out.update(action="start", amps=final, behind=amps > int(s["max_amps"]),
               reason="plan %.1f kWh in %.2f h" % (kwh, hours))
    return out


def amps_changed(new, current):
    """Only command on a change of at least 1 A (or when current is unknown)."""
    cur = num(current)
    return cur is None or abs(int(new) - cur) >= 1


def as_local(dt_now, iso_or_hhmm):
    """input_datetime state 'HH:MM:SS' or 'YYYY-MM-DD HH:MM:SS' -> next local departure datetime."""
    v = str(iso_or_hhmm or "").strip()
    if not v or v.lower() in BAD:
        return None
    try:
        if len(v) <= 8:
            return next_departure(dt_now, v[:5])
        d = datetime.fromisoformat(v)
        if d.tzinfo is None and dt_now.tzinfo is not None:
            d = d.replace(tzinfo=dt_now.tzinfo)
        return d if d > dt_now else next_departure(dt_now, d.strftime("%H:%M"))
    except ValueError:
        return None
