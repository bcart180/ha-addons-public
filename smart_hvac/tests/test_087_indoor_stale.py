"""0.8.7 indoor_stale_min: a frozen room sensor falls back to the unit's current_temperature."""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rootfs", "app"))
from engine import Engine  # noqa: E402

NOW = 1_800_000_000.0
Z = {"name": "lr", "indoor": "sensor.lr_t"}


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def e():
    return Engine.__new__(Engine)


def test_off_by_default():
    s = {"sensor.lr_t": {"state": "66", "last_reported": iso(NOW - 99999)}}
    assert e()._indoor_stale(s, Z, NOW) is False
    assert e()._indoor_stale(s, dict(Z, indoor_stale_min=0), NOW) is False


def test_stale_by_last_reported():
    s = {"sensor.lr_t": {"state": "66", "last_reported": iso(NOW - 61 * 60), "last_updated": iso(NOW)}}
    assert e()._indoor_stale(s, dict(Z, indoor_stale_min=60), NOW) is True


def test_fresh_last_reported_wins_over_old_last_updated():
    s = {"sensor.lr_t": {"state": "66", "last_reported": iso(NOW - 60), "last_updated": iso(NOW - 99999)}}
    assert e()._indoor_stale(s, dict(Z, indoor_stale_min=60), NOW) is False


def test_falls_back_to_last_updated_and_no_timestamp_is_not_stale():
    assert e()._indoor_stale({"sensor.lr_t": {"state": "66", "last_updated": iso(NOW - 7200)}},
                             dict(Z, indoor_stale_min=60), NOW) is True
    assert e()._indoor_stale({"sensor.lr_t": {"state": "66"}}, dict(Z, indoor_stale_min=60), NOW) is False
    assert e()._indoor_stale({}, dict(Z, indoor_stale_min=60), NOW) is False
    assert e()._indoor_stale({}, {"name": "x", "indoor_stale_min": 60}, NOW) is False


def test_bad_option_value_is_off():
    s = {"sensor.lr_t": {"state": "66", "last_reported": iso(NOW - 99999)}}
    assert e()._indoor_stale(s, dict(Z, indoor_stale_min="x"), NOW) is False
