"""0.5.4: ReDoS guards for masking and summary extraction (fake 555 data only)."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))
import call_summary as cs  # noqa: E402

LIMIT_S = 1.0

HOSTILE = {
    "plus_paren_dash_dot_10k": "+" + "(-." * 10_000,
    "push_fallback_90k": "+" + "(-." * 30_000,
    "lookalike_O_100k": "O" * 100_000 + "5",
    "dotted_caps_100k": "A." * 50_000,
    "this_is_caps_100k": "this is " + "A" * 100_000,
    "digit_letter_seps_100k": "5!a" * 33_334,
    "md_link_nest_100k": "[a](" * 25_000,
    "mixed_100k": ("555-01 x (-.+O" * 8_000)[:100_000],
}


def _timed(fn, *args):
    t = time.perf_counter()
    fn(*args)
    return time.perf_counter() - t


def test_hostile_inputs_are_fast():
    for name, text in HOSTILE.items():
        for label, fn, arg in (
            ("mask", cs.mask_phone_runs, text),
            ("fallback", cs.fallback_line, {"transcript": text}),
            ("extract", cs.extract_summary, text),
            ("stored", cs.stored_or_extract, {"summary": {"urgency": "low", "reason": text, "name": text}}),
        ):
            took = _timed(fn, arg)
            assert took < LIMIT_S, f"{name}/{label} took {took:.2f}s"


def test_lookalikes_unchanged():
    assert cs.mask_phone_runs("call 555-O1OO now") == "call •••0100 now"
    assert cs.mask_phone_runs("l-555-0177") == "•••0177"
    assert cs._fix_lookalikes("abcO55-5x") == "abcO55-5x"
    assert cs._fix_lookalikes("x l-555-0177l") == "x 1-555-01771"


def test_cap_never_leaves_partial_number():
    text = "a" * (cs.MASK_MAX - 5) + " 555-555-0123 tail"
    out = cs.mask_phone_runs(text)
    assert len(out) <= cs.MASK_MAX + 1
    assert "555" not in out and "0123" not in out
    assert out.endswith("…")


def test_short_text_not_capped():
    assert cs.mask_phone_runs("Ann at 555.555.0142") == "Ann at •••0142"
