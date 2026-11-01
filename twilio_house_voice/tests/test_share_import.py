"""Store migration importer (plan C3/C4/C6). Synthetic 555-01xx data only.

Run: pytest <addon>/tests/test_share_import.py
"""
import json
import logging
import os
import pathlib
import re
import sys

import pytest

APP = pathlib.Path(__file__).resolve().parents[1] / "rootfs" / "app"
sys.path.insert(0, str(APP))

import share_import  # noqa: E402

UID = os.getuid()
ALLOW = {"trust_lists.json": "trust_lists.json", "index.json": "voicemail/index.json"}
E164 = re.compile(r"\+?1?\D?\(?\d{3}\)?\D?\d{3}\D?\d{4}")
TRUST = {
    "allow": [{"number": "+15555550101", "note": "Synthetic friend"}],
    "block": [{"number": "+15555550199", "note": "Synthetic spam"}, {"number": "+15555550142", "note": ""}],
}
INDEX = {"calls": [{"call_id": "CA0001", "from": "+15555550123", "transcript": "Call me at 555-555-0123."}]}


def stage(tmp_path, files, addon="addon", mode=0o700):
    root = tmp_path / "share" / ".migrate-2026-11-01"
    src = root / addon
    src.mkdir(parents=True)
    for name, body in files.items():
        p = src / name
        p.write_bytes(body if isinstance(body, bytes) else json.dumps(body).encode())
    os.chmod(root, mode)
    os.chmod(src, mode)
    data = tmp_path / "data"
    data.mkdir()
    return root, src, data


def run(root, data, allow=ALLOW, **kw):
    return share_import.import_from_share("addon", data, allow, staging_root=root, owner_uid=UID, **kw)


def test_imports_file_to_file_and_deletes_staging(tmp_path, caplog):
    raw = json.dumps(TRUST, indent=3).encode()  # odd indent: must survive byte-exact
    root, src, data = stage(tmp_path, {"trust_lists.json": raw, "index.json": INDEX})
    with caplog.at_level(logging.DEBUG):
        res = run(root, data)
    assert res["status"] == "imported"
    assert res["staging"] == "deleted"
    assert (data / "trust_lists.json").read_bytes() == raw
    assert json.loads((data / "voicemail" / "index.json").read_text()) == INDEX
    assert not src.exists() and not root.exists()
    marker = json.loads((data / share_import.MARKER_NAME).read_text())
    assert {f["file"]: f["entries"] for f in marker["files"]} == {"index.json": 1, "trust_lists.json": 3}
    # Counts only: no numbers or contents in logs, summary or marker (M1).
    blob = caplog.text + json.dumps(res) + json.dumps(marker)
    assert not E164.search(blob)
    assert "Synthetic" not in blob and "CA0001" not in blob


def test_one_shot_and_idempotent(tmp_path):
    root, src, data = stage(tmp_path, {"trust_lists.json": TRUST})
    assert run(root, data)["status"] == "imported"
    (data / "trust_lists.json").write_text('{"allow": [], "block": []}')
    assert run(root, data)["status"] == "already_imported"
    # Staging reappears: flagged, never re-imported, never deleted.
    src.mkdir(parents=True)
    os.chmod(root, 0o700)
    os.chmod(src, 0o700)
    (src / "trust_lists.json").write_text(json.dumps(TRUST))
    assert run(root, data)["status"] == "already_imported_staging_present"
    assert (data / "trust_lists.json").read_text() == '{"allow": [], "block": []}'
    assert (src / "trust_lists.json").exists()


def test_no_staging_is_a_noop(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    res = share_import.import_from_share("addon", data, ALLOW, staging_root=tmp_path / "nope", owner_uid=UID)
    assert res["status"] == "no_staging"
    assert not (data / share_import.MARKER_NAME).exists()


@pytest.mark.parametrize(
    "files,reason",
    [
        ({"options.json": {"x": 1}}, "not_allow_listed"),
        ({"trust_lists.json": TRUST, "secrets.json": {}}, "not_allow_listed"),
        ({"trust_lists.json": b"{not json"}, "invalid_json"),
        ({"trust_lists.json": b"\"just a string\""}, "invalid_json"),
        ({"trust_lists.json": b"\xff\xfe"}, "invalid_json"),
        ({}, "staging_empty"),
    ],
)
def test_refusals_write_nothing(tmp_path, caplog, files, reason):
    root, src, data = stage(tmp_path, files)
    with caplog.at_level(logging.DEBUG):
        res = run(root, data)
    assert res["status"] == "refused" and res["reason"] == reason
    assert list(data.iterdir()) == []
    assert src.exists()  # left for the owner to inspect
    assert "options.json" not in caplog.text and "secrets.json" not in caplog.text


def test_refuses_symlink_file(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(TRUST))
    root, src, data = stage(tmp_path, {})
    (src / "trust_lists.json").symlink_to(outside)
    res = run(root, data)
    assert res["reason"] == "symlink"
    assert list(data.iterdir()) == []


def test_refuses_symlinked_staging_dir(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    (real / "trust_lists.json").write_text(json.dumps(TRUST))
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    (root / "addon").symlink_to(real)
    data = tmp_path / "data"
    data.mkdir()
    assert run(root, data)["reason"] == "staging_symlink"


def test_refuses_directory_entry(tmp_path):
    root, src, data = stage(tmp_path, {})
    (src / "index.json").mkdir()
    assert run(root, data)["reason"] == "not_regular_file"


def test_refuses_oversize(tmp_path):
    root, src, data = stage(tmp_path, {"trust_lists.json": TRUST})
    assert run(root, data, max_bytes=10)["reason"] == "too_large"
    assert list(data.iterdir()) == []


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o701])
def test_refuses_open_permissions(tmp_path, mode):
    root, src, data = stage(tmp_path, {"trust_lists.json": TRUST}, mode=mode)
    assert run(root, data)["reason"] == "staging_mode"


def test_refuses_wrong_owner(tmp_path):
    root, src, data = stage(tmp_path, {"trust_lists.json": TRUST})
    res = share_import.import_from_share("addon", data, ALLOW, staging_root=root, owner_uid=UID + 1)
    assert res["reason"] == "staging_owner"


def test_shared_root_kept_when_other_addon_staged(tmp_path):
    root, src, data = stage(tmp_path, {"trust_lists.json": TRUST})
    other = root / "other"
    other.mkdir(mode=0o700)
    assert run(root, data)["status"] == "imported"
    assert not src.exists() and other.exists()


def test_server_hook_allow_list(tmp_path, monkeypatch):
    os.environ.pop("SUPERVISOR_TOKEN", None)
    import server  # noqa: PLC0415

    assert server.VERSION == "0.2.7"
    assert server.SHARE_IMPORT_ALLOW == {"trust_lists.json": "trust_lists.json", "index.json": "voicemail/index.json"}
    root = tmp_path / "stage"
    src = root / "twilio_house_voice"
    src.mkdir(parents=True)
    os.chmod(root, 0o700)
    os.chmod(src, 0o700)
    (src / "trust_lists.json").write_text(json.dumps(TRUST))
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(server.share_import, "STAGING_ROOT", root)
    real = server.share_import.import_from_share
    monkeypatch.setattr(server.share_import, "import_from_share", lambda *a, **k: real(*a, owner_uid=UID, **k))
    assert server.run_share_import(data)["status"] == "imported"
    # The bridge's own loader reads the imported lists.
    app = server.create_app(options={}, data_dir=data)
    assert app["trust"] is not None
    assert (data / "trust_lists.json").read_text() == json.dumps(TRUST)
