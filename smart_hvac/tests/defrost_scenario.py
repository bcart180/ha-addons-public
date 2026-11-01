"""0.5.1: scripted defrost scenario. Its output with defrost_aware off (or absent) must be byte-identical to
the 0.5.0 engine's; tests/fixtures/defrost_off_golden.json was generated from 0.5.0 main (95fbf356)."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rootfs", "app"))
sys.path.insert(0, os.path.dirname(__file__))
from engine import Engine  # noqa: E402
from test_040 import Clock  # noqa: E402
from test_engine import ZONES, FakeHA, lt14, opts, states  # noqa: E402

GOLDEN = os.path.join(os.path.dirname(__file__), "fixtures", "defrost_off_golden.json")

# (hvac_action, indoor temp, outdoor temp or None, setpoint seen in HA)
STEPS = ([("heating", 66, 30, None)] * 2 + [("heating", 65, 30, None), ("heating", 64.2, 30, None)]
         + [("defrosting", 64, 30, None)] * 3 + [("heating", 65, 30, None)]
         + [("heating", 64, None, None), ("heating", 63, "unknown", None)]
         + [("defrosting", 63, 30, 75)] * 9 + [("heating", 64, 30, 75)]
         + [("preheating", 64, 30, None), ("idle", 64, 50, None)]
         + [(a, 64 - i * 0.6, 28, None) for i, a in enumerate(["heating"] * 6)]
         + [("defrosting", 64, 28, None), ("heating", 65, 28, None)] * 6)


def run(**extra):
    zones = [dict(z, heat_pump=True) if z["name"] == "lr" else dict(z) for z in ZONES]
    ha, clk = FakeHA(states()), Clock()
    e = Engine(opts(zones=zones, **extra), ha, None, clock=clk, localtime=lt14)
    out = []
    for a, t, o, sp in STEPS:
        clk.t += 300
        st, at = ha.s["climate.lr"]
        at = dict(at, hvac_action=a, current_temperature=t)
        if sp is not None:
            at["temperature"] = sp
        ha.s["climate.lr"] = (st, at)
        ha.s["sensor.lr_t"] = (str(t), {})
        ha.s["weather.w"] = ("cloudy", {"temperature": o, "cloud_coverage": 100}) if o != "unknown" else (
            "unavailable", {})
        n = len(ha.calls)
        out.append({"r": e.evaluate(), "calls": ha.calls[n:]})
    st = {k: v for k, v in e.st.items()}
    return json.dumps({"steps": out, "st": st}, sort_keys=True, default=str)


if __name__ == "__main__":
    with open(GOLDEN, "w") as f:
        f.write(run())
