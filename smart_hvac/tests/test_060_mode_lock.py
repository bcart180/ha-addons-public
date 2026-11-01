"""0.6.0 shared-mode lock: the two mini splits share one outdoor unit, so heat and cool are never requested together.

Covers every path from the 31 Oct audit: core.pick_mode / decide (per-zone), engine write loop, /api/zone/<z>/mode,
/api/zone/<z>/power, resume / preset (engine cycle), the card (test in lovelace/tests), and a person's change made
outside the add-on (HomeKit / Siri on the raw entity) or on a split the app does not control.
"""
import itertools
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rootfs", "app"))
sys.path.insert(0, os.path.dirname(__file__))
import pytest  # noqa: E402

import core  # noqa: E402
from engine import Engine  # noqa: E402
from server import make_app  # noqa: E402
from test_engine import FakeHA, lt14  # noqa: E402

NOW = 1_800_000_000
SPLITS = [
    {"name": "lr", "label": "Room A", "climate": "climate.lr", "indoor": "sensor.lr_t", "heat_only": False,
     "facing_az": 225, "solar_factor": 0, "enabled": True},
    {"name": "br", "label": "Bed", "climate": "climate.br", "indoor": "sensor.br_t", "heat_only": False,
     "facing_az": 135, "solar_factor": 0, "enabled": True},
    {"name": "bath", "label": "Bath", "climate": "climate.bath", "indoor": "sensor.bath_t", "heat_only": True,
     "facing_az": 90, "solar_factor": 0, "enabled": True, "comfort": 72, "sleep": 66, "away": 60},
]
MODES = ["heat", "cool", "off", "fan_only", "dry", "auto", "heat_cool"]


class StampedHA(FakeHA):
    """FakeHA that also reports last_changed, like HA does."""
    def __init__(self, states, changed=None):
        super().__init__(states)
        self.changed = changed or {}

    def get_states(self):
        out = super().get_states()
        for s in out:
            if s["entity_id"] in self.changed:
                s["last_changed"] = self.changed[s["entity_id"]]
        return out


def st(lr=("heat", 70, 71), br=("heat", 70, 71), outdoor=60, bath=("heat", 72, 70)):
    return {
        "device_tracker.phone": ("home", {}),
        "weather.w": ("sunny", {"temperature": outdoor, "cloud_coverage": 0}),
        "sun.sun": ("below_horizon", {"elevation": -10, "azimuth": 0}),
        "climate.lr": (lr[0], {"temperature": lr[1], "current_temperature": lr[2],
                               "hvac_modes": ["off", "heat", "cool", "dry", "fan_only"]}),
        "sensor.lr_t": (str(lr[2]), {}),
        "climate.br": (br[0], {"temperature": br[1], "current_temperature": br[2],
                               "hvac_modes": ["off", "heat", "cool", "dry", "fan_only"]}),
        "sensor.br_t": (str(br[2]), {}),
        "climate.bath": (bath[0], {"temperature": bath[1], "current_temperature": bath[2]}),
        "sensor.bath_t": (str(bath[2]), {}),
    }


def opts(**kw):
    o = {"enabled": True, "dry_run": False, "presence_entity": "device_tracker.phone", "weather_entity": "weather.w",
         "min_cycle_min": 15, "override_hours": 2, "profiles": {"home": 71, "guest": 71}, "zones": SPLITS}
    o.update(kw)
    return o


def split_modes(ha):
    return [ha.s["climate.lr"][0], ha.s["climate.br"][0]]


def requested_modes(ha):
    return [d["hvac_mode"] for s, d in ha.calls if s == "set_hvac_mode" and d["entity_id"] != "climate.bath"]


# ---------- core ----------

def test_family():
    assert core.mode_family("dry") == "cool" and core.mode_family("heat") == "heat"
    assert core.mode_family("off") is None and core.mode_family("fan_only") is None
    assert core.shared_mode_conflict(["heat", "dry"]) and not core.shared_mode_conflict(["heat", "off", "fan_only"])


