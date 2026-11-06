# Hunter NODE-BT — BLE Protocol (reverse-engineered)

Source: `Hunter+NODE-BT_6.0.611246_APKPure.xapk` → package `com.hunter.nodev2`
(.NET MAUI / C# app). Protocol recovered by decompiling the app's own assemblies:
`NodeV2.Service.Interface.dll`, `NodeV2.Services.dll`,
`NodeV2.Services.AcrReactiveBle.dll` (ILSpy). Everything below is taken directly
from that code — not guessed.

The controller is a battery BLE irrigation controller built on a **TI CC26xx**
chip. It exposes **1–4 stations (zones)**, programs **A/B/C** (+ test **T**).

---

## 1. Discovery

Scan for peripherals **advertising the Nordic UART Service**:

```
6e400001-b5a3-f393-e0a9-e50e24dcca9e
```

The app scans filtering on that service UUID and then matches by advertised name
(the name is the user-set controller name, not a fixed prefix). For an integration,
filter on the service UUID above.

---

## 2. GATT map

| Purpose | Service UUID | Characteristic UUID | Dir / Properties |
|---|---|---|---|
| **Message — request** (app→device) | `6e400001-b5a3-f393-e0a9-e50e24dcca9e` (NUS) | `6e400002-b5a3-f393-e0a9-e50e24dcca9e` | Write (with response) |
| **Message — response** (device→app) | same | `6e400003-b5a3-f393-e0a9-e50e24dcca9e` | Indicate / Notify |
| **PIN auth** | `0ed3e3d3-8cd8-4f29-8fec-a7d3a2c5443e` | `4c9dbe52-3566-4dfc-a299-4ea1353970e2` | Write without response + Notify |
| Controller FW update | `0031f62c-b837-11e7-abc4-cec278b6b50a` | req `6d9e1460-…`, resp `41a87e7b-…` | — |
| Bootloader FW update | `3595f2c9-f57c-429c-ae01-185ef51d4020` | req `639b5b38-…`, resp `04498679-…` | — |
| BT OTA (TI) | `1d14d6ee-fd63-4fa1-bfa4-8f47b42119f0` | ctrl `f7bf3564-…`, data `984227f3-…` | — |

Only the first three rows are needed for a Home Assistant integration. CCCD is the
standard `00002902-…`.

Connection bring-up (`BleDeviceCommunication.Initialize`):
1. Connect.
2. Subscribe to notifications on **PIN** char `4c9dbe52`.
3. Subscribe to notifications on **Message-response** char `6e400003`.
4. Run the PIN login handshake (§3).
5. Exchange JSON commands (§4).

---

## 3. PIN authentication (required before commands)

All PIN packets are a fixed **6-byte little-endian struct**, then obfuscated:

```
DevicePinInfo (6 bytes, little-endian):
  offset 0  uint8   packetType
  offset 1  uint8   result        # 0 = OK (in replies)
  offset 2  uint16  randomNumber
  offset 4  uint16  pin
```

**Obfuscation on the wire** (`MessageEncryptDecrypt`, same function both ways):

```
for each of the 6 bytes b:
    out = (b ^ 0xAC) if b != 0 else 0      # NOTE: bytes equal to 0 are left as 0
```

> Quirk to replicate exactly: a plaintext byte `0x00` stays `0x00`, and a plaintext
> byte `0xAC` encodes to `0x00` (and will **not** decode back). The device uses the
> same function, so just apply it symmetrically.

**PIN value obfuscation** (`PinEncryptDecrypt`): `pin_field = PIN ^ randomNumber`.
The PIN itself is a **numeric uint16** (`ushort.Parse`), default **`0`**.

`PinMessageType` (byte): `None=0, LoginRequest=1, LoginChallengeRequest=2,
LoginChallengeResponse=3, LoginChallengeResult=4, ChangePinRequest=5,
ChangePinChallengeRequest=6, ChangePinChallengeResponse=7, ChangePinChallengeResult=8`.

### Login handshake (on char `4c9dbe52`)

1. **App→device**: `DevicePinInfo{ packetType=1 }` → encode → write.
2. **Device→app** (notify): decode → `packetType=2` (LoginChallengeRequest),
   carries `randomNumber=R`. `result` must be 0.
3. **App→device**: `DevicePinInfo{ packetType=3, randomNumber=R, pin = PIN ^ R }`
   → encode → write.
4. **Device→app** (notify): decode → `packetType=4` (LoginChallengeResult).
   `result == 0` ⇒ authenticated.

Change PIN is the same pattern with packetTypes 5→6→7→8.

---

## 4. Message transport (commands)

**Send** (`SendCommandAsync`):
1. Serialize the command object to **JSON** (Newtonsoft; compact).
2. UTF-8 encode.
3. Split into **20-byte chunks** (`ChunkBytesIntoPackets`, chunk size 20).
4. Write each chunk to `6e400002` (write with response). Note that GATT property on 6e400002 is write-with-response; writes without response are ignored by the controller.

**Receive** (`MessageServiceOnNotify`):
- Each notification on `6e400003` is a UTF-8 fragment; append to a buffer.
- The message is **complete when a `0x00` (NUL) byte is seen**; text up to the NUL
  is the full JSON. (A new fragment starting with `{` while the buffer is non-empty
  resets the buffer — i.e. messages don't interleave.)
- If the JSON matches `{ "Status": … }` it's a **status ack** (`{"Status":<int>}`,
  0 = success); otherwise it's a **read result** (a `CommandModel`, see §6).
- Commands are issued one at a time (single-command semaphore).

The integration and standalone client share the same protocol helpers. They
accept numeric PINs from `0` to `65535`, reject empty or invalid JSON replies,
and cap an assembled response at 64 KiB. A timeout, empty/invalid JSON reply, or
oversized reply disconnects the BLE client so a late notification cannot be
mistaken for the next command's response.

---

## 5. Command catalog

All are plain objects serialized to JSON. Property names are case-sensitive as shown.

### Reads (→ return a CommandModel JSON, §6)
| Call | JSON sent |
|---|---|
| Read everything | `{"Read":"All"}` |
| Controller section | `{"Read":"Section_Controller"}` |
| Sensor section | `{"Read":"Section_Sensor"}` |
| Live state | `{"Read":"Section_State"}` |
| Last run | `{"Read":"LastRun"}` |
| Logs | `{"Read":"Log"}` |

### Manual control (→ return `{"Status":0}`) — the important ones for HA
| Action | JSON sent |
|---|---|
| **Start one zone** | `{"Manual":{"StationNumber":<1..4>,"RunTime":<seconds>}}` |
| Run a program | `{"Manual":{"ProgramLetter":"A"}}` (`A`/`B`/`C`) |
| Run-all / test all | `{"Manual":{"ProgramLetter":"T","RunTime":<seconds>}}` |
| **Stop everything** | `{"Manual":{"StopAll":true}}` |

### Controller settings (→ `{"Status":0}`)
`{"Controller":{ … }}` with any of:
`Name`(str), `ControllerOff`(bool), `CurrentTime`(unix seconds),
`SeasonAdjust`(int %)+`SeasonAdjustByMonthEnable`(bool), `StationDelay`(seconds),
`PmvEnabled`(bool), `ClickSensorEnabled`(bool), `SensorBypassEnabled`(bool),
`LogCount:0`(clear log), `ProgrammableDaysOff`(unix), `SoilSensorEnabled`,
`SoilTriggerLevel`, `ClickDelaySeconds`.

Programs: `{"Program_A":{…}}` with `Name`, `StartTimes:int[]` (minutes),
`RunTime:int[]` (seconds per station), `ScheduleType/ScheduleDays/IntervalDayCount/IntervalDayNext`.
Stations: `{"Station_1":{"Name":…,"Cycle":…,"Soak":…}}` (Station_1..4).
Factory reset: `{"HunterCustom":{"FactoryReset":true}}`.

---

## 6. Response model (`CommandModel`)

```jsonc
{
  "Controller": {
    "Name": "...", "Version": "...", "BLVersion": "...", "BTVersion": "...",
    "StationCount": 4, "SerialNumber": 0, "CurrentTime": 0, "LogCount": 0,
    "SeasonAdjust": 100, "SeasonAdjustByMonthEnable": false, "SeasonAdjustByMonth": [..12..],
    "StationDelay": 0, "PmvEnabled": false, "ProgrammableDaysOff": 0,
    "ControllerOff": false, "ClickSensorEnabled": false, "ClickDelaySeconds": 0,
    "SoilSensorEnabled": false, "SoilTriggerLevel": 0, "SensorBypassEnabled": false,
    "BatteryChangeDate": 0, "SettingsChangeDate": 0, "ManualRunTime": 0,
    "SolarPanelAttached": false
  },
  "Sensor": { "Battery": 0, "MoistureSensor": 0, "ClickSensorActive": false,
              "ClickSensorRestore": 0, "ProgrammableOffRestore": 0 },
  "Program_A": { "Name": "...", "ScheduleType": 0, "ScheduleDays": 0,
                 "IntervalDayCount": 0, "IntervalDayNext": 0,
                 "StartTimes": [..], "RunTime": [..] },      // + Program_B, _C, _T
  "Station_1": { "Name": "...", "Cycle": 0, "Soak": 0 },      // + Station_2.._4
  "State": {
    "ControllerState": 0, "ClickSensorState": false, "SoilSensorPercentage": 0,
    "NextWaterTime": 0, "DailyRunTime": 0,
    "Station_1": { "Number": 1, "State": 0, "Remaining": 0 }  // + _2.._4
  }
}
```

`StationState.State` enum: `0 Idle, 1 WaitingToRun, 2 Suspended, 3 Running,
4 Soaking, 5 Delay, 6 Complete`. `Remaining` is seconds left.

`Sensor.Battery` is the battery level; `MoistureSensor` the soil reading.

### Observed on hardware (NODE-BT-543044, firmware 2.2A, 2026-10-25)

- **Timestamps are local wall-clock time encoded as a Unix epoch** ("local
  epoch"): `CurrentTime`, `ProgrammableDaysOff` and `LastRun.StartTime`.
  `CurrentTime` read 13:33 "UTC" at 12:35 UTC on the day DST ended — the clock
  is whatever the app last set and does not follow DST by itself. Write
  `{"Controller":{"CurrentTime":<local epoch>}}` to resync.
- **`State.NextWaterTime` is minutes after midnight**, not a timestamp
  (`360` = 06:00, the first Program A start). No date is given.
- **`{"Read":"All"}` has no `State` section** — read `Section_State` separately.
  It also returns `HunterCustom` and `Manual`, and `Controller.VersionLetter`.
- `Sensor` also carries `MoistureADC`. `MoistureSensor` reads `100` with no soil
  probe attached and `SoilSensorEnabled:false`. `State` has no
  `SoilSensorPercentage` on this firmware.
- `{"Read":"LastRun"}` returns a flat object:
  `{"StartEvent":2,"StartTime":<local epoch>,"Station":2,"RunTime":144,"StopEvent":1}`
  (`RunTime` in seconds; event code meanings unknown).
- Program `StartTimes` uses `65535` for an unused slot.

### Rain delay (programmable days off)

`{"Controller":{"ProgrammableDaysOff":<local epoch>}}` suspends scheduled
watering until that moment; `ControllerState` reports `3` (programmable off)
while it is active. The integration writes local midnight N days ahead, and `0`
to cancel. A past value (e.g. `1791417600` = 2026-10-08 00:00 left over from
the app) means no delay.

Verified on hardware (2026-10-25): writing `1792972800` (2026-10-26 00:00 local)
returned `{"Status":0}` and `ControllerState` went `0 → 3`; writing `0` returned
`{"Status":0}`, `ControllerState` went back to `0` and `ProgrammableDaysOff`
read back as `0`. `Sensor.ProgrammableOffRestore` stayed `0` throughout.

### Programs, stations and settings (verified on a 2-station controller, firmware 2.2A, 2026-10-26)

- A program write must carry the whole section:
  `{"Program_A":{<every field from Read All, with changes>}}` → `{"Status":0}`.
  `StartTimes` keeps its slot count (8 on this firmware) with `65535` in unused
  slots; `RunTime` is seconds per station, `0` = station not in the program.
- `ScheduleDays` is a bitmask with bit 0 = Sunday … bit 6 = Saturday (`127` =
  every day). `ScheduleType` `0` uses `ScheduleDays`; `1`, `2` and `3` follow
  the app's odd days / even days / interval options, with `IntervalDayCount` for
  the interval.
- `{"Station_1":{"Name":…,"Cycle":…,"Soak":…}}` → `{"Status":0}`. `Cycle` and
  `Soak` are seconds (`0` = off). Writing the whole section keeps the fields
  that aren't being changed.
- `StationDelay` and `ClickDelaySeconds` (rain sensor delay) are seconds.
  `BatteryChangeDate` and `SettingsChangeDate` are local-epoch timestamps;
  writing `BatteryChangeDate` records a battery change as the app does.
- Boolean settings (`ClickSensorEnabled`, `SensorBypassEnabled`,
  `SoilSensorEnabled`, `PmvEnabled`, `SolarPanelAttached`) take `true`/`false`
  and read back as written.

### Controller off and seasonal adjustment (verified 2026-10-25)

- `{"Controller":{"ControllerOff":true}}` → `{"Status":0}`, `ControllerState`
  `0 → 2` (system off); `false` brings it back to `0`.
- `{"Controller":{"SeasonAdjust":90,"SeasonAdjustByMonthEnable":false}}` →
  `{"Status":0}`, read back as `90` / `false`.
- `{"Controller":{"SeasonAdjustByMonthEnable":true}}` alone turns the monthly
  table back on; `SeasonAdjustByMonth` (January first) is untouched by either
  write.

---

## 7. Suggested Home Assistant mapping

- One **switch / valve** entity per station → ON = `{"Manual":{"StationNumber":N,"RunTime":default}}`, OFF = `{"Manual":{"StopAll":true}}` (the controller runs one zone at a time).
- Poll `{"Read":"Section_State"}` for `State.Station_N.State`/`Remaining` → entity state + remaining-time sensor.
- Sensors: `Sensor.Battery`, `Sensor.MoistureSensor`, `State.NextWaterTime`.
- Rain delay: `number` (days, 0 cancels) writing `Controller.ProgrammableDaysOff`.
- Config entry: PIN (default `0`). Use `bleak` + the `bleak-retry-connector` /
  `habluetooth` stack like other BT integrations; the app uses write-without-response
  and NUS exactly as a UART bridge.

See `hunter_node_bt_client.py` for a runnable reference (scan → login → start zone).
