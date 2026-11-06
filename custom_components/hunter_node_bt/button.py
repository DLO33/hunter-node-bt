"""Button platform for Hunter NODE-BT."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import HunterConfigEntry, HunterCoordinator
from .protocol import to_controller_time

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HunterConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Hunter NODE-BT button entities."""
    coordinator = entry.runtime_data

    entities: list[ButtonEntity] = [
        HunterStopAllButton(coordinator),
        HunterRunProgramButton(coordinator, "A"),
        HunterRunProgramButton(coordinator, "B"),
        HunterRunProgramButton(coordinator, "C"),
        HunterRunTestButton(coordinator),
        HunterCancelRainDelayButton(coordinator),
        HunterSyncClockButton(coordinator),
        HunterBatteryReplacedButton(coordinator),
        HunterRefreshButton(coordinator),
    ]

    async_add_entities(entities)


class HunterBaseButton(CoordinatorEntity[HunterCoordinator], ButtonEntity):
    """Base button entity for Hunter NODE-BT."""

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


class HunterStopAllButton(HunterBaseButton):
    """Button to stop all active irrigation."""

    _attr_icon = "mdi:stop-circle-outline"

    def __init__(self, coordinator: HunterCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_stop_all"
        self._attr_name = "Stop All Stations"

    async def async_press(self) -> None:
        """Handle button press."""
        await self.coordinator.async_stop_all()


class HunterRunProgramButton(HunterBaseButton):
    """Button to manually start an irrigation program (A, B, C)."""

    _attr_icon = "mdi:play-circle-outline"

    def __init__(self, coordinator: HunterCoordinator, program_letter: str) -> None:
        super().__init__(coordinator)
        self.program_letter = program_letter
        self._attr_unique_id = f"{coordinator.address}_run_program_{program_letter.lower()}"

        prog = coordinator.programs.get(program_letter, {})
        prog_name = prog.get("Name")
        if prog_name and prog_name.strip():
            self._attr_name = f"Run {prog_name.strip()}"
        else:
            self._attr_name = f"Run Program {program_letter}"

    async def async_press(self) -> None:
        """Handle button press."""
        await self.coordinator.async_run_program(self.program_letter)


class HunterRunTestButton(HunterBaseButton):
    """Button to run all stations test sequence."""

    _attr_icon = "mdi:playlist-play"

    def __init__(self, coordinator: HunterCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_run_test"
        self._attr_name = "Test All Stations"

    async def async_press(self) -> None:
        """Handle button press."""
        # Program T with default station runtime
        await self.coordinator.async_run_program("T", self.coordinator.default_run_time_s)


class HunterCancelRainDelayButton(HunterBaseButton):
    """Button to cancel an active rain delay and resume scheduled watering."""

    _attr_icon = "mdi:weather-sunny"

    def __init__(self, coordinator: HunterCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_cancel_rain_delay"
        self._attr_name = "Cancel Rain Delay"

    async def async_press(self) -> None:
        """Handle button press."""
        await self.coordinator.async_set_rain_delay(0)


class HunterSyncClockButton(HunterBaseButton):
    """Button to set the controller clock to Home Assistant's local time."""

    _attr_icon = "mdi:clock-check-outline"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: HunterCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_sync_clock"
        self._attr_name = "Sync Clock"

    async def async_press(self) -> None:
        """Handle button press."""
        await self.coordinator.async_sync_clock()


class HunterBatteryReplacedButton(HunterBaseButton):
    """Button to record today as the date the 9 V battery was changed."""

    _attr_icon = "mdi:battery-sync"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: HunterCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_battery_replaced"
        self._attr_name = "Battery Replaced"

    async def async_press(self) -> None:
        """Handle button press."""
        await self.coordinator.async_set_controller_settings(
            {"BatteryChangeDate": to_controller_time(dt_util.now())},
            "set battery change date",
        )


class HunterRefreshButton(HunterBaseButton):
    """Button to re-read programs, stations and settings, e.g. after using the phone app."""

    _attr_icon = "mdi:refresh"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: HunterCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_refresh"
        self._attr_name = "Refresh"

    async def async_press(self) -> None:
        """Handle button press."""
        await self.coordinator.async_full_refresh()