def test_resolver_exhaustive_never_leaves_a_conflict():
    """Every combination of want / live / held for two splits: after the fixes, no heat+cool pair remains."""
    for w1, w2, l1, l2, h1, h2, newest in itertools.product(MODES, MODES, MODES, MODES, (0, 1), (0, 1), (0, 1)):
        es = [{"name": "a", "want": w1, "live": l1, "held": bool(h1), "changed_ts": 2 if newest == 0 else 1},
              {"name": "b", "want": w2, "live": l2, "held": bool(h2), "changed_ts": 2 if newest == 1 else 1}]
        group, fixes = core.resolve_shared_mode(es)
        final = [fixes.get(e["name"], e["want"]) for e in es]
        assert not core.shared_mode_conflict(final), (es, fixes)
        assert all(core.mode_family(f) is None or core.mode_family(f) == group for f in final)
        for e in es:  # off / fan_only are never touched
            if core.mode_family(e["want"]) is None:
                assert e["name"] not in fixes


def test_resolver_precedence():
    a = {"name": "a", "want": "cool", "live": "cool", "held": True, "changed_ts": 5}
    b = {"name": "b", "want": "heat", "live": "heat", "held": False, "changed_ts": 9}
    assert core.resolve_shared_mode([a, b]) == ("cool", {"b": "cool"})        # person's hold wins over the app
    b["held"] = True
    assert core.resolve_shared_mode([a, b]) == ("heat", {"a": "heat"})        # both held: newest change wins
    a["held"] = b["held"] = False
    a["live"] = b["live"] = "heat"
    assert core.resolve_shared_mode([a, b]) == ("heat", {"a": "heat"})        # app only: keep the running direction
    a["live"] = b["live"] = "off"
    assert core.resolve_shared_mode([a, b]) == ("heat", {"a": "heat"})        # nothing running: heat


# ---------- engine ----------

def run(states, changed=None, **o):
    ha = StampedHA(states, changed)
    e = Engine(opts(**o), ha, None, clock=lambda: NOW, localtime=lt14)
    r = e.evaluate()
    return e, ha, r


def test_warm_day_hot_and_cold_rooms_never_split():
    """Above cool_lockout the old per-zone deadband picked cool for the hot room and heat for the cold one."""
    _e, ha, r = run(st(lr=("heat", 71, 76), br=("heat", 71, 66), outdoor=80))
    assert not core.shared_mode_conflict(split_modes(ha))
    assert not core.shared_mode_conflict(requested_modes(ha))
    assert "mode_lock" in {r["lr"]["reason"], r["br"]["reason"]}


def test_person_cool_hold_pulls_app_zone_to_cool():
    ha = StampedHA(st(lr=("cool", 72, 76), br=("heat", 71, 66), outdoor=80))
    e = Engine(opts(), ha, None, clock=lambda: NOW, localtime=lt14)
    e.start_override("lr")
    r = e.evaluate()
    assert split_modes(ha) == ["cool", "cool"] and r["br"]["reason"] == "mode_lock"


def test_siri_on_raw_entity_opposite_modes_newest_wins():
    s = st(lr=("cool", 72, 76), br=("heat", 71, 66), outdoor=80)
    ha = StampedHA(s, {"climate.lr": "2027-01-15T08:00:00+00:00", "climate.br": "2027-01-15T09:00:00+00:00"})
    e = Engine(opts(), ha, None, clock=lambda: NOW, localtime=lt14)
    e.start_override("lr")
    e.start_override("br")
    r = e.evaluate()
    assert split_modes(ha) == ["heat", "heat"] and r["lr"]["reason"] == "mode_lock"


def test_dry_run_requests_nothing_but_still_reports_the_lock():
    e, ha, r = run(st(lr=("heat", 71, 76), br=("heat", 71, 66), outdoor=80), dry_run=True)
    assert ha.calls == [] and not core.shared_mode_conflict([r["lr"]["mode"], r["br"]["mode"]])
    assert e.snapshot()["shared_mode"] in ("heat", "cool")


def test_disabled_split_still_counts():
    zones = [dict(SPLITS[0], enabled=False), SPLITS[1], SPLITS[2]]
    _e, ha, _ = run(st(lr=("cool", 72, 76), br=("heat", 71, 66), outdoor=80), zones=zones)
    assert split_modes(ha) == ["cool", "cool"]


def test_off_is_left_alone():
    _e, ha, _ = run(st(lr=("off", 71, 76), br=("heat", 71, 66), outdoor=50))
    assert ha.s["climate.lr"][0] == "off"


