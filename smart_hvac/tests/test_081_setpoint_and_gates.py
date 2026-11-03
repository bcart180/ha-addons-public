"""0.8.1: setpoint write failure is contained; HTTPError logs code+path only; heater-only gates."""
import io
import logging
import os
import sys
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rootfs", "app"))
sys.path.insert(0, os.path.dirname(__file__))
import pytest  # noqa: E402

import ha_client  # noqa: E402
import test_080_spare_heater as T80  # noqa: E402
import test_engine as TE  # noqa: E402


class FailSetpointHA(TE.FakeHA):
    def __init__(self, states, fail_eid):
        super().__init__(states)
        self.fail_eid = fail_eid

    def call(self, domain, service, data):
        if service == "set_temperature" and data["entity_id"] == self.fail_eid:
            self.calls.append(("FAILED", data))
            raise urllib.error.HTTPError("http://x/api/services/climate/set_temperature", 500, "err", {}, None)
        return super().call(domain, service, data)


def test_setpoint_failure_marks_zone_and_cycle_finishes(tmp_path):
    from engine import Engine
    ha = FailSetpointHA(TE.states(), "climate.lr")
    path = str(tmp_path / "state.json")
    e = Engine(TE.opts(), ha, path, clock=lambda: TE.NOW, localtime=TE.lt14)
    r = e.evaluate()  # must not raise
    assert r["lr"]["reason"] == "setpoint_write_failed" and r["lr"]["applied"] is False
    assert r["bath"]["applied"] is True  # other zone still written
    assert ("set_temperature", {"entity_id": "climate.bath", "temperature": 72.0}) in ha.calls
    assert e.st["last_result"]["zones"]["lr"]["reason"] == "setpoint_write_failed"
    assert os.path.exists(path)  # state saved


def test_setpoint_failure_retried_next_cycle(tmp_path):
    from engine import Engine
    ha = FailSetpointHA(TE.states(), "climate.lr")
    e = Engine(TE.opts(), ha, None, clock=lambda: TE.NOW, localtime=TE.lt14)
    e.evaluate()
    ha.fail_eid = None
    r = e.evaluate()
    assert ("set_temperature", {"entity_id": "climate.lr", "temperature": 71.0}) in ha.calls
    assert r["lr"]["setpoint"] == 71.0


def test_http_error_logs_code_and_path_only(monkeypatch, caplog):
    secret_body = b"SECRET-BODY-555"

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {"X-Leak": "hdr555"}, io.BytesIO(secret_body))
    monkeypatch.setattr(ha_client.urllib.request, "urlopen", boom)
    c = ha_client.HAClient(token="tok555", base="http://h/api")
    with caplog.at_level(logging.WARNING, logger="smart_hvac.ha"):
        with pytest.raises(urllib.error.HTTPError):
            c.get_history(["sensor.a"], "2026-01-01T00:00:00")
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "401" in msg and "/history/period/" in msg
    assert "SECRET" not in msg and "hdr555" not in msg and "tok555" not in msg and "filter_entity_id" not in msg


@pytest.mark.parametrize("glob_en,glob_dry,sb_en,sb_dry,ok", [
    (True, False, True, False, True),
    (True, False, False, False, False),   # heater disabled
    (True, False, True, True, False),     # heater dry_run
    (True, True, True, False, False),     # global dry_run
    (False, False, True, False, False),   # global off
    (True, False, None, None, False),     # heater keys missing -> defaults (off, dry)
])
def test_heater_gates(glob_en, glob_dry, sb_en, sb_dry, ok):
    h, ha, c = T80.mk(temp=50, live=True)
    h.opts["enabled"], h.opts["dry_run"] = glob_en, glob_dry
    sb = h.opts["spare_bathroom"]
    for k, v in (("enabled", sb_en), ("dry_run", sb_dry)):
        if v is None:
            sb.pop(k, None)
        else:
            sb[k] = v
    T80.run(h, c, 600)
    assert (True in ha.cmds) is ok


def test_heater_needs_ids_even_when_all_gates_open():
    h, ha, c = T80.mk(temp=50, live=True)
    h.configured = False
    assert h.can_heat is False


def test_config_defaults_heater_gates_safe():
    import yaml
    cfg = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "..", "config.yaml")))
    sb = cfg["options"]["spare_bathroom"]
    assert sb["enabled"] is False and sb["dry_run"] is True
    assert cfg["options"]["enabled"] is False and cfg["options"]["dry_run"] is True
