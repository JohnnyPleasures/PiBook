"""
Monitor de bateria do PiBook.

Backends suportados:
- Waveshare UPS HAT (C), INA219
- PiSugar2
- ADS1115
- Mock, quando nenhum hardware é encontrado
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

try:
    import smbus
except ImportError:
    smbus = None


class BatteryBackend(ABC):
    safety_mode = "none"
    @abstractmethod
    def read_voltage(self) -> float:
        pass

    @abstractmethod
    def read_percentage(self) -> int:
        pass

    @abstractmethod
    def is_charging(self) -> bool:
        pass

    @abstractmethod
    def is_available(self) -> bool:
        pass

    @abstractmethod
    def get_name(self) -> str:
        pass


class WaveshareUPSHatCBackend(BatteryBackend):
    """Backend do Waveshare UPS HAT (C), baseado no INA219."""

    safety_mode = "voltage_current"

    REG_SHUNT_VOLTAGE = 0x01
    REG_BUS_VOLTAGE = 0x02

    LIPO_CURVE: Tuple[Tuple[float, int], ...] = (
        (3.20, 0),
        (3.35, 2),
        (3.45, 5),
        (3.55, 9),
        (3.60, 13),
        (3.65, 18),
        (3.70, 27),
        (3.75, 40),
        (3.80, 53),
        (3.85, 64),
        (3.90, 74),
        (3.95, 82),
        (4.00, 88),
        (4.05, 92),
        (4.10, 96),
        (4.15, 98),
        (4.20, 100),
    )

    def __init__(
        self,
        i2c_bus: int = 1,
        address: int = 0x43,
        shunt_ohms: float = 0.1,
        smoothing_samples: int = 5,
        charging_threshold_ma: float = 20.0,
        discharging_threshold_ma: float = -20.0,
        battery_capacity_mah: int = 4000,
    ):
        self.logger = logging.getLogger(__name__)
        self.i2c_bus = int(i2c_bus)
        self.address = int(address, 0) if isinstance(address, str) else int(address)
        self.shunt_ohms = float(shunt_ohms)
        self.charging_threshold_ma = float(charging_threshold_ma)
        self.discharging_threshold_ma = float(discharging_threshold_ma)
        self.battery_capacity_mah = int(battery_capacity_mah)

        if self.charging_threshold_ma <= 0:
            raise ValueError("O limiar de carga tem de ser positivo")
        if self.discharging_threshold_ma >= 0:
            raise ValueError("O limiar de descarga tem de ser negativo")

        # Estado elétrico com histerese:
        #   >= +limiar  -> a carregar
        #   <= -limiar  -> a descarregar
        #   entre ambos -> mantém o último estado conhecido.
        self._charging_state: Optional[bool] = None

        if self.shunt_ohms <= 0:
            raise ValueError("O valor do resistor shunt tem de ser positivo")

        sample_count = max(1, int(smoothing_samples))
        self._voltage_samples = deque(maxlen=sample_count)
        self._current_samples = deque(maxlen=sample_count)
        self._bus = None
        self._available = False
        self._lock = threading.RLock()
        self._cached_sample: Optional[Tuple[float, float]] = None
        self._last_sample = 0.0

        self._open()

    def _open(self) -> None:
        if smbus is None:
            self.logger.debug("python3-smbus não está disponível")
            return

        try:
            self._bus = smbus.SMBus(self.i2c_bus)
            voltage = self._read_bus_voltage()
            if not 2.5 <= voltage <= 4.6:
                raise OSError(f"Tensão inesperada no INA219: {voltage:.3f} V")
            self._available = True
            self.logger.info(
                "Waveshare UPS HAT (C) detetado em I2C-%d/0x%02X",
                self.i2c_bus,
                self.address,
            )
        except Exception as exc:
            self.logger.debug(
                "INA219 não detetado em I2C-%d/0x%02X: %s",
                self.i2c_bus,
                self.address,
                exc,
            )
            self.close()

    def _read_u16(self, register: int) -> int:
        if self._bus is None:
            raise OSError("Barramento I2C não inicializado")
        values = self._bus.read_i2c_block_data(self.address, register, 2)
        if len(values) != 2:
            raise OSError("Leitura incompleta do INA219")
        return (int(values[0]) << 8) | int(values[1])

    @staticmethod
    def _signed16(value: int) -> int:
        return value - 65536 if value & 0x8000 else value

    def _read_bus_voltage(self) -> float:
        raw = self._read_u16(self.REG_BUS_VOLTAGE)
        return float(raw >> 3) * 0.004

    def _read_current_ma(self) -> float:
        raw = self._signed16(self._read_u16(self.REG_SHUNT_VOLTAGE))
        shunt_mv = float(raw) * 0.01
        return shunt_mv / self.shunt_ohms

    def _sample(self, force: bool = False) -> Tuple[float, float]:
        if not self._available:
            raise OSError("UPS HAT (C) indisponível")

        with self._lock:
            now = time.monotonic()
            if (
                not force
                and self._cached_sample is not None
                and now - self._last_sample < 0.5
            ):
                return self._cached_sample

            try:
                voltage = self._read_bus_voltage()
                current_ma = self._read_current_ma()

                if not 2.0 <= voltage <= 5.0:
                    raise ValueError(f"Tensão inválida: {voltage:.3f} V")
                if not -5000.0 <= current_ma <= 5000.0:
                    raise ValueError(f"Corrente inválida: {current_ma:.1f} mA")

                self._voltage_samples.append(voltage)
                self._current_samples.append(current_ma)

                voltage = sum(self._voltage_samples) / len(self._voltage_samples)
                current_ma = sum(self._current_samples) / len(self._current_samples)

                self._cached_sample = (voltage, current_ma)
                self._last_sample = now
                return self._cached_sample
            except Exception as exc:
                self.logger.warning("Falha ao ler o UPS HAT (C): %s", exc)
                self._available = False
                self.close()
                raise OSError(
                    "Falha de comunicação com UPS HAT (C)"
                ) from exc

    @classmethod
    def voltage_to_percentage(cls, voltage: float) -> int:
        if voltage <= cls.LIPO_CURVE[0][0]:
            return 0
        if voltage >= cls.LIPO_CURVE[-1][0]:
            return 100

        for (v0, p0), (v1, p1) in zip(cls.LIPO_CURVE, cls.LIPO_CURVE[1:]):
            if v0 <= voltage <= v1:
                ratio = (voltage - v0) / (v1 - v0)
                return int(round(p0 + ratio * (p1 - p0)))
        return 0

    def read_voltage(self) -> float:
        return self._sample()[0]

    def read_current_ma(self) -> float:
        return self._sample()[1]

    def read_percentage(self) -> int:
        voltage = self.read_voltage()
        return self.voltage_to_percentage(voltage) if voltage > 0 else 0

    def _charging_from_current(self, current_ma: float) -> bool:
        current_ma = float(current_ma)

        if current_ma >= self.charging_threshold_ma:
            self._charging_state = True
        elif current_ma <= self.discharging_threshold_ma:
            self._charging_state = False
        elif self._charging_state is None:
            # Ao arrancar dentro da zona neutra não existe informação
            # suficiente para distinguir carregador ligado de bateria.
            # Assume bateria até surgir uma leitura inequívoca.
            self._charging_state = False

        return bool(self._charging_state)

    def is_charging(self) -> bool:
        return self._charging_from_current(self.read_current_ma())

    def is_available(self) -> bool:
        return self._available

    def get_name(self) -> str:
        return "Waveshare UPS HAT (C)"

    def get_extended_status(self) -> dict:
        voltage, current_ma = self._sample(force=True)
        percentage = self.voltage_to_percentage(voltage) if voltage > 0 else 0
        remaining_hours = None

        if current_ma <= self.discharging_threshold_ma and percentage > 0:
            remaining_mah = self.battery_capacity_mah * percentage / 100.0
            remaining_hours = remaining_mah / abs(current_ma)

        return {
            "voltage": voltage,
            "percentage": percentage,
            "current_ma": current_ma,
            "power_w": voltage * current_ma / 1000.0,
            "is_charging": self._charging_from_current(current_ma),
            "remaining_hours_estimate": remaining_hours,
            "capacity_mah": self.battery_capacity_mah,
            "address": self.address,
        }

    def close(self) -> None:
        bus = self._bus
        self._bus = None
        self._available = False
        if bus is not None:
            try:
                bus.close()
            except Exception:
                pass

    def __del__(self):
        self.close()


class PiSugar2Backend(BatteryBackend):
    safety_mode = "voltage_external_power"
    def __init__(self, socket_path: str = "/tmp/pisugar-server.sock"):
        self.logger = logging.getLogger(__name__)
        self.socket_path = socket_path
        self._available = (
            os.path.exists(socket_path)
            and self._send_command("get battery") is not None
        )

    def _send_command(self, command: str) -> Optional[str]:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(1.0)
                sock.connect(self.socket_path)
                sock.sendall(f"{command}\n".encode())
                return sock.recv(1024).decode().strip()
        except Exception:
            return None

    def read_voltage(self) -> float:
        response = self._send_command("get battery_v")
        if not response:
            raise OSError("PiSugar não respondeu a battery_v")
        try:
            return float(response.split(":")[-1].strip())
        except Exception as exc:
            raise OSError("Resposta battery_v inválida") from exc

    def read_percentage(self) -> int:
        response = self._send_command("get battery")
        if not response:
            raise OSError("PiSugar não respondeu a battery")
        try:
            return int(float(response.split(":")[-1].strip()))
        except Exception as exc:
            raise OSError("Resposta battery inválida") from exc

    def is_charging(self) -> bool:
        response = self._send_command("get battery_power_plugged")
        if not response:
            raise OSError(
                "PiSugar não respondeu a battery_power_plugged"
            )
        return response.split(":")[-1].strip().lower() == "true"

    def is_available(self) -> bool:
        return self._available

    def get_name(self) -> str:
        return "PiSugar2"

    def get_rtc_time(self) -> Optional[str]:
        response = self._send_command("get rtc_time")
        return response.split(":", 1)[-1].strip() if response else None


class ADS1115Backend(BatteryBackend):
    safety_mode = "voltage_only"
    def __init__(
        self,
        adc_channel: int = 0,
        voltage_divider_ratio: float = 2.0,
        min_voltage: float = 3.0,
        max_voltage: float = 4.2,
    ):
        self.logger = logging.getLogger(__name__)
        self.adc_channel = adc_channel
        self.voltage_divider_ratio = voltage_divider_ratio
        self.min_voltage = min_voltage
        self.max_voltage = max_voltage
        self._available = False
        self.channel = None

        try:
            import board
            import busio
            import adafruit_ads1x15.ads1115 as ADS
            from adafruit_ads1x15.analog_in import AnalogIn

            i2c = busio.I2C(board.SCL, board.SDA)
            ads = ADS.ADS1115(i2c)
            channels = {0: ADS.P0, 1: ADS.P1, 2: ADS.P2, 3: ADS.P3}
            self.channel = AnalogIn(ads, channels[adc_channel])
            self._available = True
        except Exception:
            self._available = False

    def read_voltage(self) -> float:
        if not self._available:
            raise OSError("ADS1115 indisponível")
        try:
            return self.channel.voltage * self.voltage_divider_ratio
        except Exception as exc:
            self._available = False
            raise OSError("Falha de leitura ADS1115") from exc

    def read_percentage(self) -> int:
        voltage = max(self.min_voltage, min(self.max_voltage, self.read_voltage()))
        denominator = self.max_voltage - self.min_voltage
        if denominator <= 0:
            return 0
        return int(round((voltage - self.min_voltage) / denominator * 100))

    def is_charging(self) -> bool:
        return False

    def is_available(self) -> bool:
        return self._available

    def get_name(self) -> str:
        return "ADS1115"


class UnavailableBackend(BatteryBackend):
    """Hardware de bateria esperado, mas temporariamente indisponível."""

    def __init__(self, expected_backend: str = "auto"):
        self.expected_backend = str(expected_backend or "auto")

    def read_voltage(self) -> float:
        return 0.0

    def read_percentage(self) -> int:
        return 0

    def is_charging(self) -> bool:
        return False

    def is_available(self) -> bool:
        return False

    def get_name(self) -> str:
        return f"Unavailable ({self.expected_backend})"


class MockBackend(BatteryBackend):
    def read_voltage(self) -> float:
        return 3.9

    def read_percentage(self) -> int:
        return 75

    def is_charging(self) -> bool:
        return False

    def is_available(self) -> bool:
        return True

    def get_name(self) -> str:
        return "Mock"


class BatteryMonitor:
    """Interface de bateria usada pelas páginas do PiBook."""

    def __init__(
        self,
        adc_channel: int = 0,
        voltage_divider_ratio: float = 2.0,
        min_voltage: float = 3.0,
        max_voltage: float = 4.2,
        smoothing_samples: int = 5,
        update_interval: float = 30.0,
        pisugar_socket: str = "/tmp/pisugar-server.sock",
        ina219_bus: int = 1,
        ina219_address: int = 0x43,
        ina219_shunt_ohms: float = 0.1,
        battery_capacity_mah: int = 4000,
        charge_current_threshold_ma: float = 20.0,
        discharge_current_threshold_ma: float = -20.0,
        soc_state_file: str = "data/battery_state.json",
        charge_calibrated_capacity_mah: float = 4142.5,
        discharge_calibrated_capacity_mah: float = 4563.7,
        soc_persist_step: float = 0.5,
        soc_max_integration_gap: float = 120.0,
        low_battery_threshold: int = 20,
        backend_preference: str = "auto",
        backend_retry_interval: float = 15.0,
        soc_persist_interval: float = 600.0,
        soc_state_stale_age: float = 21600.0,
        soc_restore_max_delta: float = 30.0,
    ):
        self.logger = logging.getLogger(__name__)
        self.smoothing_samples = max(1, int(smoothing_samples))
        self.low_battery_threshold = int(low_battery_threshold)
        self.update_interval = float(update_interval)

        aliases = {
            "waveshare": "waveshare_ups_hat_c",
            "waveshare_ups_hat_c": "waveshare_ups_hat_c",
            "pisugar": "pisugar2",
            "pisugar2": "pisugar2",
            "ads1115": "ads1115",
            "mock": "mock",
            "auto": "auto",
        }
        requested_backend = str(
            backend_preference or "auto"
        ).strip().lower()

        if requested_backend not in aliases:
            raise ValueError(
                f"Backend de bateria desconhecido: {requested_backend}"
            )

        self.backend_preference = aliases[requested_backend]
        self.backend_retry_interval = max(
            5.0,
            float(backend_retry_interval),
        )
        self._last_backend_retry_monotonic = 0.0
        self.voltage_buffer = deque(maxlen=self.smoothing_samples)
        self.last_update = 0.0
        self._cached_voltage: Optional[float] = None
        self._cached_percentage: Optional[int] = None
        self._cached_charging: Optional[bool] = None
        self._cached_current_ma: Optional[float] = None
        self._cached_backend_voltage: Optional[float] = None

        self.soc_state_file = Path(soc_state_file).expanduser()
        self.charge_calibrated_capacity_mah = float(
            charge_calibrated_capacity_mah
        )
        self.discharge_calibrated_capacity_mah = float(
            discharge_calibrated_capacity_mah
        )
        self.soc_persist_step = max(0.1, float(soc_persist_step))
        self.soc_persist_interval = max(
            60.0,
            float(soc_persist_interval),
        )
        self.soc_state_stale_age = max(
            0.0,
            float(soc_state_stale_age),
        )
        self.soc_restore_max_delta = max(
            10.0,
            float(soc_restore_max_delta),
        )
        self.soc_max_integration_gap = max(
            10.0,
            float(soc_max_integration_gap),
        )

        if self.charge_calibrated_capacity_mah <= 0:
            raise ValueError("Capacidade calibrada de carga inválida")
        if self.discharge_calibrated_capacity_mah <= 0:
            raise ValueError("Capacidade calibrada de descarga inválida")

        self._restored_charging_state: Optional[bool] = None
        self._restored_soc_age_seconds: Optional[float] = None
        self._restored_soc_needs_validation = False
        self._soc_percent: Optional[float] = self._load_soc_state()

        if (
            self._restored_soc_age_seconds is not None
            and self._restored_soc_age_seconds
            >= self.soc_state_stale_age
        ):
            self.logger.warning(
                "Persisted charging state ignored because battery "
                "state is stale (age=%.0fs)",
                self._restored_soc_age_seconds,
            )
            self._restored_charging_state = None

        # Fonte de alimentação separada do simples estado "a carregar".
        # O estado persistido é apenas uma pista inicial; uma leitura de
        # corrente inequívoca substitui-o imediatamente.
        if self._restored_charging_state is True:
            self._power_source_state: Optional[str] = "mains"
        elif self._restored_charging_state is False:
            self._power_source_state = "battery"
        else:
            self._power_source_state = None
        self._last_persisted_soc: Optional[float] = self._soc_percent
        self._last_persist_monotonic = time.monotonic()
        self._previous_soc_monotonic: Optional[float] = None
        self._previous_soc_current: Optional[float] = None

        self._backend_detection_args = {
            "ina219_bus": ina219_bus,
            "ina219_address": ina219_address,
            "ina219_shunt_ohms": ina219_shunt_ohms,
            "battery_capacity_mah": battery_capacity_mah,
            "charge_current_threshold_ma": charge_current_threshold_ma,
            "discharge_current_threshold_ma": discharge_current_threshold_ma,
            "pisugar_socket": pisugar_socket,
            "adc_channel": adc_channel,
            "voltage_divider_ratio": voltage_divider_ratio,
            "min_voltage": min_voltage,
            "max_voltage": max_voltage,
        }

        self.backend = self._detect_backend(
            **self._backend_detection_args
        )

        if (
            isinstance(self.backend, WaveshareUPSHatCBackend)
            and self._restored_charging_state is not None
        ):
            self.backend._charging_state = self._restored_charging_state

        if isinstance(self.backend, UnavailableBackend):
            self._last_backend_retry_monotonic = time.monotonic()

        self.logger.info("Battery backend: %s", self.backend.get_name())
        self._update_reading()

    def _recover_backend_if_needed(self) -> bool:
        """Retry a temporarily unavailable physical battery backend."""
        if not isinstance(self.backend, UnavailableBackend):
            return False

        now = time.monotonic()
        if (
            now - self._last_backend_retry_monotonic
            < self.backend_retry_interval
        ):
            return False

        self._last_backend_retry_monotonic = now

        recovered = self._detect_backend(
            **self._backend_detection_args
        )

        if isinstance(recovered, UnavailableBackend):
            return False

        self.backend = recovered
        self.voltage_buffer.clear()
        self._cached_voltage = None
        self._cached_backend_voltage = None
        self._cached_current_ma = None
        self._cached_charging = None
        self._previous_soc_monotonic = None
        self._previous_soc_current = None
        self._power_source_state = None

        if (
            isinstance(self.backend, WaveshareUPSHatCBackend)
            and self._restored_charging_state is not None
        ):
            self.backend._charging_state = (
                self._restored_charging_state
            )

        self.logger.warning(
            "Battery backend recovered: %s",
            self.backend.get_name(),
        )
        return True

    def _detect_backend(
        self,
        ina219_bus: int,
        ina219_address: int,
        ina219_shunt_ohms: float,
        battery_capacity_mah: int,
        charge_current_threshold_ma: float,
        discharge_current_threshold_ma: float,
        pisugar_socket: str,
        adc_channel: int,
        voltage_divider_ratio: float,
        min_voltage: float,
        max_voltage: float,
    ) -> BatteryBackend:
        """Detect the configured real battery backend.

        Mock is only used when explicitly requested. A missing real backend is
        represented as Unavailable so fabricated battery data can never be
        mistaken for a physical measurement.
        """
        preference = self.backend_preference

        if preference == "mock":
            self.logger.warning(
                "Battery backend explicitly configured as Mock"
            )
            return MockBackend()

        if preference in ("auto", "waveshare_ups_hat_c"):
            # I2C may still be stabilising immediately after a service restart.
            attempts = 1 if smbus is None else 8

            for attempt in range(1, attempts + 1):
                waveshare = WaveshareUPSHatCBackend(
                    i2c_bus=ina219_bus,
                    address=ina219_address,
                    shunt_ohms=ina219_shunt_ohms,
                    smoothing_samples=self.smoothing_samples,
                    charging_threshold_ma=charge_current_threshold_ma,
                    discharging_threshold_ma=discharge_current_threshold_ma,
                    battery_capacity_mah=battery_capacity_mah,
                )

                if waveshare.is_available():
                    if attempt > 1:
                        self.logger.info(
                            "Waveshare UPS HAT (C) detetado na tentativa %d/%d",
                            attempt,
                            attempts,
                        )
                    return waveshare

                waveshare.close()

                if attempt < attempts:
                    if attempt == 1:
                        self.logger.warning(
                            "UPS HAT (C) não respondeu na primeira tentativa; "
                            "a repetir deteção I2C"
                        )
                    time.sleep(0.25)

            if preference == "waveshare_ups_hat_c":
                self.logger.error(
                    "Waveshare UPS HAT (C) configurado mas indisponível"
                )
                return UnavailableBackend(preference)

        if preference in ("auto", "pisugar2"):
            pisugar = PiSugar2Backend(pisugar_socket)
            if pisugar.is_available():
                return pisugar

            if preference == "pisugar2":
                self.logger.error(
                    "PiSugar2 configurado mas indisponível"
                )
                return UnavailableBackend(preference)

        if preference in ("auto", "ads1115"):
            ads1115 = ADS1115Backend(
                adc_channel=adc_channel,
                voltage_divider_ratio=voltage_divider_ratio,
                min_voltage=min_voltage,
                max_voltage=max_voltage,
            )
            if ads1115.is_available():
                return ads1115

            if preference == "ads1115":
                self.logger.error(
                    "ADS1115 configurado mas indisponível"
                )
                return UnavailableBackend(preference)

        self.logger.error(
            "Nenhum hardware real de bateria detetado; "
            "monitor em estado Unavailable"
        )
        return UnavailableBackend(preference)

    def _load_soc_state(self) -> Optional[float]:
        try:
            if not self.soc_state_file.exists():
                return None
            data = json.loads(
                self.soc_state_file.read_text(encoding="utf-8")
            )
            soc = float(data["soc"])
            if not 0.0 <= soc <= 100.0:
                raise ValueError(f"SOC fora do intervalo: {soc}")

            updated_at = data.get("updated_at")
            if updated_at is not None:
                try:
                    age = time.time() - float(updated_at)
                    if age >= 0.0:
                        self._restored_soc_age_seconds = age
                except (TypeError, ValueError):
                    pass

            restored_charging = data.get("charging")
            if isinstance(restored_charging, bool):
                self._restored_charging_state = restored_charging

            self._restored_soc_needs_validation = True

            self.logger.info(
                "Battery SOC restored from %s: %.2f%%",
                self.soc_state_file,
                soc,
            )
            return soc
        except Exception as exc:
            self.logger.warning(
                "Battery SOC state ignored (%s): %s",
                self.soc_state_file,
                exc,
            )
            return None

    def _persist_soc_state(
        self,
        *,
        voltage: float,
        current_ma: float,
        charging: bool,
        force: bool = False,
    ) -> None:
        if self._soc_percent is None:
            return

        now_mono = time.monotonic()

        if (
            not force
            and self._last_persisted_soc is not None
            and abs(self._soc_percent - self._last_persisted_soc)
            < self.soc_persist_step
            and now_mono - self._last_persist_monotonic
            < self.soc_persist_interval
        ):
            return

        try:
            self.soc_state_file.parent.mkdir(
                parents=True,
                exist_ok=True,
            )
            temp_path = self.soc_state_file.with_suffix(".json.tmp")
            payload = {
                "soc": round(self._soc_percent, 4),
                "updated_at": time.time(),
                "voltage_v": round(float(voltage), 4),
                "current_ma": round(float(current_ma), 2),
                "charging": bool(charging),
                "source": "coulomb_counter",
            }
            temp_path.write_text(
                json.dumps(payload, indent=2) + "\n",
                encoding="utf-8",
            )
            temp_path.replace(self.soc_state_file)
            self._last_persisted_soc = self._soc_percent
            self._last_persist_monotonic = now_mono
        except Exception as exc:
            self.logger.warning(
                "Failed to persist battery SOC: %s",
                exc,
            )

    def _validate_restored_soc(
        self,
        fallback_percentage: int,
    ) -> bool:
        """Validate persisted SOC against the first real battery reading."""
        if (
            not self._restored_soc_needs_validation
            or self._soc_percent is None
        ):
            return False

        self._restored_soc_needs_validation = False

        voltage_soc = float(
            max(0, min(100, int(fallback_percentage)))
        )
        delta = abs(self._soc_percent - voltage_soc)

        stale = (
            self._restored_soc_age_seconds is not None
            and self._restored_soc_age_seconds
            >= self.soc_state_stale_age
        )
        inconsistent = delta >= self.soc_restore_max_delta

        if not stale and not inconsistent:
            self.logger.info(
                "Restored battery SOC validated: %.2f%% "
                "(voltage estimate %.0f%%)",
                self._soc_percent,
                voltage_soc,
            )
            return False

        old_soc = self._soc_percent
        self._soc_percent = voltage_soc
        self._last_persisted_soc = None

        reason = "stale" if stale else "inconsistent"
        self.logger.warning(
            "Restored battery SOC %s: %.2f%% -> %.0f%% "
            "(age=%s, delta=%.1f)",
            reason,
            old_soc,
            voltage_soc,
            (
                f"{self._restored_soc_age_seconds:.0f}s"
                if self._restored_soc_age_seconds is not None
                else "unknown"
            ),
            delta,
        )
        return True

    def _update_soc(
        self,
        *,
        voltage: float,
        current_ma: float,
        charging: bool,
        fallback_percentage: int,
    ) -> None:
        now_mono = time.monotonic()
        force_persist = self._validate_restored_soc(
            fallback_percentage
        )

        if self._soc_percent is None:
            self._soc_percent = float(
                max(0, min(100, int(fallback_percentage)))
            )
            force_persist = True

        if (
            self._previous_soc_monotonic is not None
            and self._previous_soc_current is not None
        ):
            dt_seconds = now_mono - self._previous_soc_monotonic

            if 0.0 < dt_seconds <= self.soc_max_integration_gap:
                avg_current = (
                    self._previous_soc_current + current_ma
                ) / 2.0
                delta_mah = avg_current * dt_seconds / 3600.0

                if avg_current >= self.backend.charging_threshold_ma:
                    self._soc_percent += (
                        delta_mah
                        / self.charge_calibrated_capacity_mah
                        * 100.0
                    )
                elif avg_current <= self.backend.discharging_threshold_ma:
                    self._soc_percent += (
                        delta_mah
                        / self.discharge_calibrated_capacity_mah
                        * 100.0
                    )

        # Não forçar SOC=0 pela tensão.
        # O limite crítico de 3.50 V pertence ao BatterySafetyController;
        # o SOC deve continuar a representar apenas o contador de coulombs.
        # Âncora superior: depois de carga real, o UPS reduz a corrente
        # para a zona neutra mantendo a tensão elevada.
        if (
            charging
            and self._soc_percent is not None
            and self._soc_percent >= 95.0
            and self.backend.discharging_threshold_ma
            < current_ma
            < self.backend.charging_threshold_ma
            and voltage >= 4.10
        ):
            if self._soc_percent != 100.0:
                self._soc_percent = 100.0
                force_persist = True

        self._soc_percent = max(
            0.0,
            min(100.0, self._soc_percent),
        )

        self._previous_soc_monotonic = now_mono
        self._previous_soc_current = current_ma

        self._persist_soc_state(
            voltage=voltage,
            current_ma=current_ma,
            charging=charging,
            force=force_persist,
        )

    def _update_power_source_state(
        self,
        current_ma: Optional[float],
        charging: bool,
    ) -> None:
        """Update mains/battery source without altering charging logic."""
        if isinstance(self.backend, WaveshareUPSHatCBackend):
            if current_ma is None:
                return

            if current_ma >= self.backend.charging_threshold_ma:
                self._power_source_state = "mains"
            elif current_ma <= self.backend.discharging_threshold_ma:
                self._power_source_state = "battery"

            # Neutral current preserves the previous known state.
            return

        self._power_source_state = (
            "mains" if charging else "battery"
        )

    def _update_reading(self) -> None:
        if isinstance(self.backend, UnavailableBackend):
            self._recover_backend_if_needed()

            if isinstance(self.backend, UnavailableBackend):
                return

        if isinstance(self.backend, WaveshareUPSHatCBackend):
            try:
                voltage, current_ma = self.backend._sample(force=True)
            except Exception as exc:
                failed_backend = self.backend.get_name()
                self.backend = UnavailableBackend(
                    self.backend_preference
                )
                self._last_backend_retry_monotonic = time.monotonic()
                self.logger.error(
                    "Battery backend lost: %s (%s)",
                    failed_backend,
                    exc,
                )
                return

            fallback_percentage = (
                self.backend.voltage_to_percentage(voltage)
                if voltage > 0
                else 0
            )
            charging = self.backend._charging_from_current(current_ma)

            self.voltage_buffer.append(voltage)
            self._cached_voltage = (
                sum(self.voltage_buffer) / len(self.voltage_buffer)
            )
            self._cached_backend_voltage = voltage
            self._cached_current_ma = current_ma
            self._cached_charging = charging
            self._update_power_source_state(
                current_ma=current_ma,
                charging=charging,
            )

            self._update_soc(
                voltage=self._cached_voltage,
                current_ma=current_ma,
                charging=charging,
                fallback_percentage=fallback_percentage,
            )
            self._cached_percentage = int(
                round(self._soc_percent or 0.0)
            )
        else:
            try:
                if not self.backend.is_available():
                    raise OSError("backend reports unavailable")

                voltage = float(self.backend.read_voltage())
                percentage = int(self.backend.read_percentage())
                charging = bool(self.backend.is_charging())

                if not 2.0 <= voltage <= 5.0:
                    raise OSError(
                        f"invalid battery voltage: {voltage:.3f} V"
                    )

                if not 0 <= percentage <= 100:
                    raise OSError(
                        f"invalid battery percentage: {percentage}"
                    )

            except Exception as exc:
                failed_backend = self.backend.get_name()
                self.backend = UnavailableBackend(
                    self.backend_preference
                )
                self._last_backend_retry_monotonic = time.monotonic()
                self.logger.error(
                    "Battery backend lost: %s (%s)",
                    failed_backend,
                    exc,
                )
                return

            self.voltage_buffer.append(voltage)
            self._cached_voltage = (
                sum(self.voltage_buffer) / len(self.voltage_buffer)
            )
            self._cached_percentage = percentage
            self._cached_charging = charging
            self._update_power_source_state(
                current_ma=None,
                charging=charging,
            )

        self.last_update = time.time()

    def _update_if_needed(self) -> None:
        if time.time() - self.last_update >= self.update_interval:
            self._update_reading()

    def get_voltage(self) -> float:
        self._update_if_needed()
        return self._cached_voltage if self._cached_voltage is not None else 0.0

    def get_percentage(self) -> int:
        self._update_if_needed()
        return self._cached_percentage if self._cached_percentage is not None else 0

    def is_charging(self) -> bool:
        self._update_if_needed()
        return self._cached_charging if self._cached_charging is not None else False

    def get_power_source(self) -> str:
        """Return mains, battery or unknown."""
        self._update_if_needed()
        return self._power_source_state or "unknown"

    def force_update(self) -> None:
        self._update_reading()

    def persist_soc_state(self, *, refresh: bool = False) -> None:
        """Persist the exact current SOC only from a valid coulomb source."""
        if refresh:
            self._update_reading()

        if not isinstance(self.backend, WaveshareUPSHatCBackend):
            self.logger.warning(
                "Battery SOC persistence skipped: backend unavailable "
                "or not a Waveshare coulomb source"
            )
            return

        if not self.backend.is_available():
            self.logger.warning(
                "Battery SOC persistence skipped: Waveshare unavailable"
            )
            return

        if self._soc_percent is None:
            return

        self._persist_soc_state(
            voltage=float(self._cached_voltage or 0.0),
            current_ma=float(self._cached_current_ma or 0.0),
            charging=bool(self._cached_charging),
            force=True,
        )

    def is_low_battery(self, threshold: Optional[int] = None) -> bool:
        if threshold is None:
            threshold = self.low_battery_threshold
        return self.get_percentage() <= int(threshold)

    def get_status(self) -> dict:
        unavailable = isinstance(
            self.backend,
            UnavailableBackend,
        )

        if unavailable:
            percentage = self._cached_percentage
            voltage = self._cached_voltage
            charging = self._cached_charging

            return {
                "voltage": voltage,
                "percentage": percentage,
                "is_charging": charging,
                "is_low": (
                    percentage <= self.low_battery_threshold
                    if percentage is not None
                    else None
                ),
                "backend": self.backend.get_name(),
                "safety_mode": getattr(self.backend, "safety_mode", "none"),
                "hardware_available": False,
                "measurement_available": voltage is not None,
                "measurement_stale": True,
                "last_update": self.last_update or None,
            }

        status = {
            "voltage": self.get_voltage(),
            "percentage": self.get_percentage(),
            "is_charging": self.is_charging(),
            "is_low": self.is_low_battery(),
            "backend": self.backend.get_name(),
            "safety_mode": getattr(self.backend, "safety_mode", "none"),
            "hardware_available": self.hardware_available,
            "measurement_available": True,
            "measurement_stale": False,
            "last_update": self.last_update,
        }

        if isinstance(self.backend, WaveshareUPSHatCBackend):
            current_ma = float(self._cached_current_ma or 0.0)
            percentage = self.get_percentage()
            remaining_hours = None

            if (
                current_ma <= self.backend.discharging_threshold_ma
                and percentage > 0
            ):
                remaining_mah = (
                    self.discharge_calibrated_capacity_mah
                    * percentage
                    / 100.0
                )
                remaining_hours = remaining_mah / abs(current_ma)

            backend_voltage = float(
                self._cached_backend_voltage
                if self._cached_backend_voltage is not None
                else self.get_voltage()
            )

            status.update({
                "voltage": backend_voltage,
                "percentage_voltage": (
                    self.backend.voltage_to_percentage(backend_voltage)
                    if backend_voltage > 0
                    else 0
                ),
                "current_ma": current_ma,
                "power_w": backend_voltage * current_ma / 1000.0,
                "remaining_hours_estimate": remaining_hours,
                "capacity_mah": self.backend.battery_capacity_mah,
                "charge_calibrated_capacity_mah": (
                    self.charge_calibrated_capacity_mah
                ),
                "discharge_calibrated_capacity_mah": (
                    self.discharge_calibrated_capacity_mah
                ),
                "address": self.backend.address,
                "soc_precise": (
                    round(self._soc_percent, 3)
                    if self._soc_percent is not None
                    else None
                ),
            })

        return status

    @property
    def hardware_available(self) -> bool:
        return not isinstance(self.backend, MockBackend)

    def get_time(self):
        if isinstance(self.backend, PiSugar2Backend):
            try:
                rtc = self.backend.get_rtc_time()
                if rtc:
                    return datetime.fromisoformat(rtc[:19])
            except Exception:
                pass
        return datetime.now()
