"""Minimal Home Assistant client over the Supervisor proxy (homeassistant_api: true)."""
import json
import os
import re
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

    # ---- 0.8.0 spare bathroom heater: narrow, separate write paths (never climate.*) ----
    def heater_switch(self, entity_id, on):
        """switch.turn_on / turn_off on the configured heater plug only (caller passes the configured id)."""
        if not re.match(r"^switch\.[a-z0-9_]+$", str(entity_id or "")):
            raise PermissionError("entity not allowed")
        return self._req("POST", "/services/switch/%s" % ("turn_on" if on else "turn_off"), {"entity_id": entity_id})

    def heater_notify(self, title, message, service=""):
        self._req("POST", "/services/persistent_notification/create",
                  {"title": title, "message": message, "notification_id": "smart_hvac_spare_bathroom"})
        if re.match(r"^notify\.[a-z0-9_]+$", str(service or "")):
            self._req("POST", "/services/notify/%s" % service.split(".", 1)[1], {"title": title, "message": message})

    def publish_state(self, entity_id, state, attrs):
        """Mirror state for climate.spare_bathroom only (read by the card and the watchdog automation)."""
        if entity_id != "climate.spare_bathroom":
            raise PermissionError("entity not allowed")
        return self._req("POST", "/states/%s" % entity_id, {"state": state, "attributes": attrs})