def test_resolver_locked_split_decides():
    es = [{"name": "a", "want": "heat", "live": "heat", "held": True, "changed_ts": 9},
          {"name": "b", "want": "cool", "live": "cool", "held": True, "locked": True, "changed_ts": 1}]
    assert core.resolve_shared_mode(es) == ("cool", {"a": "cool"})


def test_radiant_is_not_in_the_group():
    _e, ha, _ = run(st(lr=("cool", 72, 76), br=("cool", 72, 76), outdoor=85))
    assert ha.s["climate.bath"][0] == "heat"


def test_fuzz_no_cycle_ever_requests_or_leaves_opposite_modes():
    rnd = random.Random(60)
    for _ in range(1500):
        lr = (rnd.choice(MODES), rnd.choice([60, 68, 71, 78]), rnd.choice([60, 66, 71, 76, 82]))
        br = (rnd.choice(MODES), rnd.choice([60, 68, 71, 78]), rnd.choice([60, 66, 71, 76, 82]))
        ha = StampedHA(st(lr=lr, br=br, outdoor=rnd.choice([30, 55, 70, 80, 95])),
                       {"climate.lr": "2027-01-15T08:0%d:00+00:00" % rnd.randint(0, 9),
                        "climate.br": "2027-01-15T08:0%d:00+00:00" % rnd.randint(0, 9)})
        e = Engine(opts(), ha, None, clock=lambda: NOW, localtime=lt14)
        for z in ("lr", "br"):
            if rnd.random() < 0.4:
                e.start_override(z)
        for _cycle in range(3):
            e.evaluate()
            assert not core.shared_mode_conflict(split_modes(ha)), (lr, br, ha.calls)
        # every write the engine sent leaves the pair consistent
        assert not core.shared_mode_conflict(split_modes(ha))


# ---------- API (card writes) ----------

@pytest.fixture
def mk(aiohttp_client, monkeypatch):
    async def _mk(states, **o):
        monkeypatch.setenv("SH_ALLOW", "127.0.0.1")
        ha = StampedHA(states)
        e = Engine(opts(**o), ha, None, clock=lambda: NOW, localtime=lt14)
        return await aiohttp_client(make_app(e, interval=3600)), ha, e
    return _mk


async def test_api_mode_cool_switches_the_running_split_too(mk):
    c, ha, _ = await mk(st(lr=("heat", 71, 70), br=("heat", 71, 70)))
    assert (await c.post("/api/zone/lr/mode", json={"hvac_mode": "cool"})).status == 200
    assert split_modes(ha) == ["cool", "cool"]


async def test_api_mode_leaves_an_off_split_off_and_remembers(mk):
    c, ha, e = await mk(st(lr=("heat", 71, 70), br=("off", 71, 70)))
    await c.post("/api/zone/lr/mode", json={"hvac_mode": "cool"})
    assert split_modes(ha) == ["cool", "off"] and e.st["last_on_mode"]["br"] == "cool"


async def test_api_power_on_joins_the_running_direction(mk):
    c, ha, e = await mk(st(lr=("cool", 71, 75), br=("off", 71, 70), outdoor=85))
    e.st.setdefault("last_on_mode", {})["br"] = "heat"
    await c.post("/api/zone/br/power", json={"on": True})
    assert split_modes(ha) == ["cool", "cool"]


async def test_api_every_sequence_of_card_taps_never_splits(mk):
    c, ha, _ = await mk(st(lr=("heat", 71, 70), br=("heat", 71, 70)))
    taps = [("lr", "mode", {"hvac_mode": m}) for m in ("heat", "cool", "off")] + \
           [("br", "mode", {"hvac_mode": m}) for m in ("heat", "cool", "off")] + \
           [(z, "power", {"on": v}) for z in ("lr", "br") for v in (True, False)]
    for seq in itertools.product(taps, repeat=3):
        for z, what, body in seq:
            await c.post("/api/zone/%s/%s" % (z, what), json=body)
            assert not core.shared_mode_conflict(split_modes(ha)), seq


