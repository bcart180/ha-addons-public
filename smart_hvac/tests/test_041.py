"""0.4.1 (MA L1/L2): defrost/preheat timeout + defrost_stuck event; removed zones' events pruned at start."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rootfs", "app"))
sys.path.insert(0, os.path.dirname(__file__))
import pytest  # noqa: E402

from engine import DEFROST_TIMEOUT_S, EVENT_KINDS, Engine  # noqa: E402
from test_040 import Clock, act  # noqa: E402
from test_engine import FakeHA, lt14, opts, states  # noqa: E402


def setup():
    ha, clk = FakeHA(states()), Clock()
    return Engine(opts(), ha, None, clock=clk, localtime=lt14), ha, clk


def kinds(e, name="lr"):
    return [x[1:] for x in e.st["events"].get(name, [])]


def test_timeout_is_30_min_and_kind_known():
    assert DEFROST_TIMEOUT_S == 1800 and "defrost_stuck" in EVENT_KINDS


@pytest.mark.parametrize("a", ["defrosting", "preheating"])
def test_stuck_defrost_resumes_normal_logic_and_flags(a):
    e, ha, clk = setup()
    e.evaluate()
    act(ha, "climate.lr", a)
    ha.s["climate.lr"] = ("heat", dict(ha.s["climate.lr"][1], temperature=75))  # person's change mid-defrost
    for _ in range(7):  # first seen + 30 min: still waiting
        clk.t += 300
        r = e.evaluate()
        assert r["lr"]["reason"] == a and not r["lr"]["defrost_stuck"]
    assert e.snapshot()["zones"][0]["override_until"] is None
    clk.t += 300  # 35 min after first seen: over the timeout
    r = e.evaluate()
    assert r["lr"]["defrost_stuck"] and r["lr"]["defrost"] is None and r["lr"]["reason"] != a
    assert ["defrost_stuck", "start"] in kinds(e) and ["defrost", "end"] in kinds(e)
    assert e.snapshot()["zones"][0]["override_until"]  # the 75 is now seen as a manual change
    clk.t += 300
    act(ha, "climate.lr", "heating")
    r = e.evaluate()
    assert not r["lr"]["defrost_stuck"] and ["defrost_stuck", "end"] in kinds(e)
    assert "lr" not in e.st["defrost_since"]


def test_short_defrost_spells_do_not_accumulate():
    e, ha, clk = setup()
    e.evaluate()
    for _ in range(5):
        for a in ("defrosting",) * 4 + ("heating",):
            clk.t += 300
            act(ha, "climate.lr", a)
            r = e.evaluate()
            assert not r["lr"]["defrost_stuck"]
    assert ["defrost_stuck", "start"] not in kinds(e)


def test_removed_zone_events_pruned_at_start(tmp_path):
    p = str(tmp_path / "st.json")
    json.dump({"events": {"lr": [[1, "hold", "start"]], "old_zone": [[1, "hold", "start"]]},
               "active": {"old_zone": ["hold"], "lr": ["hold"]}, "defrost_since": {"old_zone": 1}}, open(p, "w"))
    e = Engine(opts(), FakeHA(states()), p, localtime=lt14)
    assert set(e.st["events"]) == {"lr"} and set(e.st["active"]) == {"lr"} and e.st["defrost_since"] == {}
    assert e.prune_zones() == []


def test_prune_logs(tmp_path, caplog):
    p = str(tmp_path / "st.json")
    json.dump({"events": {"gone": []}}, open(p, "w"))
    with caplog.at_level("INFO", logger="smart_hvac"):
        Engine(opts(), FakeHA(states()), p, localtime=lt14)
    assert any("pruned state of removed zones: gone" in m for m in caplog.messages)
