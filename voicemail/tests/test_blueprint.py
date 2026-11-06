"""0.3.1 notification blueprint: structure, unique action ids, dry_run default, quiet hours."""
from __future__ import annotations

import ast
import datetime as dt
import pathlib
import re

import jinja2
import yaml

BP = pathlib.Path(__file__).resolve().parents[1] / "blueprints" / "callcatch_new_voicemail.yaml"


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_constructor("!input", lambda loader, node: {"!input": loader.construct_scalar(node)})


def _bp():
    return yaml.load(BP.read_text(encoding="utf-8"), Loader=_Loader)


def _render(template: str, at: str, **vars):
    env = jinja2.Environment()
    env.filters["regex_match"] = lambda value, pat: bool(re.match(pat, str(value)))
    hh, mm = (int(x) for x in at.split(":"))
    env.globals["now"] = lambda: dt.datetime(2026, 10, 30, hh, mm)
    out = env.from_string(template).render(**vars).strip()
    try:
        return ast.literal_eval(out)
    except (ValueError, SyntaxError):
        return out


def _vars(at="14:00", caller_masked="•••0130", known_contact=False):
    v = _bp()["variables"]
    newest = {"short_id": "AB12", "caller_masked": caller_masked, "known_contact": known_contact}
    ctx = {"quiet_start": "00:00:00", "quiet_end": "06:00:00", "open_path": "/47b3c4e5_voicemail",
           "voicemail_id": "vm555", "newest": newest, "context": {"id": "01CTX555"}}
    for key in ("caller_masked", "known_contact", "can_queue", "quiet", "push_tag", "action_heard", "action_callback"):
        ctx[key] = _render(v[key], at, **ctx)
    ctx["push_data"] = _render(v["push_data"], at, **ctx)
    return ctx


def test_inputs_defaults_are_safe():
    bp = _bp()
    inputs = bp["blueprint"]["input"]
    assert inputs["dry_run"]["default"] is True
    assert inputs["voice_addon"]["default"] == "47b3c4e5_twilio_house_voice"
    assert inputs["open_path"]["default"] == "/47b3c4e5_voicemail"
    assert inputs["quiet_start"]["default"] == "00:00:00" and inputs["quiet_end"]["default"] == "06:00:00"
    text = BP.read_text(encoding="utf-8")
    # The push reaches CallCatch only through the HA event bus. It does not dial.
    assert "http://" not in text and "rest_command" not in text and "shell_command" not in text
    assert "tel:" not in text and "hassio.addon_stdin" not in text
    assert re.search(r"newest\.caller(?!_)", text) is None
    assert "callcatch_command" in text and "request_callback" in text


def test_dry_run_stops_before_any_push():
    actions = _bp()["actions"]
    first = actions[0]
    assert "dry_run" in first["if"][0]["value_template"]
    assert [step.get("action") or ("stop" if "stop" in step else None) for step in first["then"]] == ["logbook.log", "stop"]


def test_action_ids_unique_per_notification_and_buttons():
    ctx = _vars()
    assert ctx["can_queue"] is True
    assert ctx["action_heard"] == "ACTION_01CTX555_HEARD"
    assert ctx["action_callback"] == "ACTION_01CTX555_CALLBACK"
    acts = ctx["push_data"]["actions"]
    assert [a["title"] for a in acts] == ["Call back", "Mark heard", "Open CallCatch"]
    assert acts[0] == {"action": "ACTION_01CTX555_CALLBACK", "title": "Call back"}
    assert "uri" not in acts[0]
    assert acts[2]["uri"] == "/47b3c4e5_voicemail"
    assert ctx["push_data"]["push"]["interruption-level"] == "active"
    # Same tag / collapse id as the pyscript push, so any overlap replaces in place.
    assert ctx["push_data"]["tag"] == "voicemail_AB12"
    assert ctx["push_data"]["apns_headers"] == {"apns-collapse-id": "voicemail_AB12"}
    # A saved contact with no masked digits still gets the queue button.
    named = _vars(caller_masked="", known_contact=True)
    assert [a["title"] for a in named["push_data"]["actions"]] == ["Call back", "Mark heard", "Open CallCatch"]
    # Withheld callers get no Call back button. The push never dials.
    anon = _vars(caller_masked="", known_contact=False)
    assert [a["title"] for a in anon["push_data"]["actions"]] == ["Mark heard", "Open CallCatch"]
    raw = _vars(caller_masked="+15555550130", known_contact=False)
    assert [a["title"] for a in raw["push_data"]["actions"]] == ["Mark heard", "Open CallCatch"]


def test_quiet_hours_are_silent():
    for at, quiet in (("00:00", True), ("03:30", True), ("05:59", True), ("06:00", False), ("23:59", False)):
        ctx = _vars(at=at)
        assert ctx["quiet"] is quiet, at
        if quiet:
            assert ctx["push_data"]["push"] == {"thread-id": "voicemail", "interruption-level": "passive", "sound": "none"}
            assert ctx["push_data"]["importance"] == "low"


def test_quiet_hours_release_only_urgent_known_contacts():
    """Midnight–06:00 ET holds everyone except an urgent call from a known contact."""
    bp = _bp()
    text = BP.read_text(encoding="utf-8")
    assert 'default: "00:00:00"' in text and 'default: "06:00:00"' in text
    held = _render(bp["variables"]["quiet"], "03:30", quiet_start="00:00:00", quiet_end="06:00:00", urgent_known=False)
    released = _render(bp["variables"]["quiet"], "03:30", quiet_start="00:00:00", quiet_end="06:00:00", urgent_known=True)
    daytime = _render(bp["variables"]["quiet"], "14:00", quiet_start="00:00:00", quiet_end="06:00:00", urgent_known=False)
    assert held is True and released is False and daytime is False


def test_single_push_owner_guard_before_any_push():
    bp = _bp()
    v = bp["variables"]
    assert bp["blueprint"]["input"]["pipeline_sensor"]["default"] == "sensor.voicemail_pipeline"
    env = {"pipeline_sensor": "sensor.voicemail_pipeline"}
    for attr, owns in ((None, False), ("", False), ("pyscript", False), ("blueprint", True)):
        push_owner = _render(v["push_owner"].replace("state_attr(pipeline_sensor, 'push_owner')", "attr"), "12:00",
                             attr=attr, **env)
        push_owner = "" if push_owner in (None, "None") else push_owner
        assert _render(v["owns_push"], "12:00", push_owner=push_owner) is owns, attr
    actions = bp["actions"]
    guard = actions[1]
    assert "not owns_push" in guard["if"][0]["value_template"]
    assert [("stop" if "stop" in st else st.get("action")) for st in guard["then"]] == ["logbook.log", "stop"]
    # The guard sits before the first push; nothing before it can notify.
    first_push = next(i for i, st in enumerate(actions) if st.get("type") == "notify")
    assert first_push == 3
    silent = actions[2]
    assert "alert_silent" in silent["if"][0]["value_template"]
    assert [("stop" if "stop" in st else st.get("action")) for st in silent["then"]] == ["logbook.log", "stop"]
