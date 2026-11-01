"""One-shot import of add-on /data files from a /share staging folder.

Used once, to move an add-on from one store to another: the old add-on's
allow-listed /data files are copied (by the owner, with the old add-on
stopped) into ``/share/.migrate-2026-11-01/<addon>/``, and the new add-on
imports them on its first start.

Rules (store migration plan, C3/C4/C6):

* Reads only ``<staging_root>/<addon>/``. The staging root and the add-on
  folder must be real directories (not symlinks), owned by the expected uid
  (root) and mode 700 (no group/other bits).
* Every entry must be on the allow-list, a regular file (symlinks refused),
  at most ``max_bytes`` and valid JSON. Any refusal aborts the whole import
  before anything is written (fail closed); the staging folder is left for
  the owner to inspect and nothing is marked done.
* Files are moved file-to-file: the validated bytes are written as-is
  (temp file + fsync + rename). Nothing is re-serialized, printed or logged.
* One-shot and idempotent: success writes a marker in /data. With the marker
  present the importer never imports again; a staging folder that is still
  there is only flagged (``status: already_imported_staging_present``).
* On success the staging source files and the add-on's staging folder are
  deleted (the shared staging root is removed only when it is empty).
* Logs and the returned summary carry only allow-listed file names, byte
  sizes, entry counts and reason codes. Never file contents or numbers.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import pathlib
import stat

STAGING_ROOT = pathlib.Path("/share/.migrate-2026-11-01")
MARKER_NAME = ".import_from_share.json"
DEFAULT_MAX_BYTES = 8 * 1024 * 1024

log = logging.getLogger("import_from_share")


class ImportRefused(Exception):
    """A staging problem. ``code`` is a short reason code, safe to log."""

    def __init__(self, code: str, name: str = ""):
        super().__init__(code)
        self.code = code
        self.name = name


def _count(doc) -> int:
    """Entry count only (list length, or the sum of a dict's list values)."""
    if isinstance(doc, list):
        return len(doc)
    if isinstance(doc, dict):
        total = 0
        for value in doc.values():
            if isinstance(value, (list, dict)):
                total += len(value)
        return total
    return 0


def _check_dir(path: pathlib.Path, owner_uid: int) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise ImportRefused("staging_missing") from None
    if stat.S_ISLNK(st.st_mode):
        raise ImportRefused("staging_symlink")
    if not stat.S_ISDIR(st.st_mode):
        raise ImportRefused("staging_not_dir")
    if st.st_uid != owner_uid:
        raise ImportRefused("staging_owner")
    if st.st_mode & 0o077:
        raise ImportRefused("staging_mode")


def _write_atomic(dest: pathlib.Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".import.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _summary(status: str, **extra) -> dict:
    out = {"status": status}
    out.update(extra)
    return out


def import_from_share(
    addon: str,
    data_root: pathlib.Path,
    allow: dict[str, str],
    *,
    staging_root: pathlib.Path | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    owner_uid: int = 0,
) -> dict:
    """Import allow-listed JSON files once. Returns a log-safe summary.

    ``allow`` maps a staging file name to its destination, relative to
    ``data_root`` (for example ``{"inbox.json": "callcatch/inbox.json"}``).
    """
    data_root = pathlib.Path(data_root)
    staging_root = pathlib.Path(staging_root if staging_root is not None else STAGING_ROOT)
    src_dir = staging_root / addon
    marker = data_root / MARKER_NAME

    if marker.exists():
        if os.path.lexists(src_dir):
            log.warning("import_from_share: already imported; staging folder still present, not re-imported (delete it)")
            return _summary("already_imported_staging_present")
        return _summary("already_imported")

    if not os.path.lexists(src_dir):
        return _summary("no_staging")

    try:
        _check_dir(staging_root, owner_uid)
        _check_dir(src_dir, owner_uid)
        staged: list[tuple[str, bytes, int]] = []
        with os.scandir(src_dir) as entries:
            names = sorted(e.name for e in entries)
        if not names:
            raise ImportRefused("staging_empty")
        for name in names:
            if name not in allow:
                raise ImportRefused("not_allow_listed")
            path = src_dir / name
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode):
                raise ImportRefused("symlink", name)
            if not stat.S_ISREG(st.st_mode):
                raise ImportRefused("not_regular_file", name)
            if st.st_size > max_bytes:
                raise ImportRefused("too_large", name)
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as fh:
                data = fh.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ImportRefused("too_large", name)
            try:
                doc = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ImportRefused("invalid_json", name) from None
            if not isinstance(doc, (dict, list)):
                raise ImportRefused("invalid_json", name)
            staged.append((name, data, _count(doc)))
    except ImportRefused as exc:
        # The name is only echoed when it is on the allow-list.
        log.error("import_from_share refused: %s%s", exc.code, f" file={exc.name}" if exc.name in allow else "")
        return _summary("refused", reason=exc.code, file=exc.name if exc.name in allow else "")
    except OSError:
        log.error("import_from_share refused: staging_unreadable")
        return _summary("refused", reason="staging_unreadable", file="")

    files = []
    for name, data, count in staged:
        dest = data_root / allow[name]
        _write_atomic(dest, data)
        files.append({"file": name, "bytes": len(data), "entries": count})

    record = {
        "imported_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "addon": addon,
        "files": files,
    }
    _write_atomic(marker, json.dumps(record, indent=2).encode("utf-8"))

    # Delete the source only after the import and the marker are on disk.
    cleanup = "deleted"
    try:
        for name, _data, _count_ in staged:
            (src_dir / name).unlink()
        src_dir.rmdir()
        try:
            staging_root.rmdir()  # only succeeds when no other add-on is staged
        except OSError:
            pass
    except OSError:
        cleanup = "flagged"
        log.warning("import_from_share: imported, but the staging folder could not be deleted; delete it by hand")

    for f in files:
        log.info("import_from_share: imported %s bytes=%d entries=%d", f["file"], f["bytes"], f["entries"])
    return _summary("imported", files=files, staging=cleanup)
