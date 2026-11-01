"""Minimal Home Assistant client over the Supervisor proxy (homeassistant_api: true)."""
import json
import os
import urllib.parse
import urllib.request

EXCLUDED_PREFIXES = ("climate.p3arl2_",)  # Tesla climate: never touched (MA L3)


def excluded(entity_id):
    return str(entity_id or "").startswith(EXCLUDED_PREFIXES)


# Explicit allowlist. set_fan_mode added in 0.3.0 for the thermostat cards (fan control).
# On/off uses set_hvac_mode (off / heat|cool), so climate.turn_on/turn_off are NOT allowed.
ALLOWED_SERVICES = ("set_hvac_mode", "set_temperature", "set_fan_mode")

BASE = os.environ.get("SH_HA_URL", "http://supervisor/core/api")


class HAClient:
    def __init__(self, token=None, base=BASE, timeout=10):
        self.token = token or os.environ.get("SUPERVISOR_TOKEN", "")
        self.base = base.rstrip("/")
        self.timeout = timeout

    def _req(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read() or b"null")

    def get_states(self):
        return self._req("GET", "/states")

    def get_history(self, entity_ids, start_iso):
        """Read-only HA history (GET /history/period). No writes possible through this path."""
        q = urllib.parse.urlencode({"filter_entity_id": ",".join(entity_ids), "significant_changes_only": "0"})
        return self._req("GET", "/history/period/%s?%s" % (urllib.parse.quote(start_iso), q))

    def get_forecast(self, weather_entity, kind="hourly"):
        """0.6.4 read-only: weather.get_forecasts with return_response. Only this one read service is allowed
        here; it changes nothing in HA. Returns the forecast list or []."""
        if not str(weather_entity or "").startswith("weather."):
            raise PermissionError("entity not allowed")
        r = self._req("POST", "/services/weather/get_forecasts?return_response",
                      {"entity_id": weather_entity, "type": kind})
        resp = (r or {}).get("service_response", r) or {}
        return list((resp.get(weather_entity) or {}).get("forecast") or [])

    def call(self, domain, service, data):
        if domain != "climate" or service not in ALLOWED_SERVICES:
            raise PermissionError("service not allowed: %s.%s" % (domain, service))
        if not str(data.get("entity_id", "")).startswith("climate.") or excluded(data.get("entity_id")):
            raise PermissionError("entity not allowed")
        return self._req("POST", "/services/%s/%s" % (domain, service), data)
