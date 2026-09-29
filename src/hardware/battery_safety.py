"""Voltage/current based battery safety for PiBook."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BatterySafetyConfig:
    warning_voltage: float = 3.62
    critical_voltage: float = 3.50
    emergency_voltage: float = 3.35
    rearm_voltage: float = 3.70
    discharge_current_threshold_ma: float = -20.0
    charge_current_threshold_ma: float = 20.0
    warning_consecutive_readings: int = 2
    critical_consecutive_readings: int = 3


class BatterySafetyController:
    """Debounced safety state independent from displayed battery SOC."""

    def __init__(self, config: BatterySafetyConfig):
        if not (
            0.0
            < config.emergency_voltage
            < config.critical_voltage
            < config.warning_voltage
            < config.rearm_voltage
        ):
            raise ValueError("Limiares de tensão inválidos")

        if config.warning_consecutive_readings < 1:
            raise ValueError("warning_consecutive_readings tem de ser >= 1")

        if config.critical_consecutive_readings < 1:
            raise ValueError("critical_consecutive_readings tem de ser >= 1")

        if config.discharge_current_threshold_ma >= 0:
            raise ValueError("O limiar de descarga tem de ser negativo")

        if config.charge_current_threshold_ma <= 0:
            raise ValueError("O limiar de carga tem de ser positivo")

        self.config = config
        self.warning_count = 0
        self.critical_count = 0
        self.warning_latched = False
        self.critical_latched = False

    def _reset_counts(self) -> None:
        self.warning_count = 0
        self.critical_count = 0

    def _rearm(self) -> str:
        had_alert = self.warning_latched or self.critical_latched
        self.warning_latched = False
        self.critical_latched = False
        self._reset_counts()
        return "rearmed" if had_alert else "none"

    def update_voltage_only(
        self,
        *,
        voltage: float,
        external_power: bool | None = None,
        allow_critical: bool = False,
    ) -> str:
        """Safety path for backends without battery current measurement."""
        voltage = float(voltage)

        if not 2.5 <= voltage <= 4.6:
            self._reset_counts()
            return "none"

        # Known external power means the battery is not in an unsafe
        # discharge state, so clear any latched warning/critical state.
        if external_power is True:
            return self._rearm()

        if voltage >= self.config.rearm_voltage:
            return self._rearm()

        if voltage <= self.config.warning_voltage:
            self.warning_count += 1
        else:
            self.warning_count = 0

        # Without a reliable indication that the battery is actually
        # discharging, never make a destructive decision.
        if not allow_critical:
            self.critical_count = 0

            if (
                self.warning_count
                >= self.config.warning_consecutive_readings
                and not self.warning_latched
            ):
                self.warning_latched = True
                return "warning"

            return "none"

        if voltage <= self.config.emergency_voltage:
            if not self.critical_latched:
                self.critical_latched = True
                self.warning_latched = True
                return "critical"
            return "none"

        if voltage <= self.config.critical_voltage:
            self.critical_count += 1
        else:
            self.critical_count = 0

        if (
            self.critical_count
            >= self.config.critical_consecutive_readings
            and not self.critical_latched
        ):
            self.critical_latched = True
            self.warning_latched = True
            return "critical"

        if (
            self.warning_count
            >= self.config.warning_consecutive_readings
            and not self.warning_latched
        ):
            self.warning_latched = True
            return "warning"

        return "none"

    def update(self, *, voltage: float, current_ma: float) -> str:
        """
        Return one event:
        - none
        - warning
        - critical
        - rearmed
        """

        voltage = float(voltage)
        current_ma = float(current_ma)

        # Ignore impossible/failed readings.
        if not 2.5 <= voltage <= 4.6:
            self._reset_counts()
            return "none"

        # A clearly positive current means the charger is supplying the cell.
        if current_ma >= self.config.charge_current_threshold_ma:
            return self._rearm()

        # At a healthy recovered voltage the alert may be armed again.
        if voltage >= self.config.rearm_voltage:
            return self._rearm()

        # Near zero current is ambiguous. Never make a destructive decision.
        if current_ma > self.config.discharge_current_threshold_ma:
            self._reset_counts()
            return "none"

        # Emergency floor: one valid, clearly discharging reading is enough.
        if voltage <= self.config.emergency_voltage:
            if not self.critical_latched:
                self.critical_latched = True
                self.warning_latched = True
                return "critical"
            return "none"

        if voltage <= self.config.critical_voltage:
            self.critical_count += 1
        else:
            self.critical_count = 0

        if voltage <= self.config.warning_voltage:
            self.warning_count += 1
        else:
            self.warning_count = 0

        # Critical is evaluated before warning so a deep drop does not delay it.
        if (
            self.critical_count
            >= self.config.critical_consecutive_readings
            and not self.critical_latched
        ):
            self.critical_latched = True
            self.warning_latched = True
            return "critical"

        if (
            self.warning_count
            >= self.config.warning_consecutive_readings
            and not self.warning_latched
        ):
            self.warning_latched = True
            return "warning"

        return "none"
