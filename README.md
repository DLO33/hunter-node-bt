# Hunter NODE-BT Home Assistant Integration

![Hunter NODE-BT integration icon](custom_components/hunter_node_bt/brand/icon.png)

Custom Home Assistant integration (HACS) for **Hunter NODE-BT** Bluetooth battery-operated irrigation controllers (1, 2, and 4 stations).

Reverse-engineered directly from the official Hunter NODE-BT app (`com.hunter.nodev2`).

[![Buy me a coffee](custom_components/hunter_node_bt/brand/buymeacoffee.png)](https://buymeacoffee.com/ffilip37k)

---

## Features

- **Automatic Bluetooth Discovery**: Home Assistant automatically detects nearby Hunter NODE-BT controllers advertising the Nordic UART Service.
- **PIN Authentication**: Supports default PIN (`0`) and custom security PINs with challenge-response encryption.
- **Valve Control**: One valve entity per station (1–4 depending on your controller model):
  - Turn on a valve to start irrigation for that station's Run Time (defaults to the configured manual run time).
  - Turn off a valve to stop irrigation.
- **Rain Delay**: suspend scheduled watering for 1–30 days.
  - `number.<controller>_rain_delay`: set to N days to delay watering until local midnight N days from now; set to `0` to cancel.
  - **Cancel Rain Delay** button and **Rain Delay Until** timestamp sensor.
  - Works in automations, e.g. `number.set_value` with `value: 2` when the forecast says rain.
- **Controller On/Off**: `switch` to turn the whole controller off (no watering at all, e.g. for winter) and back on.
- **Seasonal Adjustment**:
  - `number` Season Adjust (0–300 %): sets a global adjustment and turns the monthly table off.
  - `switch` Monthly Season Adjust: use the controller's month-by-month table (shown as the `by_month` attribute).
  - `sensor` Active Season Adjust: the % applied right now.
- **Per-Station Run Time**: a `number` per station (minutes) used when its valve is opened; remembered across restarts.
  - Service `hunter_node_bt.start_watering` waters a valve for a given `duration`:
    ```yaml
    action: hunter_node_bt.start_watering
    target:
      entity_id: valve.oliveira
    data:
      duration: "00:15:00"
    ```
- **Programs (A, B, C)**: read and edit the controller's schedules from Home Assistant.
  - `sensor` Program X Schedule: a one-line summary such as `Every day at 06:30 (10 min)`, with `start_times`, `water_days` and `run_minutes` attributes.
  - `text` Program X Start Times: 24-hour times such as `06:30, 16:00`, or `off`.
  - `text` Program X Water Days: `every day`, `odd days`, `even days`, `every 3 days`, or days such as `Mon, Wed, Fri`.
  - `number` Program X Station N Run Time (minutes per start; `0` takes the station out of the program).
  - Programs B and C start disabled; enable their entities if you use them.
  - Service `hunter_node_bt.set_program` changes any of these in one call, leaving the rest as it is:
    ```yaml
    action: hunter_node_bt.set_program
    data:
      program: A
      start_times: ["06:30", "16:00"]
      water_days: "Mon, Wed, Fri"
      station: 1
      run_minutes: 15
    ```
  - Programs, station names and settings are re-read every hour, so changes made in the phone app show up; the **Refresh** button re-reads them straight away.
- **Station Settings**: `text` Station N Name (the name shown in the phone app) and `number` Station N Cycle / Soak (minutes, `0` = off).
- **Controller Settings** (configuration entities): Rain Sensor Enabled, Rain Sensor Delay (hours), Sensor Bypass, Soil Sensor Enabled, Soil Sensor Trigger Level, Master Valve / Pump, Solar Panel Attached and Delay Between Stations.
- **Sensors**:
  - Controller Battery level (`%`).
  - Soil moisture level (`%`).
  - Controller operational state (Idle, Running, Rain Delay, etc.).
  - Next scheduled watering time timestamp.
  - Last run (start time, station, run time).
  - Rain delay end time.
  - Remaining run time per station (seconds).
  - Battery Changed and Settings Changed dates (diagnostic).
- **Binary Sensors**:
  - Rain sensor active / Clik sensor detection.
  - Rain delay active status.
- **Buttons**:
  - **Stop All Stations**: Instantly halts all active watering valves.
  - **Run Program A / B / C**: Manually start pre-configured irrigation cycles.
  - **Test All Stations**: Runs a quick test sequence across all stations.
  - **Cancel Rain Delay**: Resumes scheduled watering.
  - **Sync Clock**: Sets the controller clock to Home Assistant's local time (the controller does not follow DST on its own).
  - **Battery Replaced**: Records today as the battery change date on the controller.
  - **Refresh**: Re-reads programs, station names and settings from the controller.
- **Options Flow**: Easily change the default manual run time (in minutes) via the integration settings without re-pairing.

---

## Installation

### Via HACS (Recommended)

1. Open **HACS** in your Home Assistant instance.
2. Click the three dots in the top right corner and select **Custom repositories**.
3. Add the repository URL: `https://github.com/filipejrcorreia/hunter-node-bt` with category **Integration**.
4. Click **Download**, then restart Home Assistant.

### Manual Installation

1. Copy the `custom_components/hunter_node_bt` folder into your Home Assistant `<config_dir>/custom_components/` directory.
2. Restart Home Assistant.

---

## Configuration

1. In Home Assistant, go to **Settings > Devices & Services > Add Integration**.
2. Search for **Hunter NODE-BT** (or accept the discovered Bluetooth device notification).
3. If prompted, select your controller's Bluetooth address and enter your **PIN** (default is `0`).
4. Set your preferred default manual run time (default is 10 minutes).

---

## Standalone Test Client

A standalone command-line testing tool is included in [`hunter_node_bt_client.py`](hunter_node_bt_client.py):

```bash
# Set up virtual environment and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install bleak

# Read full controller status (auto-discovers nearby device)
python hunter_node_bt_client.py read-all

# Read current running/sensor state
python hunter_node_bt_client.py read-state

# Start station 1 for 60 seconds
python hunter_node_bt_client.py start 1 60

# Stop all stations
python hunter_node_bt_client.py stop

# Run program A
python hunter_node_bt_client.py run-program A

# Delay watering for 2 days (0 cancels)
python hunter_node_bt_client.py rain-delay 2

# Set the controller clock to this computer's local time
python hunter_node_bt_client.py sync-clock

# Send an arbitrary raw JSON command
python hunter_node_bt_client.py raw '{"Read":"Section_Sensor"}'

# Target a specific address or custom PIN
python hunter_node_bt_client.py -a <ADDRESS> -p <PIN> <command>
```

---

## Protocol Details

For the full reverse-engineered Bluetooth protocol documentation, see [PROTOCOL.md](PROTOCOL.md).
