"""Number platform for Hunter NODE-BT."""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode, RestoreNumber
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MAX_RAIN_DELAY_DAYS, MAX_RUN_TIME_MIN, MAX_SEASON_ADJUST
from .coordinator import HunterConfigEntry, HunterCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HunterConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Hunter NODE-BT number entities."""
    coordinator = entry.runtime_data
    entities: list[NumberEntity] = [
        HunterRainDelayNumber(coordinator),
        HunterSeasonAdjustNumber(coordinator),
    ]
    count = coordinator.station_count or 1
    entities.extend(
        HunterStationRunTimeNumber(coordinator, station_num)
        for station_num in range(1, count + 1)
    )
    async_add_entities(entities)


class HunterBaseNumber(CoordinatorEntity[HunterCoordinator], NumberEntity):
    """Base number entity for Hunter NODE-BT."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: HunterCoordinator) -> None:
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


class HunterRainDelayNumber(HunterBaseNumber):
    """Rain delay in days: setting N suspends scheduled watering, 0 cancels it."""

    _attr_icon = "mdi:weather-pouring"
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_max_value = MAX_RAIN_DELAY_DAYS
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.DAYS

    def __init__(self, coordinator: HunterCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_rain_delay_days"
        self._attr_name = "Rain Delay"

    @property
    def native_value(self) -> int:
        """Return days left on the rain delay."""
        return self.coordinator.rain_delay_days

    async def async_set_native_value(self, value: float) -> None:
        """Start (or cancel, with 0) a rain delay of the given number of days."""
        await self.coordinator.async_set_rain_delay(int(value))


class HunterSeasonAdjustNumber(HunterBaseNumber):
    """Global seasonal adjustment %; setting it turns the monthly table off."""

    _attr_icon = "mdi:sun-thermometer-outline"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_max_value = MAX_SEASON_ADJUST
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator: HunterCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_season_adjust"
        self._attr_name = "Season Adjust"

    @property
    def native_value(self) -> int | None:
        """Return the % in force: this month's table entry while the monthly table is on.

        The global value is ignored by the controller while the monthly table is on,
        so showing it (e.g. 100 % when November waters at 10 %) was misleading.
        """
        return self.coordinator.season_adjust_active

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "global_season_adjust": self.coordinator.controller.get("SeasonAdjust"),
            "by_month": bool(self.coordinator.controller.get("SeasonAdjustByMonthEnable")),
        }

    async def async_set_native_value(self, value: float) -> None:
        """Set the global seasonal adjustment %."""
        await self.coordinator.async_set_season_adjust(int(value))


class HunterStationRunTimeNumber(HunterBaseNumber, RestoreNumber):
    """Run time used when a station's valve is opened from Home Assistant.

    Stored in Home Assistant only; the controller has no per-station manual time.
    """

    _attr_icon = "mdi:timer-outline"
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 1
    _attr_native_max_value = MAX_RUN_TIME_MIN
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    def __init__(self, coordinator: HunterCoordinator, station_number: int) -> None:
        super().__init__(coordinator)
        self.station_number = station_number
        self._attr_unique_id = f"{coordinator.address}_station_{station_number}_run_time"

        station_cfg = coordinator.stations.get(station_number, {})
        configured_name = station_cfg.get("Name")
        if configured_name and configured_name.strip():
            self._attr_name = f"{configured_name.strip()} Run Time"
        else:
            self._attr_name = f"Station {station_number} Run Time"

    async def async_added_to_hass(self) -> None:
        """Restore the last run time chosen for this station."""
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            minutes = int(last.native_value)
            if 1 <= minutes <= MAX_RUN_TIME_MIN:
                self.coordinator.station_run_times[self.station_number] = minutes * 60

    @property
    def native_value(self) -> int:
        """Return the station run time in minutes."""
        return self.coordinator.station_run_time_s(self.station_number) // 60

    async def async_set_native_value(self, value: float) -> None:
        """Set the station run time."""
        self.coordinator.station_run_times[self.station_number] = int(value) * 60
        self.async_write_ha_state()
