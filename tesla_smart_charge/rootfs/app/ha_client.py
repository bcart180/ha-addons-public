"""Minimal Home Assistant REST client over the Supervisor proxy (homeassistant_api: true).
Writes are allow-listed to the services this add-on needs AND to entity ids the user configured."""
import json
import os
import urllib.request

BASE = os.environ.get("TSC_HA_URL", "http://supervisor/core/api")

ALLOWED = {
    ("switch", "turn_on"), ("switch", "turn_off"),
    ("number", "set_value"),
    ("button", "press"),
    ("input_boolean", "turn_off"),
}


class HAClient:
    def __init__(self, allowed_entities, notify_service="", status_entity="", token=None, base=BASE, timeout=15):
        self.token = token or os.environ.get("SUPERVISOR_TOKEN", "")
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.allowed_entities = {e for e in allowed_entities if e}
        self.notify_service = notify_service or ""
        self.status_entity = status_entity or ""

    def _req(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read() or b"null")

    def get_states(self):
        return {s["entity_id"]: s for s in (self._req("GET", "/states") or [])}

    def call(self, domain, service, data):
        if (domain, service) not in ALLOWED:
            raise PermissionError("service not allowed: %s.%s" % (domain, service))
        eid = str(data.get("entity_id", ""))
        if not eid.startswith(domain + ".") or eid not in self.allowed_entities:
            raise PermissionError("entity not allowed: %s" % eid)
        return self._req("POST", "/services/%s/%s" % (domain, service), data)

    def notify(self, title, message):
        if not self.notify_service:
            return None
        svc = self.notify_service.split(".", 1)[1]
        return self._req("POST", "/services/notify/%s" % svc, {"title": title, "message": message})

    def set_status(self, state, attrs):
        if not self.status_entity:
            return None
        return self._req("POST", "/states/%s" % self.status_entity, {"state": state, "attributes": attrs})
