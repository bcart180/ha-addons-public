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


def _vars(at="14:00", caller="+15555550130"):
    v = _bp()["variables"]
    ctx = {"quiet_start": "00:00:00", "quiet_end": "06:00:00", "open_path": "/47b3c4e5_voicemail",
           "voicemail_id": "vm555", "caller": caller, "newest": {"short_id": "AB12"},
           "context": {"id": "01CTX555"}}
    for key in ("can_call", "quiet", "push_tag", "action_heard", "action_block", "action_undo"):
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
    # Actions reach add-ons only through HA (event bus / Supervisor stdin), never add-on URLs.
    assert "http://" not in text and "rest_command" not in text and "shell_command" not in text
    assert "callcatch_command" in text and "hassio.addon_stdin" in text


def test_dry_run_stops_before_any_push():
    actions = _bp()["actions"]
    first = actions[0]
    assert "dry_run" in first["if"][0]["value_template"]
    assert [step.get("action") or ("stop" if "stop" in step else None) for step in first["then"]] == ["logbook.log", "stop"]


def test_action_ids_unique_per_notification_and_buttons():
    ctx = _vars()
    assert ctx["action_heard"] == "ACTION_01CTX555_HEARD"
    assert ctx["action_block"] == "ACTION_01CTX555_BLOCK"
    acts = ctx["push_data"]["actions"]
    assert [a["title"] for a in acts] == ["Call back", "Mark heard", "Block", "Open CallCatch"]
    assert acts[0] == {"action": "URI", "title": "Call back", "uri": "tel:+15555550130"}
    assert acts[3]["uri"] == "/47b3c4e5_voicemail"
    assert ctx["push_data"]["push"]["interruption-level"] == "active"
    # Same tag / collapse id as the pyscript push, so any overlap replaces in place.
    assert ctx["push_data"]["tag"] == "voicemail_AB12"
    assert ctx["push_data"]["apns_headers"] == {"apns-collapse-id": "voicemail_AB12"}
    # Withheld numbers get no Call back / Block button.
    anon = _vars(caller="anonymous")
    assert [a["title"] for a in anon["push_data"]["actions"]] == ["Mark heard", "Open CallCatch"]


def test_quiet_hours_are_silent():
    for at, quiet in (("00:00", True), ("03:30", True), ("05:59", True), ("06:00", False), ("23:59", False)):
        ctx = _vars(at=at)
        assert ctx["quiet"] is quiet, at
        if quiet:
            assert ctx["push_data"]["push"] == {"thread-id": "voicemail", "interruption-level": "passive", "sound": "none"}
            assert ctx["push_data"]["importance"] == "low"


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
    assert first_push == 2
