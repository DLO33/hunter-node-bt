"""BLE client for the Hunter NODE-BT controller.

Protocol recovered from the official app (com.hunter.nodev2). See PROTOCOL.md.
Transport is the Nordic UART Service; commands are JSON written in 20-byte chunks
and responses are NUL-terminated JSON via notifications. A PIN challenge-response
login (XOR-obfuscated 6-byte packets) is required first.
"""

from __future__ import annotations

import asyncio
import json
import logging

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak_retry_connector import establish_connection

from .const import ACTIVE_STATES, MSG_RX, MSG_TX, PIN_CHAR
from .protocol import (
    CHUNK_SIZE,
    MAX_RESPONSE_SIZE,
    MessageTooLargeError,
    NulDelimitedMessageBuffer,
    chunk_payload,
    parse_pin_info,
    pin_info,
    sanitize_hunter_json,
    validate_pin,
    xor_ac,
)

_LOGGER = logging.getLogger(__name__)

COMMAND_TIMEOUT = 20

# PinMessageType
_LOGIN_REQUEST = 1
_LOGIN_CHALLENGE_REQUEST = 2
_LOGIN_CHALLENGE_RESPONSE = 3
_LOGIN_CHALLENGE_RESULT = 4


class HunterAuthError(Exception):
    """Raised when the PIN is rejected."""


class HunterConnectionError(Exception):
    """Raised on connection/communication failure."""


