"""0.6.1 learned warm-up: per-zone recovery rate from the zone's own runs sizes the pre-wake lead."""
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

NOW = 1_800_000_000


def test_fallback_until_three_samples():
    assert core.learned_lead([], 50, 3) == 45
    assert core.learned_lead([[50, 1.0], [52, 1.2]], 50, 3) == 45


def test_31_oct_room_a_needs_a_much_earlier_start():
    # observed ~0.5 F/h at 52-53 F outside (69.8 -> 71 took ~140 min); gap 68 -> 71 = 3 F
    lead = core.learned_lead([[53, 0.5], [52, 0.5], [53, 0.6]], 53, 3)
    assert lead == 180  # clamped at the 3 h ceiling (unclamped ~380 min)
    assert core.learned_lead([[53, 3.0], [52, 3.0], [53, 3.0]], 53, 3) == 71


def test_rate_tracks_outdoor_and_never_extrapolates_wildly():
    s = [[30, 1.0], [40, 2.0], [50, 3.0], [60, 4.0]]
    assert abs(core.learned_rate(s, 45) - 2.5) < 1e-6
    assert core.learned_rate(s, -40) == core.learned_rate(s, 25)  # clamped to 5 F below the coldest seen
    assert core.RATE_MIN <= core.learned_rate([[50, 99], [50, 99], [50, 99]], 50) <= core.RATE_MAX


def test_lead_bounds():
    s = [[50, 2.0]] * 3
    assert core.learned_lead(s, 50, 0) == 15 and core.learned_lead(s, 50, -2) == 15
    assert 15 <= core.learned_lead(s, 50, 1) <= 180 and core.learned_lead(s, 50, 50) == 180


def test_warmup_step_records_a_completed_run():
    rec = core.warmup_step(None, "heat", "heating", 68.0, 71.0, 50, 0)
    assert rec["ep"]["t0"] == 68.0
    rec = core.warmup_step(rec, "heat", "heating", 69.5, 71.0, 50, 3600)
    rec = core.warmup_step(rec, "heat", "idle", 70.6, 71.0, 50, 7200)
    assert rec["ep"] is None and rec["samples"]["heat"] == [[50.0, 1.3]]


def test_warmup_step_drops_runs_on_setpoint_or_mode_change_and_short_runs():
    rec = core.warmup_step(None, "heat", "heating", 68.0, 71.0, 50, 0)
    rec = core.warmup_step(rec, "heat", "heating", 69.0, 72.0, 50, 600)  # setpoint changed -> new run at 69
    assert rec["ep"]["t0"] == 69.0
    rec = core.warmup_step(rec, "heat", "idle", 71.6, 72.0, 50, 900)  # 5 min only -> no sample
    assert rec["samples"]["heat"] == []
    rec = core.warmup_step(None, "cool", "cooling", 76.0, 72.0, 85, 0)
    rec = core.warmup_step(rec, "cool", "idle", 72.4, 72.0, 85, 3600)
    assert rec["samples"]["cool"] == [[85.0, 3.6]]


def test_warmup_step_survives_bad_shapes():
    for bad in (None, [], "x", {"samples": "x", "ep": 3}, {"samples": {"heat": [1, [2]]}}):
        rec = core.warmup_step(bad, "heat", "heating", 68, 71, 50, 0)
        assert rec["samples"]["heat"] == [] and rec["ep"]


def test_option_off_is_byte_identical_to_060():
    golden = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "defrost_off_golden.json")))
    assert json.loads(defrost_scenario.run()) == golden


def _eng(o, s):
    ha = StampedHA(s)
    return Engine(o, ha, None, clock=lambda: NOW, localtime=lt14), ha


def test_engine_learns_and_reports_the_lead():
    e, ha = _eng(opts(learned_preheat=True), st(lr=("heat", 71, 68), br=("heat", 71, 68), outdoor=50))
    ha.s["climate.lr"][1]["hvac_action"] = "heating"
    r = e.evaluate()
    assert r["lr"]["preheat_lead_min"] == 45 and e.st["warmup"]["lr"]["ep"]
    assert "preheat_lead_min" not in r["bath"]


def test_engine_option_off_keeps_no_warmup_state():
    e, _ha = _eng(opts(), st())
    r = e.evaluate()
    assert "warmup" not in e.st and "preheat_lead_min" not in r["lr"]


def test_engine_bad_warmup_state_is_reset():
    e, _ha = _eng(opts(learned_preheat=True), st())
    e.st["warmup"] = "garbage"
    e.evaluate()
    assert isinstance(e.st["warmup"], dict)


def test_learned_lead_moves_the_preheat_start():
    c = dict(core.DEFAULTS)
    wake = core._hm(c["wake"])
    for lead, expect in ((45, "home"), (120, "home")):
        c["preheat_lead_min"] = lead
        assert core.pick_profile("auto", True, wake - lead + 1, c) == expect
    c["preheat_lead_min"] = 45
    assert core.pick_profile("auto", True, wake - 120 + 1, c) == "sleep"
