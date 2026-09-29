#!/usr/bin/env python3
import argparse
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from src.hardware.battery_monitor import WaveshareUPSHatCBackend


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bus", type=int, default=1)
    parser.add_argument("--address", default="0x43")
    parser.add_argument("--samples", type=int, default=8)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()

    ups = WaveshareUPSHatCBackend(
        i2c_bus=args.bus,
        address=args.address,
        shunt_ohms=0.1,
        smoothing_samples=5,
        battery_capacity_mah=4000,
    )

    if not ups.is_available():
        print(
            f"ERRO: não foi possível ler o INA219 em "
            f"I2C-{args.bus}/{args.address}."
        )
        return 1

    print(f"UPS HAT (C) detetado em {args.address}.\n")
    try:
        for index in range(max(1, args.samples)):
            status = ups.get_extended_status()
            state = "a carregar" if status["current_ma"] > 0.0 else "em descarga"
            print(
                f"{index + 1:02d}: "
                f"{status['voltage']:.3f} V | "
                f"{status['current_ma']:+.1f} mA | "
                f"{status['power_w']:+.3f} W | "
                f"{status['percentage']:3d}% | {state}"
            )
            if index + 1 < args.samples:
                time.sleep(max(0.1, args.interval))
    finally:
        ups.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
