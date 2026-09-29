#!/usr/bin/env python3
"""Passive battery-cycle logger for PiBook.

Reads the lightweight localhost battery-log snapshot API. It does not control SOC,
alerts, charging, or shutdown.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


API_URL = os.environ.get(
    "PIBOOK_BATTERY_LOG_API",
    "http://127.0.0.1:5000/api/battery_log_snapshot",
)
INTERVAL = max(
    10.0,
    float(os.environ.get("PIBOOK_BATTERY_LOG_INTERVAL", "30")),
)
DATA_DIR = Path(
    os.environ.get(
        "PIBOOK_BATTERY_LOG_DIR",
        "/home/pi/PiBook/diagnostics/battery_cycles",
    )
)

FIELDS = [
    "wall_time",
    "monotonic_s",
    "boot_id",
    "session_id",
    "battery_backend",
    "voltage_v",
    "current_ma",
    "power_w",
    "soc_percentage",
    "soc_precise",
    "percentage_voltage",
    "charging",
    "delta_mah",
    "session_net_mah",
    "session_charged_mah",
    "session_discharged_mah",
    "current_screen",
    "wifi_status",
    "bluetooth_status",
]


def get_boot_id() -> str:
    try:
        return Path(
            "/proc/sys/kernel/random/boot_id"
        ).read_text(encoding="utf-8").strip()
    except Exception:
        return "unknown"


def read_stats() -> dict:
    request = urllib.request.Request(
        API_URL,
        headers={"User-Agent": "PiBook-Battery-Logger/1"},
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.load(response)


def ensure_writer(path: Path):
    new_file = not path.exists() or path.stat().st_size == 0
    handle = path.open(
        "a",
        encoding="utf-8",
        newline="",
        buffering=1,
    )
    writer = csv.DictWriter(handle, fieldnames=FIELDS)
    if new_file:
        writer.writeheader()
        handle.flush()
        os.fsync(handle.fileno())
    return handle, writer


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logger = logging.getLogger("pibook-battery-logger")

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    boot_id = get_boot_id()
    session_id = str(uuid.uuid4())
    filename = f"battery_boot_{boot_id[:8]}_v2.csv"
    path = DATA_DIR / filename

    handle, writer = ensure_writer(path)

    logger.info(
        "Battery logger started: interval=%.0fs file=%s session=%s",
        INTERVAL,
        path,
        session_id,
    )

    previous_monotonic = None
    previous_current = None

    session_net_mah = 0.0
    session_charged_mah = 0.0
    session_discharged_mah = 0.0
    rows_since_sync = 0

    try:
        while True:
            loop_start = time.monotonic()

            try:
                data = read_stats()

                voltage = float(
                    data.get("battery_voltage", 0.0) or 0.0
                )
                current = float(
                    data.get("battery_current_ma", 0.0) or 0.0
                )
                soc_percentage = int(
                    data.get("battery_percentage", 0) or 0
                )
                soc_precise = float(
                    data.get("battery_soc_precise", soc_percentage)
                    or soc_percentage
                )
                percentage_voltage = int(
                    data.get(
                        "battery_percentage_voltage",
                        soc_percentage,
                    )
                    or 0
                )
                charging = bool(
                    data.get("battery_charging", False)
                )

                now_mono = time.monotonic()
                delta_mah = 0.0

                if (
                    previous_monotonic is not None
                    and previous_current is not None
                ):
                    dt_hours = (
                        now_mono - previous_monotonic
                    ) / 3600.0

                    # Trapezoidal integration of measured battery current.
                    avg_current = (
                        previous_current + current
                    ) / 2.0
                    delta_mah = avg_current * dt_hours

                    # Ignore absurd gaps, e.g. process suspended for hours.
                    if 0.0 <= dt_hours <= (INTERVAL * 4.0 / 3600.0):
                        session_net_mah += delta_mah
                        if delta_mah >= 0:
                            session_charged_mah += delta_mah
                        else:
                            session_discharged_mah += -delta_mah
                    else:
                        delta_mah = 0.0

                previous_monotonic = now_mono
                previous_current = current

                row = {
                    "wall_time": time.strftime(
                        "%Y-%m-%dT%H:%M:%S%z"
                    ),
                    "monotonic_s": f"{now_mono:.3f}",
                    "boot_id": boot_id,
                    "session_id": session_id,
                    "battery_backend": data.get(
                        "battery_backend",
                        "",
                    ),
                    "voltage_v": f"{voltage:.4f}",
                    "current_ma": f"{current:.2f}",
                    "power_w": f"{voltage * current / 1000.0:.5f}",
                    "soc_percentage": soc_percentage,
                    "soc_precise": f"{soc_precise:.4f}",
                    "percentage_voltage": percentage_voltage,
                    "charging": charging,
                    "delta_mah": f"{delta_mah:.6f}",
                    "session_net_mah": f"{session_net_mah:.4f}",
                    "session_charged_mah": (
                        f"{session_charged_mah:.4f}"
                    ),
                    "session_discharged_mah": (
                        f"{session_discharged_mah:.4f}"
                    ),
                    "current_screen": data.get(
                        "current_screen",
                        "",
                    ),
                    "wifi_status": data.get(
                        "wifi_status",
                        "",
                    ),
                    "bluetooth_status": data.get(
                        "bluetooth_status",
                        "",
                    ),
                }

                writer.writerow(row)
                handle.flush()
                rows_since_sync += 1

                # Persist at least every ~5 minutes without forcing an SD
                # sync for every 30-second sample.
                if rows_since_sync >= 10:
                    os.fsync(handle.fileno())
                    rows_since_sync = 0

            except (
                urllib.error.URLError,
                TimeoutError,
                ValueError,
                KeyError,
                json.JSONDecodeError,
            ) as exc:
                logger.warning(
                    "Battery API sample failed: %s",
                    exc,
                )
            except Exception:
                logger.exception(
                    "Unexpected battery logger error"
                )

            elapsed = time.monotonic() - loop_start
            time.sleep(max(1.0, INTERVAL - elapsed))
    finally:
        try:
            handle.flush()
            os.fsync(handle.fileno())
        except Exception:
            pass
        handle.close()


if __name__ == "__main__":
    main()
