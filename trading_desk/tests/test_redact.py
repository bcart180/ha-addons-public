import io, logging, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rootfs", "app"))
os.environ.setdefault("TD_MOCK", "1")
import server  # noqa: E402

DUMMY = "dummytok_0000000000000000ABCD"


def _capture(fn):
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(logging.Formatter("%(message)s"))
    h.addFilter(server.Redact())
    lg = logging.getLogger("td-redact-test")
    lg.handlers, lg.propagate, lg.level = [h], False, logging.DEBUG
    fn(lg)
    return buf.getvalue()


def test_exception_traceback_masked(monkeypatch):
    monkeypatch.setattr(server, "GH_TOKEN", DUMMY)
    def go(lg):
        try:
            raise RuntimeError("auth failed for " + DUMMY)
        except RuntimeError:
            lg.exception("boom %s", DUMMY)
    out = _capture(go)
    assert DUMMY not in out
    assert "****ABCD" in out and "RuntimeError" in out


def test_stack_info_masked(monkeypatch):
    monkeypatch.setattr(server, "GH_TOKEN", DUMMY)
    out = _capture(lambda lg: lg.warning("x " + DUMMY, stack_info=True))
    assert DUMMY not in out and "****ABCD" in out
