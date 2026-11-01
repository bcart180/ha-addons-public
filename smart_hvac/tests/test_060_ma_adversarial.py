import copy, random, pytest
from test_060_mode_lock import *  # noqa
from test_060_mode_lock import st, opts, StampedHA, split_modes, NOW, lt14, Engine, core, make_app

class TraceHA(StampedHA):
    def __init__(s, states, changed=None, fail_on=None):
        super().__init__(states, changed); s.trace=[]; s.fail_on=fail_on
    def call(s, d, svc, data):
        if s.fail_on and s.fail_on(svc, data): raise OSError("boom")
        super().call(d, svc, data); s.trace.append(tuple(split_modes(s)))

def transient(ha): return [t for t in ha.trace if core.shared_mode_conflict(t)]

# 1. auto / heat_cool on one split is never treated as a conflict
@pytest.mark.parametrize("m", ["auto", "heat_cool"])
def test_auto_is_left_next_to_heat(m):
    s = st(lr=(m, 71, 76), br=("heat", 71, 66), outdoor=50)
    s["climate.lr"][1]["hvac_modes"] += ["auto", "heat_cool"]
    ha = TraceHA(s); e = Engine(opts(), ha, None, clock=lambda: NOW, localtime=lt14)
    e.start_override("lr"); e.evaluate()
    print("AUTO", m, split_modes(ha))
    assert split_modes(ha)[0] not in ("auto", "heat_cool"), "auto split left beside heat split"

# 2. transient opposite during an API direction change
async def test_api_mode_transient(aiohttp_client, monkeypatch):
    monkeypatch.setenv("SH_ALLOW", "127.0.0.1")
    ha = TraceHA(st()); e = Engine(opts(), ha, None, clock=lambda: NOW, localtime=lt14)
    c = await aiohttp_client(make_app(e, interval=3600))
    await c.post("/api/zone/lr/mode", json={"hvac_mode": "cool"})
    print("API trace", ha.trace)
    assert not transient(ha)

# 3. partial failure on API: other succeeds, primary fails
async def test_api_partial_failure(aiohttp_client, monkeypatch):
    monkeypatch.setenv("SH_ALLOW", "127.0.0.1")
    ha = TraceHA(st(), fail_on=lambda svc, d: svc == "set_hvac_mode" and d["entity_id"] == "climate.lr")
    e = Engine(opts(), ha, None, clock=lambda: NOW, localtime=lt14)
    c = await aiohttp_client(make_app(e, interval=3600))
    r = await c.post("/api/zone/lr/mode", json={"hvac_mode": "cool"})
    print("partial", r.status, split_modes(ha))
    assert not core.shared_mode_conflict(split_modes(ha))

# 4. engine partial failure: forced write to one split fails mid-loop
def test_engine_partial_failure_and_transient():
    found_t = found_p = 0
    rnd = random.Random(7)
    for _ in range(600):
        lr = (rnd.choice(["heat","cool","off","dry"]), rnd.choice([60,68,71,78]), rnd.choice([60,66,71,76,82]))
        br = (rnd.choice(["heat","cool","off","dry"]), rnd.choice([60,68,71,78]), rnd.choice([60,66,71,76,82]))
        o = rnd.choice([30,55,70,80,95])
        for fail in (None, "climate.lr", "climate.br"):
            ha = TraceHA(st(lr=lr, br=br, outdoor=o), fail_on=(lambda f: (lambda svc,d: svc=="set_hvac_mode" and d["entity_id"]==f))(fail) if fail else None)
            e = Engine(opts(), ha, None, clock=lambda: NOW, localtime=lt14)
            try: e.evaluate()
            except OSError: pass
            if transient(ha) and not fail: found_t += 1
            if fail and core.shared_mode_conflict(split_modes(ha)) and not core.shared_mode_conflict([lr[0], br[0]]): found_p += 1
    print("engine transient cases", found_t, "partial-fail new conflicts", found_p)
    assert found_t == 0 and found_p == 0

# 5. race: API changes both splits after the engine's state read (stale read)
def test_stale_read_race():
    bad = 0; rnd = random.Random(11)
    for _ in range(800):
        lr = (rnd.choice(["heat","cool","off"]), rnd.choice([60,68,71,78]), rnd.choice([60,66,71,76,82]))
        br = (rnd.choice(["heat","cool","off"]), rnd.choice([60,68,71,78]), rnd.choice([60,66,71,76,82]))
        s = st(lr=lr, br=br, outdoor=rnd.choice([30,55,70,80,95]))
        if core.shared_mode_conflict([lr[0], br[0]]): continue
        flip = rnd.choice(["heat","cool"])
        class RaceHA(TraceHA):
            def get_states(self):
                out = super().get_states()
                for k in ("climate.lr","climate.br"):
                    if self.s[k][0] != "off": self.s[k] = (flip, self.s[k][1])   # API write lands mid-cycle
                return out
        ha = RaceHA(s); e = Engine(opts(), ha, None, clock=lambda: NOW, localtime=lt14)
        e.evaluate()
        if core.shared_mode_conflict(split_modes(ha)): bad += 1
    print("stale-read conflicts", bad)
    assert bad == 0

def test_recovery_next_cycle_after_one_off_failure():
    rnd = random.Random(7); stuck = 0; n = 0
    for _ in range(600):
        lr = (rnd.choice(["heat","cool","off","dry"]), rnd.choice([60,68,71,78]), rnd.choice([60,66,71,76,82]))
        br = (rnd.choice(["heat","cool","off","dry"]), rnd.choice([60,68,71,78]), rnd.choice([60,66,71,76,82]))
        o = rnd.choice([30,55,70,80,95])
        for fail in ("climate.lr", "climate.br"):
            box = {"n": 1}
            def f(svc, d, fail=fail, box=box):
                if svc == "set_hvac_mode" and d["entity_id"] == fail and box["n"]: box["n"] = 0; return True
            ha = TraceHA(st(lr=lr, br=br, outdoor=o), fail_on=f)
            e = Engine(opts(), ha, None, clock=lambda: NOW, localtime=lt14)
            try: e.evaluate()
            except OSError: pass
            if core.shared_mode_conflict(split_modes(ha)):
                n += 1; e.evaluate()
                if core.shared_mode_conflict(split_modes(ha)): stuck += 1
    print("one-off failure conflicts", n, "still conflicted after next cycle", stuck)
    assert stuck == 0
