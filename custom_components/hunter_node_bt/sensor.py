"""Sensor platform for Hunter NODE-BT."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import CONTROLLER_STATES, DOMAIN
from .coordinator import HunterConfigEntry, HunterCoordinator
from .protocol import from_controller_time, next_water_time
from .schedule import PROGRAM_LETTERS, format_start_time

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HunterConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Hunter NODE-BT sensors."""
    coordinator = entry.runtime_data

    entities: list[SensorEntity] = [
        HunterBatterySensor(coordinator),
        HunterControllerStateSensor(coordinator),
        HunterNextWaterTimeSensor(coordinator),
        HunterRainDelayUntilSensor(coordinator),
        HunterLastRunSensor(coordinator),
        HunterActiveSeasonAdjustSensor(coordinator),
        HunterControllerDateSensor(
            coordinator, "BatteryChangeDate", "battery_changed", "Battery Changed", "mdi:battery-sync"
        ),
        HunterControllerDateSensor(
            coordinator, "SettingsChangeDate", "settings_changed", "Settings Changed", "mdi:cog-sync"
        ),
    ]
    entities.extend(HunterProgramScheduleSensor(coordinator, letter) for letter in PROGRAM_LETTERS)

    # MoistureSensor reads 100 with no probe attached, so only trust the setting
    ctrl_data = coordinator.data.get("Controller", {})
    if ctrl_data.get("SoilSensorEnabled"):
        entities.append(HunterSoilMoistureSensor(coordinator))

    # Add remaining run time sensor for each station
    count = coordinator.station_count or 1
    for i in range(1, count + 1):
        entities.append(HunterStationRemainingSensor(coordinator, i))

    async_add_entities(entities)


class HunterBaseSensor(CoordinatorEntity[HunterCoordinator], SensorEntity):
    """Base class for Hunter sensors."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: HunterCoordinator) -> None:
        """Initialize sensor."""
        super().__init__(coordinator)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device registry info."""
        ctrl = self.coordinator.controller
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.address)},
            name=self.coordinator.entry.title or "Hunter NODE-BT",
            manufacturer="Hunter Industries",
            model=f"NODE-BT ({self.coordinator.station_count} Station)",
            sw_version=str(ctrl.get("Version", "")) if ctrl.get("Version") else None,
            hw_version=str(ctrl.get("BTVersion", "")) if ctrl.get("BTVersion") else None,
            serial_number=str(ctrl.get("SerialNumber")) if ctrl.get("SerialNumber") else None,
        )


class HunterBatterySensor(HunterBaseSensor):
    """Battery percentage sensor."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator: HunterCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_battery"
        self._attr_name = "Battery"

    @property
    def native_value(self) -> int | None:
        """Return current battery percentage."""
        sensor = self.coordinator.data.get("Sensor", {})
        return sensor.get("Battery")


class HunterSoilMoistureSensor(HunterBaseSensor):
    """Soil moisture percentage sensor."""

    _attr_device_class = SensorDeviceClass.MOISTURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator: HunterCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_moisture"
        self._attr_name = "Soil Moisture"

    @property
    def native_value(self) -> int | None:
        """Return current moisture percentage."""
        state = self.coordinator.data.get("State", {})
        val = state.get("SoilSensorPercentage")
        if val is not None and val > 0:
            return val
        sensor = self.coordinator.data.get("Sensor", {})
        return sensor.get("MoistureSensor")


class HunterControllerStateSensor(HunterBaseSensor):
    """Controller operational state sensor."""

    _attr_icon = "mdi:state-machine"

    def __init__(self, coordinator: HunterCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_state"
        self._attr_name = "Controller State"

    @property
    def native_value(self) -> str:
        """Return state description."""
        state = self.coordinator.data.get("State", {})
        raw_state = state.get("ControllerState", 0)
        return CONTROLLER_STATES.get(raw_state, f"unknown_{raw_state}")


class HunterNextWaterTimeSensor(HunterBaseSensor):
    """Next scheduled watering timestamp sensor."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: HunterCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_next_water_time"
        self._attr_name = "Next Water Time"

    @property
    def native_value(self) -> datetime | None:
        """Return the next watering start.

        The controller only reports the start as minutes after midnight, so the
        date is the next occurrence of that time of day, after the rain delay
        ends when one is active. None while the controller is off.
        """
        if self.coordinator.controller.get("ControllerOff"):
            return None
        state = self.coordinator.data.get("State", {})
        after = dt_util.now()
        if (until := self.coordinator.rain_delay_until) is not None and until > after:
            after = until
        return next_water_time(state.get("NextWaterTime"), after)


