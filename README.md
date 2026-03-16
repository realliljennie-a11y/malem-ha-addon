# Malem Alarm — Home Assistant Add-on

A Home Assistant add-on that integrates the **Malem wireless bedwetting alarm** as a native moisture sensor. The Malem app stopped receiving updates around 2015 and no longer installs on modern Android, but the BLE sensor hardware still works perfectly. This add-on reverse-engineers the proprietary BLE protocol to bring it into Home Assistant.

## Requirements

- Home Assistant OS on a Raspberry Pi (tested on Pi 5) or any host with Bluetooth
- The sensor must be within BLE range (~10m) of the Pi
- Mosquitto MQTT broker add-on installed and running

## Installation

1. In HA: **Settings → Add-ons → ⋮ → Repositories**
2. Add: `https://github.com/realliljennie-a11y/malem-ha-addon`
3. Install **Malem Alarm** from the add-on store
4. Configure MQTT credentials in the **Configuration** tab
5. Start the add-on

## Configuration

| Option | Default | Description |
|--------|---------|-------------|
| `mqtt_host` | `core-mosquitto` | MQTT broker hostname |
| `mqtt_port` | `1883` | MQTT broker port |
| `mqtt_username` | _(empty)_ | MQTT username |
| `mqtt_password` | _(empty)_ | MQTT password |
| `mqtt_topic_prefix` | `malem` | Topic prefix (change if running multiple sensors) |
| `dry_timeout` | `15` | Seconds to wait before declaring dry if 0xAC is missed |
| `log_level` | `info` | Logging verbosity: debug / info / warning / error |

## Entities

Once running, the following entities appear automatically in HA via MQTT discovery:

- **`binary_sensor.malem_moisture`** — `on` when wet, `off` when dry
  - Attributes: `last_wet_duration` (seconds), `last_wet_time`, `sensor_address`
- **`sensor.malem_battery`** — battery percentage, if supported by your device

## Protocol notes

The Malem sensor uses a proprietary BLE protocol over a custom service (`0xfff0`). Connection requires a challenge-response authentication that must complete within approximately 3 seconds of connecting. The status characteristic (`0xfff4`) reports:

| Byte | Meaning |
|------|---------|
| `0x00` | Standby (before first event) |
| `0xAA` | Wet event started |
| `0xAB` | Wet / alarm active (repeats while sensor is wet) |
| `0xAC` | Dry, event ended — `byte[1] × 5` = duration in seconds |

The authentication key table and magic bytes were extracted from the decompiled original Android APK.

## Multiple sensors

If you have more than one Malem sensor, run a second instance of the add-on (clone the repo, change the `slug` in `config.yaml`) and set a different `mqtt_topic_prefix` in the configuration of each instance. Each sensor will appear as a separate device in HA.
