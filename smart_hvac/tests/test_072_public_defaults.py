"""0.7.2: zones ship empty; no real entity IDs in config.yaml; an empty zone list is inert."""
import os
import re
import sys

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rootfs", "app"))
sys.path.insert(0, os.path.dirname(__file__))

from test_engine import eng, opts  # noqa: E402

CFG = os.path.join(os.path.dirname(__file__), "..", "config.yaml")


def test_zones_default_empty():
    cfg = yaml.safe_load(open(CFG))
    assert cfg["options"]["zones"] == []
    assert cfg["options"]["presence_entities"] == [] and cfg["options"]["weather_entity"] == ""


def test_no_entity_ids_in_option_defaults():
    rx = re.compile(r"\b(climate|sensor|binary_sensor|device_tracker|person|weather|input_boolean)\.[a-z0-9_]+")
    cfg = yaml.safe_load(open(CFG))
    assert not rx.search(yaml.safe_dump(cfg["options"]))


def test_empty_zones_evaluate_sends_nothing():
    e, ha = eng(opts(zones=[]))
    e.evaluate()
    assert ha.calls == []
    assert e.snapshot() is not None
