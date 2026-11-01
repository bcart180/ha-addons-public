"""0.6.4 forecast anticipation: hourly forecast (read-only) feeds the blend, the season guard and the warm-up."""
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rootfs", "app"))
sys.path.insert(0, os.path.dirname(__file__))
import pytest  # noqa: E402

import core  # noqa: E402
import defrost_scenario  # noqa: E402
from engine import Engine  # noqa: E402
from ha_client import HAClient  # noqa: E402
from test_060_mode_lock import StampedHA, opts, st  # noqa: E402
from test_engine import lt14  # noqa: E402

NOW = 1_800_000_000


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def fc(temps, start=NOW):
    return [{"datetime": iso(start + i * 3600), "temperature": t} for i, t in enumerate(temps)]


class FcHA(StampedHA):
    def __init__(self, s, forecast, fail=False):
        super().__init__(s)
        self.fc, self.fail, self.n = forecast, fail, 0

    def get_forecast(self, entity, kind="hourly"):
        self.n += 1
        if self.fail:
            raise OSError("down")
        return self.fc


def test_summary():
    f = fc([50, 48, 46, 44, 42, 40, 38, 60, 61, 62, 63, 64, 70])
    s = core.forecast_summary(f, NOW, NOW + 5 * 3600)
    assert s == {"mean": 44.0, "high": 70, "at_wake": 40}
    assert core.forecast_summary([], NOW) == {"mean": None, "high": None, "at_wake": None}
    assert core.forecast_summary([{"datetime": "x", "temperature": 3}, 5, {}], NOW)["mean"] is None
    assert core.forecast_summary(f, NOW, NOW + 30 * 3600)["at_wake"] is None


def test_forecast_high_lifts_the_season_guard_only_when_given():
    base = {"enabled": True, "manual_profile": "home", "bryson_home": True, "minute_of_day": 840, "now_ts": NOW,
            "outdoor_temp": 70, "indoor_temp": 76, "unit_available": True, "last_mode": "heat",
            "last_setpoint": 71, "last_mode_change_ts": 0}
    assert core.decide(base, {}, None)["mode"] == "heat"            # 70 out < 75 lockout -> coast in heat
    assert core.decide(dict(base, forecast_high=82), {}, None)["mode"] == "cool"


def test_option_off_is_byte_identical():
    golden = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "defrost_off_golden.json")))
    assert json.loads(defrost_scenario.run()) == golden


def test_engine_off_never_asks_for_a_forecast():
    ha = FcHA(st(), fc([40] * 24))
    Engine(opts(), ha, None, clock=lambda: NOW, localtime=lt14).evaluate()
    assert ha.n == 0


def test_engine_on_caches_and_survives_failure():
    clock = [NOW]
    ha = FcHA(st(), fc([40] * 24))
    e = Engine(opts(forecast_anticipation=True), ha, None, clock=lambda: clock[0], localtime=lt14)
    e.evaluate()
    e.evaluate()
    assert ha.n == 1
    clock[0] += 1801
    e.evaluate()
    assert ha.n == 2
    bad = FcHA(st(), [], fail=True)
    Engine(opts(forecast_anticipation=True), bad, None, clock=lambda: NOW, localtime=lt14).evaluate()


def test_warm_forecast_never_splits_modes():
    ha = FcHA(st(lr=("heat", 71, 76), br=("heat", 71, 66), outdoor=70), fc([75, 80, 85, 88] + [85] * 20))
    Engine(opts(forecast_anticipation=True), ha, None, clock=lambda: NOW, localtime=lt14).evaluate()
    assert not core.shared_mode_conflict([ha.s["climate.lr"][0], ha.s["climate.br"][0]])


def test_client_forecast_is_read_only_and_weather_only(monkeypatch):
    c = HAClient(token="t", base="http://x")
    seen = []
    monkeypatch.setattr(c, "_req", lambda m, p, b=None: seen.append((m, p, b)) or {
        "service_response": {"weather.w": {"forecast": [{"datetime": "2027-01-01T00:00:00+00:00",
                                                         "temperature": 40}]}}})
    assert c.get_forecast("weather.w")[0]["temperature"] == 40
    assert seen == [("POST", "/services/weather/get_forecasts?return_response",
                     {"entity_id": "weather.w", "type": "hourly"})]
    with pytest.raises(PermissionError):
        c.get_forecast("climate.lr")
    with pytest.raises(PermissionError):
        c.call("weather", "get_forecasts", {"entity_id": "weather.w"})
