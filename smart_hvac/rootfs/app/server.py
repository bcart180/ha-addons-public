"""Smart HVAC add-on: 5-minute decide loop + ingress GUI."""
import asyncio
import json
import logging
import os
import re

from aiohttp import web

import core
from engine import Engine
from heater import SpareHeater
from ha_client import HAClient

OPTIONS = os.environ.get("SH_OPTIONS", "/data/options.json")
STATE = os.environ.get("SH_STATE", "/data/state.json")
HEATER_STATE = os.environ.get("SH_HEATER_STATE", "/data/heater.json")
HEATER_TICK_S = 30
STATIC = os.path.join(os.path.dirname(__file__), "static")
INGRESS_IP = "172.30.32.2"
log = logging.getLogger("smart_hvac")


def load_options(path=OPTIONS):
    with open(path) as f:
        opts = json.load(f)
    # Shipped default is on. A saved options file from before 0.8.9 omits the key; fill it in.
    # An explicit false stays off. The engine still treats a missing key as off (0.5.0 golden).
    if isinstance(opts, dict):
        opts.setdefault("defrost_aware", True)
    return opts


@web.middleware
async def ingress_only(request, handler):
    allow = os.environ.get("SH_ALLOW") or INGRESS_IP  # empty never disables the check
    if request.method == "GET" and request.path == "/api/health" and request.remote == "127.0.0.1":
        return await handler(request)  # Docker HEALTHCHECK only (MA M1)
    if request.remote not in allow.split(","):
        return web.Response(status=403, text="ingress only")
    return await handler(request)


# card preset -> engine profile (0.4.0)
PRESETS = {"home": "home", "sleep": "sleep", "away": "away", "eco": "auto"}


