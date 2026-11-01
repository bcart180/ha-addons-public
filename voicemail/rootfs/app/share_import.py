"""One-shot import of add-on /data files from a /share staging folder.

Used once, to move an add-on from one store to another: the old add-on's
allow-listed /data files are copied (by the owner, with the old add-on
stopped) into ``/share/.migrate-2026-11-01/<addon>/``, and the new add-on
imports them on its first start.

Rules (store migration plan, C3/C4/C6):

* Reads only ``<staging_root>/<addon>/``. The staging root and the add-on
  folder must be real directories (not symlinks), owned by the expected uid
  (root) and mode 700 (no group/other bits).
* Every entry must be on the allow-list, a regular file (symlinks refused;
  opened O_NONBLOCK|O_NOFOLLOW and re-checked with fstat on the open fd),
  at most ``max_bytes`` and valid JSON. Any refusal aborts the whole import
  before anything is written (fail closed); the staging folder is left for
  the owner to inspect and nothing is marked done.
* Fresh install expected: if any destination already exists the whole import
  is refused (``dest_exists``), staging left in place, nothing overwritten.
* Files are moved file-to-file: the validated bytes are written as-is. All
  temp files are written and fsynced first, then all are renamed, then the
  marker; on any failure the temps (and any renamed files) are removed and no
  marker is written. Nothing is re-serialized, printed or logged.
* Staging traversal and cleanup go through directory fds opened with
  O_DIRECTORY|O_NOFOLLOW, so a swapped-in symlink cannot steer them.
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
import errno
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


_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | _O_CLOEXEC


def _check_dir_stat(st: os.stat_result, owner_uid: int) -> None:
    if stat.S_ISLNK(st.st_mode):
        raise ImportRefused("staging_symlink")
    if not stat.S_ISDIR(st.st_mode):
        raise ImportRefused("staging_not_dir")
    if st.st_uid != owner_uid:
        raise ImportRefused("staging_owner")
    if st.st_mode & 0o077:
        raise ImportRefused("staging_mode")


def _open_dir(name, owner_uid: int, dir_fd: int | None = None) -> int:
    """lstat, then open with O_DIRECTORY|O_NOFOLLOW and re-check the open fd.

    All later traversal and cleanup go through the returned fd, so swapping
    the path for a symlink afterwards cannot steer reads or deletes (L2).
    """
    try:
        st = os.lstat(name, dir_fd=dir_fd)
    except FileNotFoundError:
        raise ImportRefused("staging_missing") from None
    _check_dir_stat(st, owner_uid)
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        raise ImportRefused("staging_missing") from None
    except NotADirectoryError:
        raise ImportRefused("staging_not_dir") from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ImportRefused("staging_symlink") from None
        raise
    try:
        fst = os.fstat(fd)
        _check_dir_stat(fst, owner_uid)
        if (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
            raise ImportRefused("staging_changed")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_staged(name: str, src_fd: int, max_bytes: int) -> bytes:
    """Read one staged file safely (L1): never blocks, never follows links."""
    st = os.lstat(name, dir_fd=src_fd)
    if stat.S_ISLNK(st.st_mode):
        raise ImportRefused("symlink", name)
    if not stat.S_ISREG(st.st_mode):
        raise ImportRefused("not_regular_file", name)
    if st.st_size > max_bytes:
        raise ImportRefused("too_large", name)
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=src_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ImportRefused("symlink", name) from None
        raise ImportRefused("file_changed", name) from None
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode):
            raise ImportRefused("not_regular_file", name)
        if (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
            raise ImportRefused("file_changed", name)
        if fst.st_size > max_bytes:
            raise ImportRefused("too_large", name)
        chunks = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if len(data) > max_bytes:
        raise ImportRefused("too_large", name)
    return data


def _write_temp(dest: pathlib.Path, data: bytes) -> pathlib.Path:
    """Write ``data`` to a fresh temp file next to ``dest`` and fsync it."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".import.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        _unlink_quiet(tmp)
        raise
    return tmp


