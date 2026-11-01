"""0.6.2 presence: person_a + person_b phones, arrivals at once, departures debounced (away_delay_min)."""
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

D = 15 * 60


def test_departure_needs_the_full_delay_without_a_break():
    r = core.presence_debounce(None, True, 0, D)
    r = core.presence_debounce(r, False, 60, D)
    assert r["confirmed"] is True
    r = core.presence_debounce(r, False, 60 + D - 1, D)
    assert r["confirmed"] is True
    r = core.presence_debounce(r, False, 60 + D, D)
    assert r["confirmed"] is False


def test_a_blip_resets_the_timer_and_arrival_is_instant():
    r = core.presence_debounce(None, True, 0, D)
    r = core.presence_debounce(r, False, 100, D)
    r = core.presence_debounce(r, True, 200, D)
    r = core.presence_debounce(r, False, 300, D)
    assert core.presence_debounce(r, False, 100 + D, D)["confirmed"] is True
    away = core.presence_debounce({"confirmed": False, "away_since": 0}, True, 5, D)
    assert away["confirmed"] is True


def test_unknown_keeps_record_and_bad_shapes_are_safe():
    r = core.presence_debounce({"confirmed": True, "away_since": 50}, None, 99, D)
    assert r == {"confirmed": True, "away_since": 50}
    for bad in (None, [], "x", {"confirmed": "y", "away_since": "z"}):
        assert core.presence_debounce(bad, False, 10, D)["confirmed"] is False  # first reading counts as is


def test_delay_zero_is_the_old_behaviour():
    assert core.presence_debounce({"confirmed": True}, False, 10, 0)["confirmed"] is False


def test_option_off_is_byte_identical():
    golden = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "defrost_off_golden.json")))
    assert json.loads(defrost_scenario.run()) == golden


def _eng(o, s, clock):
    ha = StampedHA(s)
    return Engine(o, ha, None, clock=lambda: clock[0], localtime=lt14), ha


def two_phones(person_a, person_b):
    s = st()
    s.pop("device_tracker.phone")
    s["device_tracker.person_a"] = (person_a, {})
    s["device_tracker.person_b"] = (person_b, {})
    return s


def o(**kw):
    return opts(presence_entity="", presence_entities=["device_tracker.person_a", "device_tracker.person_b"], **kw)


def test_person_b_home_keeps_the_house_home():
    clock = [1_800_000_000]
    e, ha = _eng(o(away_delay_min=15), two_phones("not_home", "home"), clock)
    e.evaluate()
    assert e.st["last_result"]["presence_home"] is True


def test_person_b_wifi_flap_does_not_set_back():
    """Seen 28 Oct ~1 AM: person_b's tracker flipped unavailable / not_home every few seconds while home."""
    clock = [1_800_000_000]
    e, ha = _eng(o(away_delay_min=15), two_phones("not_home", "home"), clock)
    e.evaluate()
    for i, k in enumerate(["not_home", "unavailable", "not_home", "home", "not_home", "unavailable"]):
        clock[0] += 60
        ha.s["device_tracker.person_b"] = (k, {})
        e.evaluate()
        assert e.st["last_result"]["presence_home"] is True, (i, k)
    clock[0] += D
    ha.s["device_tracker.person_b"] = ("not_home", {})
    e.evaluate()
    assert e.st["last_result"]["presence_home"] is False


def test_engine_option_off_keeps_old_immediate_away_and_no_state():
    clock = [1_800_000_000]
    e, ha = _eng(o(), two_phones("not_home", "not_home"), clock)
    e.evaluate()
    assert e.st["last_result"]["presence_home"] is False and "presence" not in e.st


def test_everyone_unreadable_is_still_a_hold():
    clock = [1_800_000_000]
    e, ha = _eng(o(away_delay_min=15), two_phones("unavailable", "unavailable"), clock)
    e.evaluate()
    assert e.st["last_result"]["presence_home"] is None