def make_app(engine, interval=300, heater=None, heater_tick=HEATER_TICK_S):
    app = web.Application(middlewares=[ingress_only])
    if heater is not None:
        engine.heater = heater
    lock = asyncio.Lock()

    async def run_once():
        async with lock:
            try:
                return await asyncio.get_running_loop().run_in_executor(None, engine.evaluate)
            except Exception as e:  # fail safe: log, change nothing
                log.warning("evaluate failed: %s", type(e).__name__)
                return None

    async def loop(_app):
        async def _t():
            while True:
                await run_once()
                await asyncio.sleep(interval)
        task = asyncio.create_task(_t())
        yield
        task.cancel()

    async def index(_r):
        return web.FileResponse(os.path.join(STATIC, "index.html"))

    async def api_state(_r):
        return web.json_response(engine.snapshot())

    async def api_health(_r):
        return web.json_response({"ok": True})

    async def read_json(r):
        """MA L1/L2: JSON content-type only; body must be an object. Returns dict or None."""
        if r.content_type != "application/json":
            return None
        try:
            body = await r.json()
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    async def api_profile(r):
        body = await read_json(r)
        if body is None:
            return web.json_response({"error": "bad request"}, status=400)
        try:
            engine.set_profile(body.get("profile"))
        except ValueError:
            return web.json_response({"error": "bad profile"}, status=400)
        await run_once()
        return web.json_response(engine.snapshot())

    async def api_preset(r):
        """0.4.0 one-tap presets for the card. Same as /api/profile (global profile, no climate write here;
        the next cycle runs now and applies it through the usual gates). Eco = automatic (presence +
        schedule: away setback when nobody is home, sleep at night)."""
        body = await read_json(r)
        if body is None:
            return web.json_response({"error": "bad request"}, status=400)
        prof = PRESETS.get(str(body.get("preset", "")).lower())
        if not prof:
            return web.json_response({"error": "bad preset"}, status=400)
        engine.set_profile(prof)
        log.info("preset %s -> profile %s", body.get("preset"), prof)
        await run_once()
        return web.json_response(engine.snapshot())

    async def zone_write(r):
        """Common gate for every card write. Returns (zone, body) or a web.Response error."""
        zone = engine.zone(r.match_info["zone"])
        if not zone:
            return web.json_response({"error": "no zone"}, status=404)
        if not engine.writes_allowed:
            return web.json_response({"error": "app is off or in dry run"}, status=409)
        body = await read_json(r)
        if body is None:
            return web.json_response({"error": "bad request"}, status=400)
        return zone, body

    async def call(service, data):
        await asyncio.get_running_loop().run_in_executor(None, engine.ha.call, "climate", service, data)

    async def unit(zone):
        return await asyncio.get_running_loop().run_in_executor(None, engine.unit_state, zone)

    async def vacation():
        return await asyncio.get_running_loop().run_in_executor(None, engine.vacation_on)

    def parse_hours(body, required=False):
        """Explicit hold length. Omitted means the add-on's override_hours (2 h) for old clients."""
        if "hours" not in body or body.get("hours") is None:
            if required:
                return None, web.json_response({"error": "hours required"}, status=400)
            return None, None
        h = body.get("hours")
        if isinstance(h, bool) or not isinstance(h, (int, float)) or not 0.5 <= h <= 12:
            return None, web.json_response({"error": "bad hours"}, status=400)
        return float(h), None

    def done(zone, hours=None, **wrote):
        # Every card change is a manual hold the app respects until it expires or "Resume auto".
        # hours None keeps the 2 h default. The reply reflects the write right away (0.3.2).
        # A temperature hold remembers the number it used. A mode-only write does not invent one.
        if "setpoint" in wrote and wrote.get("setpoint") is not None:
            engine.remember_override_temp(zone, wrote["setpoint"])
        engine.start_override(zone["name"], hours)
        if wrote:
            engine.note_card_write(zone["name"], **wrote)
        return web.json_response(engine.snapshot())

    async def api_setpoint(r):
        g = await zone_write(r)
        if isinstance(g, web.Response):
            return g
        zone, body = g
        try:
            t = float(body.get("temperature"))
        except (TypeError, ValueError):
            return web.json_response({"error": "bad temperature"}, status=400)
        hi = engine.radiant_setpoint_max if zone.get("heat_only") else 84  # clamped at 85 (MA L2)
        if not 55 <= t <= hi:
            return web.json_response({"error": "out of range"}, status=400)
        async with lock:
            states = await asyncio.get_running_loop().run_in_executor(
                None, lambda: {x.get("entity_id"): x for x in engine.ha.get_states() or []})
            cl, _ = engine._get(states, zone.get("climate"))
            data, t, why = engine.setpoint_data(zone, states, cl, t)  # 0.8.6: capability-aware payload
            if data is None and why != "unchanged":
                return web.json_response({"error": "setpoint not supported: %s" % why}, status=409)
            if data is not None:
                await call("set_temperature", data)
        hours, err = parse_hours(body)
        if err:
            return err
        done(zone, setpoint=t, hours=hours)
        await run_once()  # mode follows the override temperature on this request, not the next interval
        return web.json_response(engine.snapshot())

    async def try_mode(zone, mode):
        """set_hvac_mode on one zone; False (logged) when the write fails."""
        try:
            await call("set_hvac_mode", {"entity_id": zone["climate"], "hvac_mode": mode})
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("mode write failed for %s (%s): %s", zone["name"], mode, type(e).__name__)
            return False

    async def switch_group(zone, mode, others, hours=None):
        """MA #43 F2/F3: change zone to mode with the other running splits following, never passing heat+cool.
        others: [(zone, live mode)] that conflict with mode. Order: others off -> zone -> others to mode.
        A failed off / primary write rolls the others back to their previous mode (consistent with the primary's
        unchanged mode). Returns None on success, else an error string (nothing opposite is left live)."""
        turned_off = []
        for other, ost in others:
            if not await try_mode(other, "off"):
                break
            turned_off.append((other, ost))
        else:
            if await try_mode(zone, mode):
                for other, _ost in turned_off:
                    if await try_mode(other, mode):
                        done(other, mode=mode, hours=hours)
                    else:
                        done(other, mode="off", hours=hours)  # left off: safe; its next "on" joins the group
                return None
        for other, ost in turned_off:  # rollback: the primary never changed, so the old modes are consistent
            if not await try_mode(other, ost):
                done(other, mode="off", hours=hours)
        return "mode write failed; nothing changed"

    async def api_mode(r):
        g = await zone_write(r)
        if isinstance(g, web.Response):
            return g
        zone, body = g
        mode = body.get("hvac_mode")
        allowed = ("heat", "off") if zone.get("heat_only") else ("heat", "cool", "off")
        async with lock:  # MA #43 F3: card writes are serialized with the engine cycle
            _st, attr = await unit(zone)
            supported = attr.get("hvac_modes") or allowed
            if mode not in allowed or mode not in supported:
                return web.json_response({"error": "bad mode"}, status=400)
            if mode != "off" and zone.get("heat_only") and await vacation():
                return web.json_response({"error": "vacation: radiant stays off"}, status=409)
            others = []
            if not zone.get("heat_only") and core.mode_family(mode):
                # 0.6.0 shared-mode lock: a heat / cool change on one split applies to every split that is running
                # (one outdoor unit). Splits that are off stay off; their next "on" uses this mode.
                for other in split_zones(zone):
                    ost, _ = await unit(other)
                    if ost is None or core.mode_family(ost) is None:
                        engine.st.setdefault("last_on_mode", {})[other["name"]] = mode
                        continue
                    if core.mode_family(ost) != core.mode_family(mode):  # opposite, or auto / heat_cool
                        others.append((other, ost))
            hours, err_h = parse_hours(body)
            if err_h:
                return err_h
            err = await switch_group(zone, mode, others, hours)
            if err:
                return web.json_response(dict(engine.snapshot(), error=err), status=502)
            return done(zone, mode=mode, hours=hours)

    def split_zones(zone):
        """Every other enabled split zone (heat_only false) on the shared outdoor unit."""
        return [z for z in engine.opts.get("zones", []) or []
                if not z.get("heat_only") and z.get("enabled", True) and z["name"] != zone["name"]]

    async def running_family(zone):
        """heat / cool the other splits are running now, or None (auto / heat_cool is not a direction)."""
        fams = set()
        for other in split_zones(zone):
            ost, _ = await unit(other)
            fams.add(core.mode_family(ost))
        fams &= {"heat", "cool"}
        return fams.pop() if len(fams) == 1 else None

    async def api_power(r):
        g = await zone_write(r)
        if isinstance(g, web.Response):
            return g
        zone, body = g
        on = body.get("on")
        if not isinstance(on, bool):
            return web.json_response({"error": "bad request"}, status=400)
        async with lock:  # MA #43 F3
            mode = "off"
            others = []
            if on:
                st, attr = await unit(zone)
                if st not in (None, "off"):
                    return done(zone)
                mode = "heat" if zone.get("heat_only") or "cool" not in (attr.get("hvac_modes") or []) else (
                    engine.st.get("last_on_mode", {}).get(zone["name"], "heat"))
                if not zone.get("heat_only"):
                    # 0.6.0 shared-mode lock: turning a split on joins the direction the other split already runs
                    mode = await running_family(zone) or mode
                    for other in split_zones(zone):  # an auto / heat_cool split is brought to that direction
                        ost, _ = await unit(other)
                        if core.mode_family(ost) not in (None, core.mode_family(mode)):
                            others.append((other, ost))
            else:
                st, _ = await unit(zone)
                if st in ("heat", "cool"):
                    engine.st.setdefault("last_on_mode", {})[zone["name"]] = st
            if mode != "off" and zone.get("heat_only") and await vacation():
                return web.json_response({"error": "vacation: radiant stays off"}, status=409)
            err = await switch_group(zone, mode, others)
            if err:
                return web.json_response(dict(engine.snapshot(), error=err), status=502)
            return done(zone, mode=mode)

    async def api_fan(r):
        g = await zone_write(r)
        if isinstance(g, web.Response):
            return g
        zone, body = g
        if zone.get("heat_only"):
            return web.json_response({"error": "zone has no fan"}, status=400)
        fan = body.get("fan_mode")
        async with lock:
            _st, attr = await unit(zone)
            if not isinstance(fan, str) or fan not in (attr.get("fan_modes") or []):
                return web.json_response({"error": "bad fan mode"}, status=400)
            await call("set_fan_mode", {"entity_id": zone["climate"], "fan_mode": fan})
        return done(zone, fan_mode=fan)

    async def api_preference(r):
        """Save a schedule comfort target. Never starts a hold and never writes the climate entity itself.
        A following cycle may apply it only where no hold is active. Safe to call while holds exist."""
        zone = engine.zone(r.match_info["zone"])
        if not zone:
            return web.json_response({"error": "no zone"}, status=404)
        body = await read_json(r)
        if body is None:
            return web.json_response({"error": "bad request"}, status=400)
        before = dict(engine.st.get("override_until") or {})
        try:
            engine.set_zone_preference(zone["name"], str(body.get("profile") or ""), body.get("temperature"))
        except ValueError:
            return web.json_response({"error": "bad preference"}, status=400)
        if engine.st.get("override_until") != before:
            engine.st["override_until"] = before  # preference must not create or clear a hold
            engine.save()
        await run_once()
        return web.json_response(engine.snapshot())

    async def api_override(r):
        """Temporary setpoint. hours is required so the duration is never an implied 2 h."""
        g = await zone_write(r)
        if isinstance(g, web.Response):
            return g
        zone, body = g
        hours, err = parse_hours(body, required=True)
        if err:
            return err
        try:
            t = float(body.get("temperature"))
        except (TypeError, ValueError):
            return web.json_response({"error": "bad temperature"}, status=400)
        hi = engine.radiant_setpoint_max if zone.get("heat_only") else 84
        if not 55 <= t <= hi:
            return web.json_response({"error": "out of range"}, status=400)
        async with lock:
            states = await asyncio.get_running_loop().run_in_executor(
                None, lambda: {x.get("entity_id"): x for x in engine.ha.get_states() or []})
            cl, _ = engine._get(states, zone.get("climate"))
            data, t, why = engine.setpoint_data(zone, states, cl, t)
            if data is None and why != "unchanged":
                return web.json_response({"error": "setpoint not supported: %s" % why}, status=409)
            if data is not None:
                await call("set_temperature", data)
        done(zone, setpoint=t, hours=hours)
        await run_once()  # mode follows the override temperature on this request, not the next interval
        return web.json_response(engine.snapshot())

    async def api_override_temp(r):
        """Remember one override temperature. Does not start or clear a hold and does not write the unit.
        Same gate as a schedule preference: allowed while the master switch is off and in practice mode."""
        zone = engine.zone(r.match_info["zone"])
        if not zone:
            return web.json_response({"error": "bad zone"}, status=400)
        body = await read_json(r)
        if body is None:
            return web.json_response({"error": "bad request"}, status=400)
        temp = body.get("temperature") if "temperature" in body else None
        if isinstance(temp, (bool, dict, list)) or temp is None:
            return web.json_response({"error": "bad temperature"}, status=400)
        before = dict(engine.st.get("override_until") or {})
        try:
            engine.set_override_temp(zone["name"], temp)
        except ValueError:
            return web.json_response({"error": "out of range"}, status=400)
        if engine.st.get("override_until") != before:
            engine.st["override_until"] = before
            engine.save()
        return web.json_response(engine.snapshot())

    async def api_resume_all(r):
        """Drop every zone hold and let the schedule run. Does not change the global profile."""
        if await read_json(r) is None:
            return web.json_response({"error": "bad request"}, status=400)
        engine.resume_all()
        await run_once()
        return web.json_response(engine.snapshot())

    async def api_hold(r):
        """Start (or extend) a manual hold without changing the unit. Optional hours 0.5-12."""
        zone = engine.zone(r.match_info["zone"])
        if not zone:
            return web.json_response({"error": "no zone"}, status=404)
        body = await read_json(r)
        if body is None:
            return web.json_response({"error": "bad request"}, status=400)
        h = body.get("hours")
        if h is not None:
            if isinstance(h, bool) or not isinstance(h, (int, float)) or not 0.5 <= h <= 12:
                return web.json_response({"error": "bad hours"}, status=400)
        engine.start_override(zone["name"], h)
        if engine.saved_override_temp(zone["name"]) is not None:
            await run_once()  # a remembered temperature picks heat or cool on this request
        return web.json_response(engine.snapshot())

    async def api_resume(r):
        if r.content_type != "application/json":
            return web.json_response({"error": "bad request"}, status=400)
        if not engine.zone(r.match_info["zone"]):
            return web.json_response({"error": "no zone"}, status=404)
        # Restore the scheduled setpoint: forget the card's own write so it is not re-held as manual.
        engine.resume(r.match_info["zone"])
        await run_once()
        return web.json_response(engine.snapshot())

    async def api_history(r):
        zone = engine.zone(r.match_info["zone"])
        if not zone:
            return web.json_response({"error": "no zone"}, status=404)
        rng = r.query.get("range", "24h")
        if rng not in engine.HISTORY_RANGES:
            return web.json_response({"error": "bad range"}, status=400)
        try:
            data = await asyncio.get_running_loop().run_in_executor(None, engine.history, zone, rng)
        except Exception as e:  # HA unreachable -> empty graph, never 500
            log.warning("history failed: %s", type(e).__name__)
            return web.json_response({"error": "history unavailable"}, status=502)
        return web.json_response(data)

    async def thermostat(_r):
        return web.FileResponse(os.path.join(STATIC, "thermostat.html"))

    async def classic(_r):
        return web.FileResponse(os.path.join(STATIC, "classic.html"))

    app.router.add_get("/", index)
    app.router.add_get("/classic", classic)
    app.router.add_get("/api/state", api_state)
    app.router.add_get("/api/health", api_health)
    app.router.add_post("/api/profile", api_profile)
    app.router.add_post("/api/preset", api_preset)
    app.router.add_post("/api/zone/{zone}/preference", api_preference)
    app.router.add_post("/api/zone/{zone}/override-temp", api_override_temp)
    app.router.add_post("/api/zone/{zone}/override", api_override)
    app.router.add_post("/api/resume-all", api_resume_all)
    app.router.add_post("/api/zone/{zone}/setpoint", api_setpoint)
    app.router.add_post("/api/zone/{zone}/mode", api_mode)
    app.router.add_post("/api/zone/{zone}/power", api_power)
    app.router.add_post("/api/zone/{zone}/fan", api_fan)
    app.router.add_post("/api/zone/{zone}/hold", api_hold)
    app.router.add_post("/api/zone/{zone}/resume", api_resume)
    app.router.add_get("/api/zone/{zone}/history", api_history)
    async def heater_loop(_app):
        """0.8.0 spare bathroom heater: own 30 s loop, independent of the split cycle. Plug off at start and stop."""
        if heater is None:
            yield
            return
        run = asyncio.get_running_loop().run_in_executor
        try:
            await run(None, heater.safe_off, "startup")
        except Exception as e:
            log.warning("heater startup off failed: %s", type(e).__name__)

        async def _t():
            while True:
                await run(None, heater.tick)
                await asyncio.sleep(heater_tick)
        task = asyncio.create_task(_t())
        yield
        task.cancel()
        try:
            await run(None, heater.safe_off, "shutdown")
        except Exception as e:
            log.warning("heater shutdown off failed: %s", type(e).__name__)

    async def api_heater(r):
        if heater is None or r.match_info["zone"] != "spare_bathroom" or not heater.configured:
            return web.json_response({"error": "no zone"}, status=404)
        body = await read_json(r)
        if body is None:
            return web.json_response({"error": "bad request"}, status=400)
        try:
            if r.match_info["what"] == "mode":
                heater.set_mode(body.get("hvac_mode"))
            elif r.match_info["what"] == "setpoint":
                t = body.get("temperature")
                if isinstance(t, bool):
                    raise ValueError
                heater.set_setpoint(t)
            else:
                return web.json_response({"error": "bad request"}, status=400)
        except ValueError:
            return web.json_response({"error": "bad value"}, status=400)
        await asyncio.get_running_loop().run_in_executor(None, heater.tick)
        return web.json_response(engine.snapshot())

    app.router.add_get("/thermostat", thermostat)
    app.router.add_post("/api/heater/{zone}/{what}", api_heater)
    app.cleanup_ctx.append(loop)
    app.cleanup_ctx.append(heater_loop)
    return app