def _unlink_quiet(path: pathlib.Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _fsync_dir(path: pathlib.Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | _O_CLOEXEC)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _commit_all(pairs: list[tuple[pathlib.Path, bytes]]) -> None:
    """All-or-nothing write (L4): every temp first, then every rename.

    Destinations are known not to exist. On any failure the temps and any
    destinations already renamed in this call are removed, then it re-raises.
    """
    temps: list[tuple[pathlib.Path, pathlib.Path]] = []
    renamed: list[pathlib.Path] = []
    try:
        for dest, data in pairs:
            temps.append((_write_temp(dest, data), dest))
        for tmp, dest in temps:
            if os.path.lexists(dest):
                raise FileExistsError(dest.name)
            os.rename(tmp, dest)
            renamed.append(dest)
        for parent in {dest.parent for _t, dest in temps}:
            _fsync_dir(parent)
    except BaseException:
        for tmp, _dest in temps:
            _unlink_quiet(tmp)
        for dest in renamed:
            _unlink_quiet(dest)
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

    root_fd = src_fd = None
    try:
        try:
            root_fd = _open_dir(staging_root, owner_uid)
            src_fd = _open_dir(addon, owner_uid, dir_fd=root_fd)
            staged: list[tuple[str, bytes, int]] = []
            names = sorted(os.listdir(src_fd))
            if not names:
                raise ImportRefused("staging_empty")
            for name in names:
                if name not in allow:
                    raise ImportRefused("not_allow_listed")
            for name in names:
                data = _read_staged(name, src_fd, max_bytes)
                try:
                    doc = json.loads(data.decode("utf-8"))
                except (ValueError, RecursionError):
                    # ValueError covers JSONDecodeError, UnicodeDecodeError and
                    # the int-digit limit; RecursionError covers deep nesting (L3).
                    raise ImportRefused("invalid_json", name) from None
                if not isinstance(doc, (dict, list)):
                    raise ImportRefused("invalid_json", name)
                staged.append((name, data, _count(doc)))
            # Fresh install expected: never overwrite existing /data files (L4).
            for name, _data, _c in staged:
                dest = data_root / allow[name]
                if os.path.lexists(dest) or os.path.lexists(dest.with_name(dest.name + ".import.tmp")):
                    raise ImportRefused("dest_exists", name)
        except ImportRefused as exc:
            # The name is only echoed when it is on the allow-list.
            log.error("import_from_share refused: %s%s", exc.code, f" file={exc.name}" if exc.name in allow else "")
            return _summary("refused", reason=exc.code, file=exc.name if exc.name in allow else "")
        except OSError:
            log.error("import_from_share refused: staging_unreadable")
            return _summary("refused", reason="staging_unreadable", file="")

        files = [{"file": n, "bytes": len(d), "entries": c} for n, d, c in staged]
        record = {
            "imported_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "addon": addon,
            "files": files,
        }
        pairs = [(data_root / allow[n], d) for n, d, _c in staged]
        try:
            _commit_all(pairs)
            try:
                _commit_all([(marker, json.dumps(record, indent=2).encode("utf-8"))])
            except BaseException:
                for dest, _d in pairs:
                    _unlink_quiet(dest)
                raise
        except OSError:
            log.error("import_from_share failed: write_failed (nothing imported, staging left)")
            return _summary("error", reason="write_failed", file="")

        # Delete the source only after the import and the marker are on disk,
        # and only through the directory fds opened above (L2).
        cleanup = "deleted"
        try:
            for name, _data, _count_ in staged:
                os.unlink(name, dir_fd=src_fd)
            os.rmdir(addon, dir_fd=root_fd)
        except OSError:
            cleanup = "flagged"
            log.warning("import_from_share: imported, but the staging folder could not be deleted; delete it by hand")
        else:
            try:
                st, fst = os.lstat(staging_root), os.fstat(root_fd)
                if stat.S_ISDIR(st.st_mode) and (st.st_dev, st.st_ino) == (fst.st_dev, fst.st_ino):
                    os.rmdir(staging_root)  # only succeeds when no other add-on is staged
            except OSError:
                pass
    finally:
        for fd in (src_fd, root_fd):
            if fd is not None:
                os.close(fd)

    for f in files:
        log.info("import_from_share: imported %s bytes=%d entries=%d", f["file"], f["bytes"], f["entries"])
    return _summary("imported", files=files, staging=cleanup)
