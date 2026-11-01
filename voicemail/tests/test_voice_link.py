"""CallCatch <-> Voice link: schema pairing and the redacted wiring planner."""
from __future__ import annotations

import importlib.util
import pathlib

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
CC = yaml.safe_load((ROOT / "voicemail/config.yaml").read_text())
VO = yaml.safe_load((ROOT / "twilio_house_voice/config.yaml").read_text())
spec = importlib.util.spec_from_file_location("wire", ROOT / "voicemail/tools/wire_voice_link.py")
wire = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wire)


@pytest.mark.parametrize("src,dst", wire.PAIRS)
def test_pairs_are_password_and_blank(src, dst):
    assert VO["schema"][src] == "password" and CC["schema"][dst] == "password"
    assert VO["options"][src] == "" and CC["options"][dst] == ""


def test_trust_url_shape():
    assert CC["options"]["voice_trust_url"] == ""
    assert wire.trust_url("abc123-twilio-house-voice") == "http://abc123-twilio-house-voice:8787"
    for bad in ("example.com", "", "h:1", "a/b"):
        with pytest.raises(SystemExit):
            wire.trust_url(bad)


def test_plan_copies_and_never_reports_values():
    v = {"ha_webhook_token": "S1-not-real", "trust_api_token": "S2-not-real"}
    new, rep = wire.plan(v, {"ingest_token": "", "voice_trust_token": "S2-not-real"}, "http://h:8787")
    assert new["ingest_token"] == "S1-not-real" and new["voice_trust_token"] == "S2-not-real"
    assert new["voice_trust_url"] == "http://h:8787"
    text = "\n".join(rep)
    assert "S1" not in text and "S2" not in text
    assert "ha_webhook_token->ingest_token: differs" in text
    assert "trust_api_token->voice_trust_token: match" in text


def test_plan_refuses_to_mint():
    with pytest.raises(SystemExit) as e:
        wire.plan({"ha_webhook_token": "x"}, {}, "http://h:8787")
    assert "trust_api_token" in str(e.value)


def test_slug_lookup():
    addons = [{"slug": "abc123_twilio_house_voice"}, {"slug": "abc123_voicemail"}]
    assert wire.find_slug(addons, "voicemail") == "abc123_voicemail"
    with pytest.raises(SystemExit):
        wire.find_slug(addons + [{"slug": "zz_voicemail"}], "voicemail")