# Exact request line + 2xx status only (0.7.4). Errors, other methods, /api/healthz and query strings still log.
# aiohttp default access format: %a %t "%r" %s ... -> match from line start so a UA/Referer
# containing the health request line cannot hide another request.
_HEALTH_OK = re.compile(r'^\S+ \[[^\]]*\] "GET /api/health HTTP/1\.[01]" 2\d\d ')


class _NoHealthAccess(logging.Filter):
    """Drop only successful aiohttp access lines for exactly GET /api/health (watchdog / HEALTHCHECK)."""
    def filter(self, record: logging.LogRecord) -> bool:
        return _HEALTH_OK.match(record.getMessage()) is None


def main():
    opts = load_options()
    logging.basicConfig(level=opts.get("log_level", "info").upper())
    logging.getLogger("aiohttp.access").addFilter(_NoHealthAccess())
    eng = Engine(opts, HAClient(), STATE)
    log.info("Smart HVAC start: enabled=%s dry_run=%s zones=%d", eng.enabled, eng.dry_run,
             len(opts.get("zones", [])))
    ha = eng.ha
    heater = SpareHeater(opts, ha, HEATER_STATE)
    log.info("Spare bathroom heater: %s", "configured" if heater.configured else "not configured (disabled)")
    web.run_app(make_app(eng, int(opts.get("interval_s", 300)), heater=heater), host="0.0.0.0", port=8099)


if __name__ == "__main__":
    main()
