"""0.7.5: learned_preheat + away_delay_min=15 turned on together must never put the two splits in opposite modes.

Same fuzz as test_060 (random start modes / temps / overrides), but with both options on, a clock that moves through
pre-wake and departure windows, and presence flapping so the 15-minute departure debounce is exercised.
"""
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rootfs", "app"))
sys.path.insert(0, os.path.dirname(__file__))

import core  # noqa: E402
from engine import Engine  # noqa: E402
from test_060_mode_lock import MODES, NOW, StampedHA, opts, split_modes, st  # noqa: E402
from test_engine import lt14  # noqa: E402


def test_fuzz_preheat_and_away_delay_never_split_modes():
    rnd = random.Random(75)
    for _ in range(600):
        lr = (rnd.choice(MODES), rnd.choice([60, 68, 71, 78]), rnd.choice([60, 66, 71, 76, 82]))
        br = (rnd.choice(MODES), rnd.choice([60, 68, 71, 78]), rnd.choice([60, 66, 71, 76, 82]))
        ha = StampedHA(st(lr=lr, br=br, outdoor=rnd.choice([30, 55, 70, 80, 95])),
                       {"climate.lr": "2027-01-15T08:0%d:00+00:00" % rnd.randint(0, 9),
                        "climate.br": "2027-01-15T08:0%d:00+00:00" % rnd.randint(0, 9)})
        t = [NOW + rnd.randint(0, 86400)]
        e = Engine(opts(learned_preheat=True, away_delay_min=15), ha, None, clock=lambda: t[0], localtime=lt14)
        for z in ("lr", "br"):
            if rnd.random() < 0.3:
                e.start_override(z)
        for _cycle in range(6):
            ha.s["device_tracker.phone"] = (rnd.choice(["home", "not_home", "unavailable"]), {})
            e.evaluate()
            assert not core.shared_mode_conflict(split_modes(ha)), (lr, br, ha.calls)
            t[0] += rnd.choice([60, 600, 900, 1800, 3600])
        assert not core.shared_mode_conflict(split_modes(ha))
