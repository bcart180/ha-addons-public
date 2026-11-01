"""0.6.3 sleep coast: start the sleep setback early enough that the room drifts down by bedtime."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rootfs", "app"))
sys.path.insert(0, os.path.dirname(__file__))

import core  # noqa: E402
import defrost_scenario  # noqa: E402
from engine import Engine  # noqa: E402
from test_060_mode_lock import StampedHA, opts, st  # noqa: E402
from test_engine import lt14  # noqa: E402

SLEEP = core._hm(core.DEFAULTS["sleep_start"])


def test_advance_puts_sleep_before_sleep_start_only_when_set():
    c = dict(core.DEFAULTS)
    assert core.pick_profile("auto", True, SLEEP - 30, c) == "home"
    c["sleep_advance_min"] = 60
    assert core.pick_profile("auto", True, SLEEP - 30, c) == "sleep"
    assert core.pick_profile("auto", True, SLEEP - 61, c) == "home"
    assert core.pick_profile("auto", False, SLEEP - 30, c) == "away"
    assert core.pick_profile("guest", True, SLEEP - 30, c) == "guest"


def test_31_oct_drift_rate_hits_the_cap():
    s = [[55, 0.4], [57, 0.45], [53, 0.4]]
    assert core.sleep_advance(s, 55, 71, 68, 120) == 120          # 3 F at 0.4 F/h = 450 min -> capped
    assert core.sleep_advance([[55, 3.0]] * 3, 55, 71, 68, 120) == 60
    assert core.sleep_advance(s, 55, 67, 68, 120) == 0            # already at / below target
    assert core.sleep_advance(s[:2], 55, 71, 68, 120) == 0        # not enough runs
    assert core.sleep_advance(s, 55, 71, 68, 0) == 0              # option off


def test_coast_step_learns_idle_drift_only():
    r = core.coast_step(None, "heat", "idle", 73.0, 68.0, 55, 0)
    r = core.coast_step(r, "heat", "idle", 72.0, 68.0, 55, 3600)
    r = core.coast_step(r, "heat", "heating", 71.0, 68.0, 55, 7200)  # unit starts -> run ends
    assert r["samples"] == [[55.0, 1.0]] and r["ep"] is None
    r = core.coast_step(None, "heat", "heating", 73.0, 68.0, 55, 0)
    assert r["ep"] is None  # actively heating is not drift
    for bad in (None, [], {"samples": "x", "ep": 1}):
        assert core.coast_step(bad, "heat", "idle", 73, 68, 55, 0)["ep"]


def test_option_off_is_byte_identical():
    golden = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "defrost_off_golden.json")))
    assert json.loads(defrost_scenario.run()) == golden


def test_engine_off_keeps_no_state_on_reports_advance():
    ha = StampedHA(st())
    e = Engine(opts(), ha, None, clock=lambda: 1_800_000_000, localtime=lt14)
    r = e.evaluate()
    assert "coast" not in e.st and "sleep_advance_min" not in r["lr"]
    ha = StampedHA(st(lr=("heat", 68, 72), br=("heat", 68, 72)))
    e = Engine(opts(sleep_coast_max_min=120), ha, None, clock=lambda: 1_800_000_000, localtime=lt14)
    e.st["coast"] = "bad"
    r = e.evaluate()
    assert r["lr"]["sleep_advance_min"] == 0 and e.st["coast"]["lr"]["ep"]
    assert "sleep_advance_min" not in r["bath"]


def test_early_sleep_never_splits_modes():
    ha = StampedHA(st(lr=("heat", 71, 76), br=("heat", 71, 66), outdoor=80))
    e = Engine(opts(sleep_coast_max_min=120), ha, None, clock=lambda: 1_800_000_000, localtime=lt14)
    e.st["coast"] = {z: {"samples": [[80, 0.5]] * 3, "ep": None} for z in ("lr", "br")}
    e.evaluate()
    assert not core.shared_mode_conflict([ha.s["climate.lr"][0], ha.s["climate.br"][0]])