class HunterNodeBTClient:
    """Manages a connection to one controller."""

    def __init__(self, device: BLEDevice, pin: int, name: str | None = None) -> None:
        self._device = device
        self._pin = validate_pin(pin)
        self._name = name or device.address
        self._client: BleakClient | None = None
        self._lock = asyncio.Lock()
        self._pin_q: asyncio.Queue[tuple[int, int, int, int]] = asyncio.Queue()
        self._msg_buf = NulDelimitedMessageBuffer(MAX_RESPONSE_SIZE)
        self._msg_future: asyncio.Future[str] | None = None

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    # -- notification handlers ----------------------------------------------
    def _on_pin(self, _char, data: bytearray) -> None:
        raw = xor_ac(bytes(data))
        if len(raw) >= 6:
            self._pin_q.put_nowait(parse_pin_info(raw))

    def _on_msg(self, _char, data: bytearray) -> None:
        future = self._msg_future
        if future is None or future.done():
            self._msg_buf.clear()
            return
        try:
            text = self._msg_buf.append(bytes(data))
        except MessageTooLargeError as err:
            future.set_exception(HunterConnectionError(str(err)))
            return
        if text is not None:
            future.set_result(text)

    # -- lifecycle -----------------------------------------------------------
    async def ensure_connected(self) -> None:
        if self.is_connected:
            return
        try:
            self._client = await establish_connection(
                BleakClient, self._device, self._name
            )
            await self._client.start_notify(PIN_CHAR, self._on_pin)
            await self._client.start_notify(MSG_TX, self._on_msg)
            await self._login()
        except HunterAuthError:
            await self.disconnect()
            raise
        except Exception as err:  # noqa: BLE001
            await self.disconnect()
            raise HunterConnectionError(str(err)) from err

    async def _login(self) -> None:
        while not self._pin_q.empty():
            self._pin_q.get_nowait()
        await self._client.write_gatt_char(
            PIN_CHAR, xor_ac(pin_info(_LOGIN_REQUEST)), response=False
        )
        ptype, result, rnd, _ = await self._wait_pin(_LOGIN_CHALLENGE_REQUEST)
        if result != 0:
            raise HunterConnectionError(f"Login challenge failed (type={ptype} result={result})")
        resp = pin_info(
            _LOGIN_CHALLENGE_RESPONSE, random_number=rnd, pin=self._pin ^ rnd
        )
        await self._client.write_gatt_char(PIN_CHAR, xor_ac(resp), response=False)
        ptype, result, _, _ = await self._wait_pin(_LOGIN_CHALLENGE_RESULT)
        if result != 0:
            raise HunterAuthError(f"PIN rejected (result={result})")

    async def _wait_pin(self, wanted: int) -> tuple[int, int, int, int]:
        """Wait for a PIN packet of the wanted type, skipping others (a repeated challenge or a stale packet from an
        earlier connection made a correct PIN look rejected: 'PIN rejected (result=0)')."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + COMMAND_TIMEOUT
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            packet = await asyncio.wait_for(self._pin_q.get(), remaining)
            if packet[0] == wanted:
                return packet
            _LOGGER.debug("Ignoring PIN packet type %s while waiting for %s", packet[0], wanted)

    async def disconnect(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    # -- commands ------------------------------------------------------------
    async def _send(self, command: dict, expect_reply: bool = True) -> dict | None:
        async with self._lock:
            await self.ensure_connected()
            payload = json.dumps(command, separators=(",", ":")).encode("utf-8")
            loop = asyncio.get_running_loop()
            reply_future: asyncio.Future[str] | None = None
            if expect_reply:
                reply_future = loop.create_future()
                self._msg_future = reply_future
                self._msg_buf.clear()
            try:
                for chunk in chunk_payload(payload, CHUNK_SIZE):
                    await self._client.write_gatt_char(MSG_RX, chunk, response=True)
                if reply_future is None:
                    return None
                try:
                    raw_reply = await asyncio.wait_for(reply_future, COMMAND_TIMEOUT)
                except TimeoutError as err:
                    raise HunterConnectionError(
                        "Timed out waiting for a controller response"
                    ) from err
                if not raw_reply:
                    raise HunterConnectionError("Empty response from controller")
                try:
                    result = json.loads(sanitize_hunter_json(raw_reply))
                except json.JSONDecodeError as err:
                    raise HunterConnectionError(
                        "Invalid JSON response from controller"
                    ) from err
                if not isinstance(result, dict):
                    raise HunterConnectionError(
                        "Unexpected response from controller"
                    )
                return result
            except asyncio.CancelledError:
                await self.disconnect()
                raise
            except HunterConnectionError:
                await self.disconnect()
                raise
            except Exception as err:  # noqa: BLE001
                await self.disconnect()
                raise HunterConnectionError(str(err)) from err
            finally:
                if self._msg_future is reply_future:
                    self._msg_future = None
                self._msg_buf.clear()

    async def read_all(self) -> dict:
        reply = await self._send({"Read": "All"})
        if reply is None:
            raise HunterConnectionError("Missing response from Read All")
        return reply

    async def _read(self, section: str) -> dict:
        reply = await self._send({"Read": section})
        if reply is None:
            raise HunterConnectionError(f"Missing response from Read {section}")
        return reply

    async def read_state(self) -> dict:
        return await self._read("Section_State")

    async def read_controller(self) -> dict:
        return await self._read("Section_Controller")

    async def read_sensor(self) -> dict:
        return await self._read("Section_Sensor")

    async def read_last_run(self) -> dict:
        """Return the flat LastRun record (StartEvent, StartTime, Station, RunTime, StopEvent)."""
        return await self._read("LastRun")

    @staticmethod
    def _check_status(reply: dict | None, operation: str) -> None:
        status = reply.get("Status") if reply is not None else None
        if not isinstance(status, int) or isinstance(status, bool):
            raise HunterConnectionError(
                f"Invalid status response while attempting to {operation}"
            )
        if status != 0:
            raise HunterConnectionError(
                f"Failed to {operation}: status={status}"
            )

    async def start_station(self, station: int, seconds: int) -> None:
        reply = await self._send({"Manual": {"StationNumber": station, "RunTime": seconds}})
        self._check_status(reply, f"start station {station}")

    async def run_program(self, letter: str, seconds: int | None = None) -> None:
        cmd: dict = {"Manual": {"ProgramLetter": letter}}
        if seconds is not None:
            cmd["Manual"]["RunTime"] = seconds
        reply = await self._send(cmd)
        self._check_status(reply, f"run program {letter}")

    async def stop_all(self) -> None:
        reply = await self._send({"Manual": {"StopAll": True}})
        self._check_status(reply, "stop all stations")

    async def set_controller(self, settings: dict, operation: str) -> None:
        """Write one or more Controller settings."""
        reply = await self._send({"Controller": settings})
        self._check_status(reply, operation)

    async def set_programmable_days_off(self, until: int) -> None:
        """Suspend scheduled watering until a controller timestamp (0 cancels)."""
        await self.set_controller({"ProgrammableDaysOff": until}, "set rain delay")

    async def set_current_time(self, now: int) -> None:
        """Set the controller clock (controller local-epoch timestamp)."""
        await self.set_controller({"CurrentTime": now}, "set controller time")

    async def set_controller_off(self, off: bool) -> None:
        """Turn the controller off (no watering at all) or back on."""
        await self.set_controller({"ControllerOff": off}, "set controller off")

    async def set_season_adjust(self, percent: int) -> None:
        """Set the global seasonal adjustment; this turns the monthly table off."""
        await self.set_controller(
            {"SeasonAdjust": percent, "SeasonAdjustByMonthEnable": False},
            "set season adjust",
        )

    async def set_season_adjust_by_month(self, enabled: bool) -> None:
        """Turn the controller's monthly seasonal adjustment table on or off."""
        await self.set_controller(
            {"SeasonAdjustByMonthEnable": enabled}, "set monthly season adjust"
        )

    async def set_program(self, letter: str, program: dict) -> None:
        """Write a whole Program_X section (A, B or C)."""
        reply = await self._send({f"Program_{letter}": program})
        self._check_status(reply, f"write program {letter}")

    async def set_station(self, station: int, settings: dict) -> None:
        """Write a Station_N section (Name, Cycle and Soak)."""
        reply = await self._send({f"Station_{station}": settings})
        self._check_status(reply, f"write station {station}")


def station_is_active(state_value: int | None) -> bool:
    """True if a StationState.State means water is flowing."""
    return state_value in ACTIVE_STATES
