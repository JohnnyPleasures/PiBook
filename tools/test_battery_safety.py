#!/usr/bin/env python3
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from src.hardware.battery_safety import (
    BatterySafetyConfig,
    BatterySafetyController,
)
from src.ui.battery_alert_screen import BatteryAlertScreen


def feed(controller, rows):
    return [
        controller.update(
            voltage=voltage,
            current_ma=current_ma,
        )
        for voltage, current_ma in rows
    ]


cfg = BatterySafetyConfig()

# Healthy discharge.
controller = BatterySafetyController(cfg)
assert feed(
    controller,
    [(3.90, -200.0)] * 3,
) == ["none", "none", "none"]

# One transient dip must not warn.
controller = BatterySafetyController(cfg)
assert feed(
    controller,
    [(3.61, -200.0), (3.66, -200.0)],
) == ["none", "none"]

# Two confirmed readings under 3.62 V -> one warning only.
controller = BatterySafetyController(cfg)
assert feed(
    controller,
    [(3.61, -200.0), (3.61, -200.0)],
) == ["none", "warning"]
assert controller.update(
    voltage=3.61,
    current_ma=-200.0,
) == "none"

# Charging confirmed by positive current rearms the warning.
assert controller.update(
    voltage=3.65,
    current_ma=100.0,
) == "rearmed"

assert feed(
    controller,
    [(3.61, -200.0), (3.61, -200.0)],
) == ["none", "warning"]

# Three critical readings -> one critical event.
controller = BatterySafetyController(cfg)
events = feed(
    controller,
    [(3.49, -200.0)] * 3,
)
assert events == ["none", "warning", "critical"], events
assert controller.update(
    voltage=3.49,
    current_ma=-200.0,
) == "none"

# Emergency floor -> immediate critical event.
controller = BatterySafetyController(cfg)
assert controller.update(
    voltage=3.34,
    current_ma=-200.0,
) == "critical"

# Near-zero/ambiguous current can never cause a destructive event.
controller = BatterySafetyController(cfg)
assert feed(
    controller,
    [(3.30, -5.0)] * 5,
) == ["none"] * 5

# Invalid voltage is ignored.
controller = BatterySafetyController(cfg)
assert controller.update(
    voltage=0.0,
    current_ma=-200.0,
) == "none"

# Render both alert images in RAM only.
screen = BatteryAlertScreen(800, 480)
for critical in (False, True):
    image = screen.render(
        critical=critical,
        voltage=3.55,
        percentage=9,
        shutdown_enabled=False,
    )
    assert image.size == (800, 480)
    assert image.mode == "1"

print("BatterySafetyController healthy path: OK")
print("Transient dip debounce: OK")
print("Low-battery warning latch: OK")
print("Charging rearm: OK")
print("Critical debounce: OK")
print("Emergency floor: OK")
print("Ambiguous-current guard: OK")
print("Invalid-reading guard: OK")
print("BatteryAlertScreen off-screen render: OK")