async def test_api_rejects_dry_and_fan_only_modes(mk):
    c, ha, _ = await mk(st())
    for m in ("dry", "fan_only", "auto"):
        assert (await c.post("/api/zone/lr/mode", json={"hvac_mode": m})).status == 400


# ---------- MA #43 re-gate: auto / heat_cool, write ordering, failed writes ----------

class FailHA(StampedHA):
    """Records the split pair after every write; set_hvac_mode on `fail` raises."""
    def __init__(self, states, fail=None):
        super().__init__(states)
        self.fail, self.trace = fail, []

    def call(self, d, svc, data):
        if svc == "set_hvac_mode" and data["entity_id"] == self.fail:
            raise OSError("write failed")
        super().call(d, svc, data)
        self.trace.append(tuple(split_modes(self)))


@pytest.mark.parametrize("m", ["auto", "heat_cool"])
def test_auto_counts_as_conflict(m):
    assert core.mode_family(m) == "auto"
    assert core.shared_mode_conflict([m, "heat"]) and core.shared_mode_conflict([m, "cool"])
    assert core.shared_mode_conflict([m, "auto"]) and not core.shared_mode_conflict([m, "off", "fan_only"])
    es = [{"name": "a", "want": m, "live": m, "held": True, "changed_ts": 9},
          {"name": "b", "want": "cool", "live": "cool", "held": False, "changed_ts": 1}]
    assert core.resolve_shared_mode(es) == ("cool", {"a": "cool"})   # auto is never the group direction


def test_engine_flip_writes_off_first_never_heat_and_cool():
    ha = FailHA(st(lr=("heat", 72, 76), br=("heat", 72, 76), outdoor=85))
    e = Engine(opts(), ha, None, clock=lambda: NOW, localtime=lt14)
    e.start_override("lr")
    ha.s["climate.lr"] = ("cool", ha.s["climate.lr"][1])  # a person flips lr to cool
    e.evaluate()
    assert not any(core.shared_mode_conflict(t) for t in ha.trace), ha.trace


def test_engine_failed_off_write_leaves_no_conflict():
    ha = FailHA(st(lr=("cool", 72, 76), br=("heat", 72, 66), outdoor=85), fail="climate.br")
    e = Engine(opts(), ha, None, clock=lambda: NOW, localtime=lt14)
    e.start_override("lr")
    e.evaluate()  # br cannot be written: lr is stopped rather than left cooling beside a heating head
    assert not core.shared_mode_conflict(split_modes(ha))


async def test_api_mode_primary_failure_rolls_back_and_returns_502(aiohttp_client, monkeypatch):
    monkeypatch.setenv("SH_ALLOW", "127.0.0.1")
    ha = FailHA(st(), fail="climate.lr")
    e = Engine(opts(), ha, None, clock=lambda: NOW, localtime=lt14)
    c = await aiohttp_client(make_app(e, interval=3600))
    r = await c.post("/api/zone/lr/mode", json={"hvac_mode": "cool"})
    assert r.status == 502 and split_modes(ha) == ["heat", "heat"]
    assert not any(core.shared_mode_conflict(t) for t in ha.trace)


async def test_api_mode_other_off_failure_changes_nothing(aiohttp_client, monkeypatch):
    monkeypatch.setenv("SH_ALLOW", "127.0.0.1")
    ha = FailHA(st(), fail="climate.br")
    e = Engine(opts(), ha, None, clock=lambda: NOW, localtime=lt14)
    c = await aiohttp_client(make_app(e, interval=3600))
    r = await c.post("/api/zone/lr/mode", json={"hvac_mode": "cool"})
    assert r.status == 502 and split_modes(ha) == ["heat", "heat"]


async def test_api_mode_corrects_an_auto_split(mk):
    c, ha, _ = await mk(st(lr=("heat", 71, 70), br=("auto", 71, 70)))
    assert (await c.post("/api/zone/lr/mode", json={"hvac_mode": "cool"})).status == 200
    assert split_modes(ha) == ["cool", "cool"]


async def test_api_power_on_beside_auto_split(mk):
    c, ha, e = await mk(st(lr=("auto", 71, 75), br=("off", 71, 70)))
    await c.post("/api/zone/br/power", json={"on": True})
    assert not core.shared_mode_conflict(split_modes(ha)) and "auto" not in split_modes(ha)
