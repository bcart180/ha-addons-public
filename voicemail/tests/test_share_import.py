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


def test_empty_staging_is_info(tmp_path, caplog):
    root, src, data = stage(tmp_path, {})
    with caplog.at_level(logging.DEBUG):
        res = run(root, data)
    assert res == {"status": "staging_empty"}
    assert list(data.iterdir()) == []
    assert not (data / share_import.MARKER_NAME).exists()
    assert src.exists()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    infos = [r for r in caplog.records if r.levelno == logging.INFO and "staging folder is empty" in r.getMessage()]
    assert len(infos) == 1


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


def test_server_hook_allow_list_and_diag_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("VM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VM_MOCK", "1")
    import server  # noqa: PLC0415

    assert server.VERSION == "0.6.0"
    assert set(server.SHARE_IMPORT_ALLOW) == {"inbox.json", "legacy_import.json", "drive_queue.json"}
    assert "options.json" not in server.SHARE_IMPORT_ALLOW
    root = tmp_path / "stage"
    src = root / "voicemail"
    src.mkdir(parents=True)
    os.chmod(root, 0o700)
    os.chmod(src, 0o700)
    (src / "inbox.json").write_text(json.dumps({"items": {"vm1": {"caller": "+15555550111"}}}))
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(share_import, "STAGING_ROOT", root)
    monkeypatch.setattr(server.share_import, "STAGING_ROOT", root)
    real = server.share_import.import_from_share
    monkeypatch.setattr(server.share_import, "import_from_share", lambda *a, **k: real(*a, owner_uid=UID, **k))
    res = server.run_share_import()
    assert res["status"] == "imported"
    assert server.SHARE_IMPORT["status"] == "imported"
    assert (tmp_path / "data" / "inbox.json").exists()
    assert not src.exists()


# --- Security Audit L1-L4 hardening (2026-11-01) ---


def _swap_after_lstat(monkeypatch, target_name, action):
    real = os.lstat
    fired = []

    def fake(path, *a, **kw):
        res = real(path, *a, **kw)
        if not fired and os.fspath(path) == target_name and kw.get("dir_fd") is not None:
            fired.append(1)
            action(kw["dir_fd"])
        return res

    monkeypatch.setattr(share_import.os, "lstat", fake)


def test_l1_fifo_swapped_in_after_lstat_is_refused_not_hung(tmp_path, monkeypatch):
    import threading

    root, src, data = stage(tmp_path, {"trust_lists.json": TRUST})

    def swap(_dir_fd):
        os.unlink(src / "trust_lists.json")
        os.mkfifo(src / "trust_lists.json")

    _swap_after_lstat(monkeypatch, "trust_lists.json", swap)
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("r", run(root, data)), daemon=True)
    th.start()
    th.join(5)
    assert not th.is_alive(), "importer blocked on a FIFO"
    assert out["r"]["status"] == "refused"
    assert out["r"]["reason"] in ("not_regular_file", "file_changed")
    assert list(data.iterdir()) == []


def test_l1_regular_file_swapped_after_lstat_is_refused(tmp_path, monkeypatch):
    root, src, data = stage(tmp_path, {"trust_lists.json": TRUST})

    def swap(_dir_fd):
        (src / "other.tmp").write_bytes(json.dumps(TRUST).encode())  # new inode
        os.replace(src / "other.tmp", src / "trust_lists.json")

    _swap_after_lstat(monkeypatch, "trust_lists.json", swap)
    res = run(root, data)
    assert res["status"] == "refused" and res["reason"] == "file_changed"


def test_l1_plain_fifo_refused(tmp_path):
    root, src, data = stage(tmp_path, {})
    os.mkfifo(src / "trust_lists.json")
    assert run(root, data)["reason"] == "not_regular_file"


def test_l2_symlinked_staging_dir_refused(tmp_path):
    root, src, data = stage(tmp_path, {"trust_lists.json": TRUST})
    real = tmp_path / "elsewhere"
    os.rename(src, real)
    os.symlink(real, src)
    res = run(root, data)
    assert res["status"] == "refused" and res["reason"] == "staging_symlink"
    assert (real / "trust_lists.json").exists()