class HunterStationRemainingSensor(HunterBaseSensor):
    """Remaining watering time for a specific station."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: HunterCoordinator, station_number: int) -> None:
        super().__init__(coordinator)
        self.station_number = station_number
        self._attr_unique_id = f"{coordinator.address}_station_{station_number}_remaining"

        station_cfg = coordinator.stations.get(station_number, {})
        configured_name = station_cfg.get("Name")
        if configured_name and configured_name.strip():
            self._attr_name = f"{configured_name.strip()} Remaining Time"
        else:
            self._attr_name = f"Station {station_number} Remaining Time"

    @property
    def native_value(self) -> int:
        """Return remaining run time in seconds."""
        state = self.coordinator.data.get("State", {})
        st_data = state.get(f"Station_{self.station_number}", {})
        return st_data.get("Remaining", 0)


class HunterRainDelayUntilSensor(HunterBaseSensor):
    """Time scheduled watering resumes after a rain delay."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:weather-pouring"

    def __init__(self, coordinator: HunterCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_rain_delay_until"
        self._attr_name = "Rain Delay Until"

    @property
    def native_value(self) -> datetime | None:
        """Return when the rain delay ends, or None when inactive."""
        return self.coordinator.rain_delay_until


class HunterLastRunSensor(HunterBaseSensor):
    """Start time of the last watering run."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:history"

    def __init__(self, coordinator: HunterCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_last_run"
        self._attr_name = "Last Run"

    @property
    def _last_run(self) -> dict:
        return self.coordinator.data.get("LastRun") or {}

    @property
    def native_value(self) -> datetime | None:
        """Return when the last run started."""
        return from_controller_time(
            self._last_run.get("StartTime"), dt_util.get_default_time_zone()
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return station, duration and raw start/stop event codes."""
        last_run = self._last_run
        station = last_run.get("Station")
        station_cfg = self.coordinator.stations.get(station, {}) if station else {}
        return {
            "station_number": station,
            "station_name": station_cfg.get("Name"),
            "run_time_seconds": last_run.get("RunTime"),
            "start_event": last_run.get("StartEvent"),
            "stop_event": last_run.get("StopEvent"),
        }


class HunterActiveSeasonAdjustSensor(HunterBaseSensor):
    """Seasonal adjustment % applied to run times right now."""

    _attr_icon = "mdi:sun-thermometer-outline"
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator: HunterCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_season_adjust_active"
        self._attr_name = "Active Season Adjust"

    @property
    def native_value(self) -> int | None:
        """Return the monthly entry when the table is on, else the global value."""
        return self.coordinator.season_adjust_active

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return where the value comes from."""
        by_month = bool(self.coordinator.controller.get("SeasonAdjustByMonthEnable"))
        return {"source": "monthly" if by_month else "global"}


class HunterProgramScheduleSensor(HunterBaseSensor):
    """Summary of a program, e.g. 'Every day at 06:30 (10 min)', with the details as attributes."""

    _attr_icon = "mdi:calendar-sync"

    def __init__(self, coordinator: HunterCoordinator, letter: str) -> None:
        super().__init__(coordinator)
        self.letter = letter
        self._attr_unique_id = f"{coordinator.address}_program_{letter.lower()}_schedule"
        self._attr_name = f"Program {letter} Schedule"

    @property
    def available(self) -> bool:
        """Only available once the controller has reported this program."""
        return super().available and self.letter in self.coordinator.programs

    @property
    def native_value(self) -> str:
        """Return the one-line summary ('Off' when the program never waters)."""
        return self.coordinator.program(self.letter).summary()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return start times (HH:MM), water days and run minutes per station."""
        program = self.coordinator.program(self.letter)
        return {
            "program_name": program.name,
            "active": program.is_active,
            "start_times": [format_start_time(minutes) for minutes in program.start_times],
            "water_days": program.water_days_text,
            "run_minutes": {
                station: round(seconds / 60, 1) for station, seconds in program.run_times.items()
            },
            "total_minutes_per_start": round(program.run_seconds_per_start / 60, 1),
        }


class HunterControllerDateSensor(HunterBaseSensor):
    """A date the controller records, such as when the battery was last changed."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: HunterCoordinator, field: str, key: str, name: str, icon: str
    ) -> None:
        super().__init__(coordinator)
        self.field = field
        self._attr_unique_id = f"{coordinator.address}_{key}"
        self._attr_name = name
        self._attr_icon = icon

    @property
    def native_value(self) -> datetime | None:
        """Return the recorded date (None when the controller has none)."""
        return from_controller_time(
            self.coordinator.controller.get(self.field), dt_util.get_default_time_zone()
        )
