"""DataUpdateCoordinator for Hunter NODE-BT."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
import logging

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import HunterAuthError, HunterConnectionError, HunterNodeBTClient, station_is_active
from .const import (
    CONF_DEFAULT_RUN_TIME,
    CONF_PIN,
    CONF_SCAN_INTERVAL,
    DEFAULT_PIN,
    DEFAULT_RUN_TIME_MIN,
    DEFAULT_SCAN_INTERVAL_MIN,
    DOMAIN,
)
from .protocol import (
    from_controller_time,
    rain_delay_days_remaining,
    rain_delay_end,
    to_controller_time,
)

_LOGGER = logging.getLogger(__name__)

# A BLE call through a proxy can hang forever; a hung poll stops the coordinator for good.
POLL_TIMEOUT = 120  # seconds
DISCONNECT_TIMEOUT = 10  # seconds
# While a station runs, poll faster so the valve closes in HA when the run ends.
ACTIVE_SCAN_INTERVAL = timedelta(minutes=1)
# Polls through the proxy fail now and then; keep the last data for this many failures in a row.
MAX_SOFT_FAILURES = 2

type HunterConfigEntry = ConfigEntry[HunterCoordinator]


class HunterCoordinator(DataUpdateCoordinator[dict]):
    """Polls controller state over BLE and caches the static station layout."""

    def __init__(self, hass: HomeAssistant, entry: HunterConfigEntry) -> None:
        scan_interval_min = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MIN)
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {entry.title}",
            update_interval=timedelta(minutes=int(scan_interval_min)),
        )
        self._idle_interval = timedelta(minutes=int(scan_interval_min))
        self._failures = 0
        self._unsub_run_end: Callable[[], None] | None = None
        self.entry = entry
        self.address: str = entry.unique_id or entry.data["address"]
        self._pin = int(entry.data.get(CONF_PIN, DEFAULT_PIN))
        self._client: HunterNodeBTClient | None = None
        self.station_count: int = 0
        self.controller: dict = {}
        self.stations: dict[int, dict] = {}
        self.programs: dict[str, dict] = {}
        # Per-station manual run time (seconds), set by the station Run Time numbers.
        self.station_run_times: dict[int, int] = {}
        self._full_data: dict = {}

    @property
    def default_run_time_s(self) -> int:
        minutes = self.entry.options.get(CONF_DEFAULT_RUN_TIME, DEFAULT_RUN_TIME_MIN)
        return int(minutes) * 60

    def station_run_time_s(self, station: int) -> int:
        """Manual run time for a station, falling back to the configured default."""
        return self.station_run_times.get(station, self.default_run_time_s)

    @property
    def season_adjust_active(self) -> int | None:
        """Seasonal adjustment % in force now (monthly table entry when enabled)."""
        if self.controller.get("SeasonAdjustByMonthEnable"):
            monthly = self.controller.get("SeasonAdjustByMonth")
            if isinstance(monthly, list) and len(monthly) == 12:
                return monthly[dt_util.now().month - 1]
            return None
        return self.controller.get("SeasonAdjust")

    @property
    def rain_delay_days(self) -> int:
        """Days left on the programmable-off (rain delay), 0 when inactive."""
        return rain_delay_days_remaining(
            self.controller.get("ProgrammableDaysOff"),
            to_controller_time(dt_util.now()),
        )

    @property
    def rain_delay_until(self) -> datetime | None:
        """Local time scheduled watering resumes, or None when no delay is active."""
        if not self.rain_delay_days:
            return None
        return from_controller_time(
            self.controller.get("ProgrammableDaysOff"),
            dt_util.get_default_time_zone(),
        )

    def _get_client(self) -> HunterNodeBTClient:
        device = bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)
        if device is None:
            raise UpdateFailed(f"Device {self.address} not seen by any Bluetooth adapter")
        if self._client is None or self._client._device != device:
            self._client = HunterNodeBTClient(device, self._pin, self.entry.title)
        return self._client

    async def _async_update_data(self) -> dict:
        try:
            async with asyncio.timeout(POLL_TIMEOUT):
                data = await self._async_poll()
        except (UpdateFailed, TimeoutError) as err:
            self._failures += 1
            if self._full_data.get("State") is not None and self._failures <= MAX_SOFT_FAILURES:
                _LOGGER.debug("Poll failed (%s), keeping last data (%s in a row)", err or "timeout", self._failures)
                return self._full_data
            if isinstance(err, TimeoutError):
                raise UpdateFailed(f"No reply within {POLL_TIMEOUT} s") from err
            raise
        self._failures = 0
        self._track_running(data)
        return data

    def _track_running(self, data: dict) -> None:
        """Poll every minute while a station runs and once more right after it should end."""
        running = [
            st for key, st in (data.get("State") or {}).items()
            if key.startswith("Station_") and isinstance(st, dict) and station_is_active(st.get("State"))
        ]
        self.update_interval = ACTIVE_SCAN_INTERVAL if running else self._idle_interval
        if self._unsub_run_end is not None:
            self._unsub_run_end()
            self._unsub_run_end = None
        remaining = [int(st.get("Remaining") or 0) for st in running if int(st.get("Remaining") or 0) > 0]
        if remaining and min(remaining) + 10 < ACTIVE_SCAN_INTERVAL.total_seconds():
            self._unsub_run_end = async_call_later(self.hass, min(remaining) + 10, self._run_end)

    async def async_shutdown(self) -> None:
        if self._unsub_run_end is not None:
            self._unsub_run_end()
            self._unsub_run_end = None
        await super().async_shutdown()

    @callback
    def _run_end(self, _now: datetime) -> None:
        self._unsub_run_end = None
        self.hass.async_create_task(self.async_request_refresh())

    async def _async_poll(self) -> dict:
        client = self._get_client()
        try:
            if not self.station_count:
                full = await client.read_all()
                self._full_data = full
                self.station_count = int((full.get("Controller") or {}).get("StationCount") or 0)

                for i in range(1, 5):
                    st_key = f"Station_{i}"
                    if st_key in full:
                        self.stations[i] = full[st_key]

                for p_letter in ("A", "B", "C", "T"):
                    p_key = f"Program_{p_letter}"
                    if p_key in full:
                        self.programs[p_letter] = full[p_key]
            else:
                # Controller and Sensor are not static: battery, rain delay and
                # settings changed from the phone app all live there.
                self._full_data.update(await client.read_controller())
                self._full_data.update(await client.read_sensor())

            # Read All does not include the State section.
            self._full_data.update(await client.read_state())
            self._full_data["LastRun"] = await client.read_last_run()
            self.controller = self._full_data.get("Controller") or {}
            return self._full_data

        except HunterAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except HunterConnectionError as err:
            raise UpdateFailed(str(err)) from err
        finally:
            await self._disconnect(client)

    async def _disconnect(self, client: HunterNodeBTClient) -> None:
        try:
            async with asyncio.timeout(DISCONNECT_TIMEOUT):
                await client.disconnect()
        except TimeoutError:
            _LOGGER.debug("Disconnect timed out")

    async def _async_command(
        self, command: Callable[[HunterNodeBTClient], Awaitable[None]]
    ) -> None:
        client = self._get_client()
        try:
            async with asyncio.timeout(POLL_TIMEOUT):
                await command(client)
        except TimeoutError as err:
            raise HunterConnectionError(f"No reply within {POLL_TIMEOUT} s") from err
        finally:
            await self._disconnect(client)
        # A short wait so the controller reports the new station state.
        await asyncio.sleep(2)
        await self.async_request_refresh()

    async def async_start_station(self, station: int, run_time_s: int | None = None) -> None:
        seconds = run_time_s if run_time_s is not None else self.station_run_time_s(station)
        await self._async_command(lambda client: client.start_station(station, seconds))

    async def async_run_program(self, letter: str, run_time_s: int | None = None) -> None:
        await self._async_command(lambda client: client.run_program(letter, run_time_s))

    async def async_stop_all(self) -> None:
        await self._async_command(lambda client: client.stop_all())

    async def _async_set_controller(
        self,
        command: Callable[[HunterNodeBTClient], Awaitable[None]],
        settings: dict,
    ) -> None:
        await self._async_command(command)
        # Reflect the change immediately; the refresh above is debounced.
        self.controller.update(settings)
        self.async_update_listeners()

    async def async_set_rain_delay(self, days: int) -> None:
        """Suspend scheduled watering for `days` days (0 cancels the delay)."""
        until = rain_delay_end(dt_util.now(), days) if days > 0 else 0
        await self._async_set_controller(
            lambda client: client.set_programmable_days_off(until),
            {"ProgrammableDaysOff": until},
        )

    async def async_set_controller_off(self, off: bool) -> None:
        """Turn the whole controller off (no scheduled or manual watering) or on."""
        await self._async_set_controller(
            lambda client: client.set_controller_off(off), {"ControllerOff": off}
        )

    async def async_set_season_adjust(self, percent: int) -> None:
        """Set a global seasonal adjustment %, turning the monthly table off."""
        await self._async_set_controller(
            lambda client: client.set_season_adjust(percent),
            {"SeasonAdjust": percent, "SeasonAdjustByMonthEnable": False},
        )

    async def async_set_season_adjust_by_month(self, enabled: bool) -> None:
        """Turn the monthly seasonal adjustment table on or off."""
        await self._async_set_controller(
            lambda client: client.set_season_adjust_by_month(enabled),
            {"SeasonAdjustByMonthEnable": enabled},
        )

    async def async_sync_clock(self) -> None:
        """Set the controller clock to Home Assistant's local time."""
        now = to_controller_time(dt_util.now())
        await self._async_command(lambda client: client.set_current_time(now))

    async def async_shutdown_client(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
            self._client = None
