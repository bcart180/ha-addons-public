"""Entry point: load /data/options.json and run the engine loop."""
import json
import logging
import os
import time

from engine import ENTITY_KEYS, Engine
from ha_client import HAClient

OPTIONS = os.environ.get("TSC_OPTIONS", "/data/options.json")


def main():
    with open(OPTIONS) as f:
        opts = json.load(f)
    logging.basicConfig(level=getattr(logging, str(opts.get("log_level", "info")).upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s")
    ha = HAClient([opts.get(k) for k in ENTITY_KEYS], opts.get("notify_service"),
                  opts.get("status_entity") or "sensor.tesla_smart_charge_status")
    eng = Engine(opts, ha)
    tick = int(opts.get("tick_s") or 30)
    logging.info("Tesla Smart Charge started (dry_run=%s, check every %s min)", eng.o["dry_run"],
                 eng.o["check_interval_min"])
    while True:
        try:
            eng.tick()
        except Exception as e:  # noqa: BLE001 - never crash the loop
            logging.warning("tick failed: %s", e)
        time.sleep(tick)


if __name__ == "__main__":
    main()
