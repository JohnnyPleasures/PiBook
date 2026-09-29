#!/usr/bin/env python3
"""Teste interativo dos dois botões físicos do PiBook."""

from __future__ import annotations

import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from src.hardware.gpio_handler import GPIOHandler


def event(message: str):
    def callback():
        print(f"\n{message}", flush=True)
    return callback


def main() -> int:
    config = PROJECT / "config" / "gpio_mapping.yaml"
    gpio = GPIOHandler(str(config), long_press_duration=0.8)

    if not gpio.hardware_available:
        print("ERRO: gpiozero não conseguiu aceder ao hardware GPIO.")
        return 1

    gpio.register_callback(
        "toggle", event("GPIO5 curto → AVANÇAR"), long_press=False
    )
    gpio.register_callback(
        "toggle", event("GPIO5 longo → SELECIONAR"), long_press=True
    )
    gpio.register_callback(
        "back", event("GPIO6 curto   → RECUAR"), long_press=False
    )
    gpio.register_callback(
        "back", event("GPIO6 longo   → VOLTAR"), long_press=True
    )

    print("Teste dos dois botões ativo.")
    print("GPIO5 curto/longo: avançar/selecionar")
    print("GPIO6 curto/longo: recuar/voltar")
    print("Mantém premido pelo menos 0,8 s para pressão longa.")
    print("Ctrl+C para terminar.\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print()
    finally:
        gpio.cleanup()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
