"""The Hunter NODE-BT integration."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_CONFIG_ENTRY_ID, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .coordinator import HunterConfigEntry, HunterCoordinator
from .schedule import PROGRAM_LETTERS, parse_start_time, parse_water_days

PLATFORMS: list[Platform] = [
    Platform.VALVE,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SWITCH,
    Platform.TEXT,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_SET_PROGRAM = "set_program"

SET_PROGRAM_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required("program"): vol.In(PROGRAM_LETTERS),
        vol.Optional("start_times"): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("water_days"): cv.string,
        vol.Optional("station", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=4)),
        vol.Optional("run_minutes"): vol.All(vol.Coerce(int), vol.Range(min=0, max=240)),
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's actions."""

    async def set_program(call: ServiceCall) -> None:
        """Change start times, water days and/or a station's run time in one program.

        Anything not given is left as it is. An empty start_times list turns the
        program's starts off.
        """
        coordinator = _coordinator_for_call(hass, call)
        try:
            start_times = (
                [parse_start_time(text) for text in call.data["start_times"]]
                if "start_times" in call.data
                else None
            )
            water_days = (
                parse_water_days(call.data["water_days"]) if "water_days" in call.data else None
            )
            run_times = (
                {call.data["station"]: call.data["run_minutes"] * 60}
                if "run_minutes" in call.data
                else None
            )
            await coordinator.async_set_program(
                call.data["program"],
                start_times=start_times,
                water_days=water_days,
                run_times=run_times,
            )
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    hass.services.async_register(DOMAIN, SERVICE_SET_PROGRAM, set_program, SET_PROGRAM_SCHEMA)
    return True


def _coordinator_for_call(hass: HomeAssistant, call: ServiceCall) -> HunterCoordinator:
    """The coordinator an action targets: the given config entry, or the only controller."""
    entries = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]
    if entry_id := call.data.get(ATTR_CONFIG_ENTRY_ID):
        entries = [entry for entry in entries if entry.entry_id == entry_id]
        if not entries:
            raise ServiceValidationError(f"No loaded Hunter NODE-BT controller with entry {entry_id}")
    if not entries:
        raise ServiceValidationError("No Hunter NODE-BT controller is loaded")
    if len(entries) > 1:
        raise ServiceValidationError(
            "More than one Hunter NODE-BT controller is set up; choose one with config_entry_id"
        )
    return entries[0].runtime_data


async def async_setup_entry(hass: HomeAssistant, entry: HunterConfigEntry) -> bool:
    """Set up Hunter NODE-BT from a config entry."""
    coordinator = HunterCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HunterConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_shutdown_client()
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: HunterConfigEntry) -> None:
    """Reload when options (e.g. default run time) change."""
    await hass.config_entries.async_reload(entry.entry_id)
