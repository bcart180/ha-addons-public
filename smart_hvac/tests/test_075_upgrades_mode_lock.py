"""0.7.5: learned_preheat + away_delay_min=15 turned on together must never put the two splits in opposite modes.

Same fuzz as test_060 (random start modes / temps / overrides), but with both options on, a moving clock
(localtime=time.gmtime) that starts 0-180 min before the 07:00 wake, per-zone seeded warm-up rates so the two
learned leads diverge, and presence flapping so the 15-minute departure debounce is exercised.
"""
import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rootfs", "app"))
sys.path.insert(0, os.path.dirname(__file__))

import core  # noqa: E402
from engine import Engine  # noqa: E402
from test_060_mode_lock import MODES, StampedHA, opts, split_modes, st  # noqa: E402

WAKE = 1_800_000_000 - 1_800_000_000 % 86400 + 7 * 3600  # 07:00 UTC on some day


def _samples(rnd, rate):
    return [[o, round(rate * rnd.uniform(0.9, 1.1), 2)] for o in (30, 50, 70, 90)]


def test_fuzz_preheat_and_away_delay_never_split_modes():
    assert time.gmtime(WAKE)[3:5] == (7, 0)
    rnd = random.Random(75)
    preheat_hits = diverged = 0
    for _ in range(600):
        lr = (rnd.choice(MODES), rnd.choice([60, 68, 71, 78]), rnd.choice([60, 62, 66, 71, 76, 80]))
        br = (rnd.choice(MODES), rnd.choice([60, 68, 71, 78]), rnd.choice([60, 62, 66, 71, 76, 80]))
        ha = StampedHA(st(lr=lr, br=br, outdoor=rnd.choice([30, 55, 70, 80, 95])),
                       {"climate.lr": "2027-01-15T08:0%d:00+00:00" % rnd.randint(0, 9),
                        "climate.br": "2027-01-15T08:0%d:00+00:00" % rnd.randint(0, 9)})
        t = [WAKE - rnd.randint(0, 180) * 60]
        e = Engine(opts(learned_preheat=True, away_delay_min=15), ha, None,
                   clock=lambda t=t: t[0], localtime=time.gmtime)
        # different learned rates per zone (slow vs fast) so their pre-wake leads diverge
        slow, fast = rnd.uniform(0.3, 1.0), rnd.uniform(3.0, 8.0)
        r_lr, r_br = (slow, fast) if rnd.random() < 0.5 else (fast, slow)
        e.st["warmup"] = {z: {"samples": {"heat": _samples(rnd, r), "cool": _samples(rnd, r)}, "ep": None}
                          for z, r in (("lr", r_lr), ("br", r_br))}
        for z in ("lr", "br"):
            if rnd.random() < 0.2:
                e.start_override(z)
        for _cycle in range(6):
            ha.s["device_tracker.phone"] = (rnd.choice(["home", "home", "not_home", "unavailable"]), {})
            res = e.evaluate()
            assert not core.shared_mode_conflict(split_modes(ha)), (lr, br, ha.calls)
            preheat_hits += sum(1 for z in ("lr", "br") if (res.get(z) or {}).get("preheat"))
            leads = [(res.get(z) or {}).get("preheat_lead_min") for z in ("lr", "br")]
            diverged += None not in leads and leads[0] != leads[1]
            t[0] += rnd.choice([60, 300, 600, 900, 1800])
        assert not core.shared_mode_conflict(split_modes(ha))
    # the fuzz must actually reach the learned pre-wake window with diverging leads
    assert preheat_hits > 100 and diverged > 100, (preheat_hits, diverged)