def test_l2_cleanup_cannot_be_steered_by_swapped_symlink(tmp_path, monkeypatch):
    root, src, data = stage(tmp_path, {"trust_lists.json": TRUST})
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "trust_lists.json").write_bytes(b'{"v": 1}')
    moved = tmp_path / "moved"
    real_commit = share_import._commit_all
    done = []

    def commit(pairs):
        real_commit(pairs)
        if not done:
            done.append(1)
            os.rename(src, moved)
            os.symlink(victim, src)

    monkeypatch.setattr(share_import, "_commit_all", commit)
    res = run(root, data)
    assert res["status"] == "imported"
    assert (victim / "trust_lists.json").read_bytes() == b'{"v": 1}'
    assert (data / "trust_lists.json").exists()
    assert not (moved / "trust_lists.json").exists()  # the real staged file was the one removed
    assert res["staging"] == "flagged"


@pytest.mark.parametrize(
    "body",
    [b"[" * 200000 + b"]" * 200000, b'{"a": ' + b"1" * 5_000_000 + b"}"],
    ids=["deep_nesting", "huge_int"],
)
def test_l3_json_resource_errors_are_invalid_json(tmp_path, body):
    root, src, data = stage(tmp_path, {"trust_lists.json": body})
    res = run(root, data)
    assert res["status"] == "refused" and res["reason"] == "invalid_json"
    assert list(data.iterdir()) == []
    assert (src / "trust_lists.json").exists()


def test_l4_existing_destination_refuses_whole_import(tmp_path):
    root, src, data = stage(tmp_path, {"trust_lists.json": TRUST, "index.json": INDEX})
    (data / "voicemail").mkdir()
    (data / "voicemail" / "index.json").write_bytes(b'{"existing": true}')
    res = run(root, data)
    assert res["status"] == "refused" and res["reason"] == "dest_exists" and res["file"] == "index.json"
    assert (data / "voicemail" / "index.json").read_bytes() == b'{"existing": true}'
    assert not (data / "trust_lists.json").exists()
    assert not (data / share_import.MARKER_NAME).exists()
    assert sorted(p.name for p in src.iterdir()) == ["index.json", "trust_lists.json"]


def test_l4_partial_write_failure_rolls_back(tmp_path, monkeypatch):
    root, src, data = stage(tmp_path, {"trust_lists.json": TRUST, "index.json": INDEX})
    real = share_import._write_temp
    calls = []

    def flaky(dest, body):
        calls.append(dest.name)
        if len(calls) == 2:
            raise OSError(28, "No space left on device")
        return real(dest, body)

    monkeypatch.setattr(share_import, "_write_temp", flaky)
    res = run(root, data)
    assert res == {"status": "error", "reason": "write_failed", "file": ""}
    leftovers = [p.relative_to(data).as_posix() for p in data.rglob("*") if p.is_file()]
    assert leftovers == []
    assert sorted(p.name for p in src.iterdir()) == ["index.json", "trust_lists.json"]


def test_l4_rename_failure_rolls_back_renamed(tmp_path, monkeypatch):
    root, src, data = stage(tmp_path, {"trust_lists.json": TRUST, "index.json": INDEX})
    real = os.rename
    calls = []

    def flaky(a, b, *x, **k):
        calls.append(1)
        if len(calls) == 2:
            raise OSError(5, "I/O error")
        return real(a, b, *x, **k)

    monkeypatch.setattr(share_import.os, "rename", flaky)
    res = run(root, data)
    assert res["status"] == "error"
    assert [p for p in data.rglob("*") if p.is_file()] == []
    assert src.exists()


def test_l4_marker_failure_rolls_back_imports(tmp_path, monkeypatch):
    root, src, data = stage(tmp_path, {"trust_lists.json": TRUST})
    real = share_import._write_temp

    def flaky(dest, body):
        if dest.name == share_import.MARKER_NAME:
            raise OSError(28, "No space left on device")
        return real(dest, body)

    monkeypatch.setattr(share_import, "_write_temp", flaky)
    assert run(root, data)["status"] == "error"
    assert [p for p in data.rglob("*") if p.is_file()] == []
    assert src.exists()


def test_hardlinked_staged_file_refused(tmp_path):
    root, src, data = stage(tmp_path, {"index.json": INDEX})
    outside = tmp_path / "outside.json"
    outside.write_bytes(json.dumps(TRUST).encode())
    os.link(outside, src / "trust_lists.json")
    res = run(root, data)
    assert res["status"] == "refused" and res["reason"] == "hardlinked"
    assert list(data.iterdir()) == []
    assert outside.exists() and sorted(p.name for p in src.iterdir()) == ["index.json", "trust_lists.json"]
