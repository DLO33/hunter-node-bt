"""Text platform for Hunter NODE-BT: program start times, water days and station names."""

from __future__ import annotations

import logging

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HunterConfigEntry, HunterCoordinator
from .schedule import PROGRAM_LETTERS, parse_start_times, parse_water_days

_LOGGER = logging.getLogger(__name__)

MAX_STATION_NAME = 32


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HunterConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Hunter NODE-BT text entities."""
    coordinator = entry.runtime_data
    entities: list[TextEntity] = []
    for letter in PROGRAM_LETTERS:
        entities.append(HunterProgramStartTimesText(coordinator, letter))
        entities.append(HunterProgramWaterDaysText(coordinator, letter))
    count = coordinator.station_count or 1
    entities.extend(
        HunterStationNameText(coordinator, station_num) for station_num in range(1, count + 1)
    )
    async_add_entities(entities)


class HunterBaseText(CoordinatorEntity[HunterCoordinator], TextEntity):
    """Base text entity for Hunter NODE-BT."""

    _attr_has_entity_name = True
    _attr_mode = TextMode.TEXT

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


class HunterProgramText(HunterBaseText):
    """Base for text entities that edit one program (A, B or C)."""

    def __init__(self, coordinator: HunterCoordinator, letter: str) -> None:
        super().__init__(coordinator)
        self.letter = letter
        # Programs B and C are often unused; keep them out of the way until wanted.
        self._attr_entity_registry_enabled_default = letter == "A"

    @property
    def available(self) -> bool:
        """Only available once the controller has reported this program."""
        return super().available and self.letter in self.coordinator.programs


class HunterProgramStartTimesText(HunterProgramText):
    """Start times of a program, e.g. '06:30, 16:00', or 'off'."""

    _attr_icon = "mdi:clock-outline"
    _attr_native_max = 120

    def __init__(self, coordinator: HunterCoordinator, letter: str) -> None:
        super().__init__(coordinator, letter)
        self._attr_unique_id = f"{coordinator.address}_program_{letter.lower()}_start_times"
        self._attr_name = f"Program {letter} Start Times"

    @property
    def native_value(self) -> str:
        """Return the start times in 24-hour time."""
        return self.coordinator.program(self.letter).start_times_text

    async def async_set_value(self, value: str) -> None:
        """Write new start times to the program on the controller."""
        try:
            start_times = parse_start_times(value)
            await self.coordinator.async_set_program(self.letter, start_times=start_times)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err


class HunterProgramWaterDaysText(HunterProgramText):
    """Water days of a program: 'every day', 'odd days', 'even days', 'every 3 days' or 'Mon, Wed, Fri'."""

    _attr_icon = "mdi:calendar-week"
    _attr_native_max = 60

    def __init__(self, coordinator: HunterCoordinator, letter: str) -> None:
        super().__init__(coordinator, letter)
        self._attr_unique_id = f"{coordinator.address}_program_{letter.lower()}_water_days"
        self._attr_name = f"Program {letter} Water Days"

    @property
    def native_value(self) -> str:
        """Return the water days."""
        return self.coordinator.program(self.letter).water_days_text

    async def async_set_value(self, value: str) -> None:
        """Write new water days to the program on the controller."""
        try:
            water_days = parse_water_days(value)
            await self.coordinator.async_set_program(self.letter, water_days=water_days)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err


class HunterStationNameText(HunterBaseText):
    """The name stored on the controller for a station (shown in the phone app)."""

    _attr_icon = "mdi:rename-outline"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_max = MAX_STATION_NAME

    def __init__(self, coordinator: HunterCoordinator, station_number: int) -> None:
        super().__init__(coordinator)
        self.station_number = station_number
        self._attr_unique_id = f"{coordinator.address}_station_{station_number}_name"
        self._attr_name = f"Station {station_number} Name"

    @property
    def available(self) -> bool:
        """Only available once the controller has reported this station."""
        return super().available and self.station_number in self.coordinator.stations

    @property
    def native_value(self) -> str | None:
        """Return the station name."""
        return self.coordinator.stations.get(self.station_number, {}).get("Name")

    async def async_set_value(self, value: str) -> None:
        """Write the new name to the controller."""
        try:
            await self.coordinator.async_set_station(self.station_number, {"Name": value.strip()})
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
