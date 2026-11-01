"""MA #46: a corrupt learner state or a learner fault is 'option off' for that learner; the mode lock always runs."""
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rootfs", "app"))
sys.path.insert(0, os.path.dirname(__file__))

import core  # noqa: E402
from engine import Engine  # noqa: E402
from test_060_mode_lock import StampedHA, opts, st  # noqa: E402
from test_engine import lt14  # noqa: E402

NOW = 1_800_000_000


def test_sleep_advance_none_rate_is_zero():
    assert core.sleep_advance([[50, 0]] * 3, 50, 74, 68, 120) == 0
    assert core.sleep_advance([["x", "y"]] * 3, 50, 74, 68, 120) == 0


def test_corrupt_coast_state_still_corrects_opposite_splits():
    ha = StampedHA(st(lr=("cool", 71, 72), br=("heat", 71, 70)))
    e = Engine(opts(sleep_coast_max_min=120), ha, None, clock=lambda: NOW, localtime=lt14)
    e.st["coast"] = {"lr": {"samples": [[50, 0]] * 3}, "br": {"samples": [[50, 0]] * 3, "ep": {"mode": "heat"}}}
    r = e.evaluate()
    assert not core.shared_mode_conflict([ha.s["climate.lr"][0], ha.s["climate.br"][0]])
    assert r["lr"].get("sleep_advance_min", 0) == 0


def test_warmup_step_corrupt_entry_no_keyerror():
    rec = {"ep": {"mode": "heat", "sp": 71.0}, "samples": {"heat": [], "cool": []}}
    out = core.warmup_step(rec, "heat", "heating", 70.8, 71, 40, NOW)
    assert out["ep"] is None or "ts" in out["ep"]
    rec = {"ep": {"mode": "heat", "sp": 71.0, "ts": "bad", "t0": None}, "samples": {}}
    core.warmup_step(rec, "heat", "heating", 70.8, 71, 40, NOW)
    rec = {"ep": {"mode": "heat"}, "samples": []}
    assert core.coast_step(rec, "heat", "idle", 72, 68, 40, NOW)["ep"]["ts"] == NOW


@pytest.mark.parametrize("learner", ["warmup_step", "coast_step", "sleep_advance", "learned_lead"])
def test_mode_lock_runs_when_any_learner_raises(monkeypatch, caplog, learner):
    def boom(*a, **k):
        raise RuntimeError("learner fault")
    monkeypatch.setattr(core, learner, boom)
    ha = StampedHA(st(lr=("cool", 71, 72), br=("heat", 71, 70)))
    e = Engine(opts(learned_preheat=True, sleep_coast_max_min=120), ha, None, clock=lambda: NOW, localtime=lt14)
    calls = []
    real = e._apply_mode_lock
    monkeypatch.setattr(e, "_apply_mode_lock", lambda *a: (calls.append(1), real(*a))[1])
    with caplog.at_level(logging.WARNING, logger="smart_hvac"):
        e.evaluate()
        e.evaluate()
    assert len(calls) == 2
    assert not core.shared_mode_conflict([ha.s["climate.lr"][0], ha.s["climate.br"][0]])
    assert sum("learner disabled" in m for m in caplog.messages) == 1  # logged once


def test_mode_lock_runs_when_forecast_learner_raises(monkeypatch):
    calls = []
    ha = StampedHA(st(lr=("cool", 71, 72), br=("heat", 71, 70)))
    e = Engine(opts(forecast_anticipation=True, learned_preheat=True), ha, None, clock=lambda: NOW, localtime=lt14)
    monkeypatch.setattr(e, "_forecast", lambda *a: (_ for _ in ()).throw(RuntimeError("forecast fault")))
    real = e._apply_mode_lock
    monkeypatch.setattr(e, "_apply_mode_lock", lambda *a: (calls.append(1), real(*a))[1])
    e.evaluate()
    assert calls == [1]
    assert not core.shared_mode_conflict([ha.s["climate.lr"][0], ha.s["climate.br"][0]])
