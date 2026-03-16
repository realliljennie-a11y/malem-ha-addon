"""
Malem Alarm — Home Assistant Add-on
Listens for Malem BLE moisture sensor events and publishes to MQTT
with Home Assistant MQTT discovery, so the sensor auto-appears in HA.

On first run the add-on scans for the sensor by name, saves its MAC address
to /config/malem_state.json, then restarts itself so run.sh can pre-cache
the BlueZ GATT services before the next connection attempt. On all subsequent
runs the MAC is loaded from the state file and used directly — no user
configuration of the MAC address required.

MQTT topics published:
  {prefix}/moisture/state          "wet" | "dry"
  {prefix}/moisture/attributes     JSON: last_wet_duration, last_wet_time
  {prefix}/moisture/availability   "online" | "offline"
  {prefix}/battery/state           0-100 (if supported by device)

HA discovery topics:
  homeassistant/binary_sensor/{prefix}_moisture/config
  homeassistant/sensor/{prefix}_battery/config   (if battery found)
"""

import asyncio
import json
import logging
import os
import random
import sys
import time
from datetime import datetime

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion
from bleak import BleakScanner, BleakClient
from bleak.exc import BleakError

# ── config ────────────────────────────────────────────────────────────────────

MQTT_HOST     = os.environ.get("MQTT_HOST", "core-mosquitto")
MQTT_PORT     = int(os.environ.get("MQTT_PORT", 1883))
MQTT_USERNAME = os.environ.get("MQTT_USERNAME", "")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")
TOPIC_PREFIX  = os.environ.get("MQTT_TOPIC_PREFIX", "malem")
DRY_TIMEOUT   = int(os.environ.get("DRY_TIMEOUT", 15))
LOG_LEVEL     = os.environ.get("LOG_LEVEL", "info").upper()
# Optional manual override — if set, skips scan and state file entirely
SENSOR_MAC_OVERRIDE = os.environ.get("SENSOR_MAC", "").strip().upper()

# State file lives in /config which is the HA config directory (mounted rw)
STATE_FILE = "/config/malem_state.json"

# ── logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("malem")

# ── BLE constants ─────────────────────────────────────────────────────────────

DEVICE_NAME           = "MALEM ALARM"
CHAR_AUTH_WRITE       = "0000fff1-0000-1000-8000-00805f9b34fb"
CHAR_AUTH_CHALLENGE   = "0000fff2-0000-1000-8000-00805f9b34fb"
CHAR_STATUS           = "0000fff4-0000-1000-8000-00805f9b34fb"
CHAR_BATTERY          = "00002a19-0000-1000-8000-00805f9b34fb"

KEY_TABLE = [0x79, 0xA8, 0xBF, 0x90, 0x88, 0x3E, 0x45, 0x13,
             0x46, 0x6C, 0xF9, 0xD7, 0x20, 0xCA, 0x58, 0xDA]

POLL_INTERVAL         = 1.5
BATTERY_POLL_INTERVAL = 300

# ── MQTT topics ───────────────────────────────────────────────────────────────

TOPIC_MOISTURE_STATE = f"{TOPIC_PREFIX}/moisture/state"
TOPIC_MOISTURE_ATTRS = f"{TOPIC_PREFIX}/moisture/attributes"
TOPIC_AVAILABILITY   = f"{TOPIC_PREFIX}/moisture/availability"
TOPIC_BATTERY_STATE  = f"{TOPIC_PREFIX}/battery/state"
TOPIC_DISC_MOISTURE  = f"homeassistant/binary_sensor/{TOPIC_PREFIX}_moisture/config"
TOPIC_DISC_BATTERY   = f"homeassistant/sensor/{TOPIC_PREFIX}_battery/config"

# ── state file ────────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log.error(f"Failed to save state: {e}")


# ── MQTT ──────────────────────────────────────────────────────────────────────

mqtt_client = mqtt.Client(
    callback_api_version=CallbackAPIVersion.VERSION2,
    client_id="malem_addon",
)

def mqtt_connect():
    if MQTT_USERNAME:
        mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

    mqtt_client.will_set(TOPIC_AVAILABILITY, "offline", retain=True)

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            log.info(f"MQTT connected to {MQTT_HOST}:{MQTT_PORT}")
            publish_discovery_moisture()
            client.publish(TOPIC_AVAILABILITY, "online", retain=True)
        else:
            log.error(f"MQTT connection failed: {reason_code}")

    def on_disconnect(client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            log.warning("MQTT disconnected unexpectedly, will retry")

    mqtt_client.on_connect = on_connect
    mqtt_client.on_disconnect = on_disconnect
    mqtt_client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
    mqtt_client.loop_start()


def publish_discovery_moisture():
    payload = {
        "name": "Malem Moisture",
        "unique_id": f"{TOPIC_PREFIX}_moisture",
        "device_class": "moisture",
        "state_topic": TOPIC_MOISTURE_STATE,
        "payload_on": "wet",
        "payload_off": "dry",
        "availability_topic": TOPIC_AVAILABILITY,
        "payload_available": "online",
        "payload_not_available": "offline",
        "json_attributes_topic": TOPIC_MOISTURE_ATTRS,
        "device": {
            "identifiers": [TOPIC_PREFIX],
            "name": "Malem Alarm",
            "model": "Malem BLE Moisture Sensor",
            "manufacturer": "Malem",
        },
        "icon": "mdi:water-alert",
    }
    mqtt_client.publish(TOPIC_DISC_MOISTURE, json.dumps(payload), retain=True)
    log.info("Published moisture discovery config")


def publish_discovery_battery():
    payload = {
        "name": "Malem Battery",
        "unique_id": f"{TOPIC_PREFIX}_battery",
        "device_class": "battery",
        "state_topic": TOPIC_BATTERY_STATE,
        "unit_of_measurement": "%",
        "availability_topic": TOPIC_AVAILABILITY,
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": {
            "identifiers": [TOPIC_PREFIX],
            "name": "Malem Alarm",
            "model": "Malem BLE Moisture Sensor",
            "manufacturer": "Malem",
        },
        "icon": "mdi:battery",
    }
    mqtt_client.publish(TOPIC_DISC_BATTERY, json.dumps(payload), retain=True)
    log.info("Published battery discovery config")


def publish_moisture(state: str, attributes: dict = None):
    mqtt_client.publish(TOPIC_MOISTURE_STATE, state, retain=True)
    if attributes:
        mqtt_client.publish(TOPIC_MOISTURE_ATTRS, json.dumps(attributes), retain=True)
    log.info(f"Moisture → {state}" + (f"  {attributes}" if attributes else ""))


def publish_battery(level: int):
    mqtt_client.publish(TOPIC_BATTERY_STATE, str(level), retain=True)
    log.info(f"Battery → {level}%")


# ── auth ──────────────────────────────────────────────────────────────────────

def generate_auth_response(tempid: int) -> bytes:
    ranXOR = KEY_TABLE[tempid]

    temp1 = random.randint(0, 255)
    temp2 = random.randint(0, 255)
    temp3 = random.randint(128, 255)
    temp4 = random.randint(0, 255)
    temp5 = random.randint(128, 255)

    t1 = temp1 >> 4;  t2 = temp1 & 0xF
    t3 = temp2 >> 4;  t4 = temp2 & 0xF
    t5 = temp3 >> 4;  t6 = temp3 & 0xF
    t7 = temp4 >> 4;  t8 = temp4 & 0xF
    t9 = temp5 >> 4;  t10 = temp5 & 0xF

    ans = ((t1 + t6 + t7) * (t3 + t4)) + ((t5 + t10) * t9) + t2 + t8

    send = [0] * 20
    send[0]  = ranXOR ^ 0x57
    send[1]  = ranXOR ^ 0x8B
    send[2]  = ranXOR ^ 0x33
    send[3]  = ranXOR ^ 0x17
    send[4]  = ranXOR ^ 0xCE
    send[5]  = ranXOR ^ 0xA0
    send[6]  = ranXOR ^ 0x99
    send[7]  = ranXOR ^ 0x85
    send[8]  = ranXOR ^ 0x02
    send[9]  = ranXOR ^ temp1
    send[10] = ranXOR ^ temp2
    send[11] = ranXOR ^ temp3
    send[12] = ranXOR ^ temp4
    send[13] = ranXOR ^ temp5
    send[14] = tempid
    send[15] = ranXOR ^ 0x4F
    send[16] = ranXOR ^ 0x2A
    send[17] = ranXOR ^ 0xE1
    send[18] = ranXOR ^ ((ans >> 8) & 0xFF)
    send[19] = ranXOR ^ (ans & 0xFF)

    for i in range(20):
        send[i] ^= 0x13
        send[i] ^= 0x46

    return bytes(send)


async def authenticate(client: BleakClient) -> bool:
    try:
        t0 = time.monotonic()
        challenge = await client.read_gatt_char(CHAR_AUTH_CHALLENGE)
        tempid = challenge[0] & 0xFF
        response = generate_auth_response(tempid)
        await client.write_gatt_char(CHAR_AUTH_WRITE, response, response=True)
        log.info(f"Authenticated in {time.monotonic()-t0:.2f}s")
        return True
    except BleakError as e:
        log.error(f"Auth failed: {e}")
        return False


# ── battery ───────────────────────────────────────────────────────────────────

async def read_battery(client: BleakClient) -> int | None:
    try:
        data = await client.read_gatt_char(CHAR_BATTERY)
        level = data[0] & 0xFF
        log.info(f"Battery level: {level}%")
        return level
    except BleakError:
        log.info("Battery characteristic not available on this device")
        return None


# ── monitor ───────────────────────────────────────────────────────────────────

async def monitor(client: BleakClient, device_address: str, has_battery: bool):
    wet = False
    wet_start = None
    last_ab = None
    last_battery_read = time.monotonic()

    pending = []
    def on_notify(sender, data):
        pending.append(bytes(data))

    await client.start_notify(CHAR_STATUS, on_notify)
    await asyncio.sleep(0.3)

    publish_moisture("dry", {"sensor_address": device_address, "status": "monitoring"})

    while client.is_connected:
        try:
            polled = await client.read_gatt_char(CHAR_STATUS)
        except BleakError as e:
            log.error(f"Poll error: {e}")
            break

        to_process = []
        while pending:
            to_process.append(pending.pop(0))
        last_notify_status = to_process[-1][0] if to_process else None
        if polled[0] != last_notify_status:
            to_process.append(polled)

        for data in to_process:
            status = data[0]
            log.debug(f"Status 0x{status:02X}")

            if status == 0xAA:
                if not wet:
                    wet = True
                    wet_start = time.monotonic()
                    last_ab = None
                    log.info("Wet event started")
                    publish_moisture("wet", {
                        "sensor_address": device_address,
                        "wet_since": datetime.now().isoformat(),
                        "status": "wet",
                    })

            elif status == 0xAB:
                if wet:
                    last_ab = time.monotonic()

            elif status == 0xAC:
                if wet:
                    raw = data[1] & 0xFF if len(data) > 1 else 0
                    device_secs = 1 if raw == 0 else min(raw * 5, 600)
                    wet = False
                    pending.clear()
                    log.info(f"Dry — duration {device_secs}s")
                    publish_moisture("dry", {
                        "sensor_address": device_address,
                        "last_wet_duration": device_secs,
                        "last_wet_time": datetime.now().isoformat(),
                        "status": "dry",
                    })

        if wet and last_ab is not None:
            if time.monotonic() - last_ab > DRY_TIMEOUT:
                local_secs = int(time.monotonic() - wet_start)
                wet = False
                log.warning(f"Dry (timeout fallback) — local duration {local_secs}s")
                publish_moisture("dry", {
                    "sensor_address": device_address,
                    "last_wet_duration": local_secs,
                    "last_wet_time": datetime.now().isoformat(),
                    "status": "dry (timeout)",
                })

        if has_battery and time.monotonic() - last_battery_read > BATTERY_POLL_INTERVAL:
            level = await read_battery(client)
            if level is not None:
                publish_battery(level)
            last_battery_read = time.monotonic()

        await asyncio.sleep(POLL_INTERVAL)

    log.warning("BLE connection lost")
    mqtt_client.publish(TOPIC_AVAILABILITY, "offline", retain=True)


# ── scanner ───────────────────────────────────────────────────────────────────

async def find_and_save_device() -> str:
    """
    Scan for the sensor by name, save its MAC to the state file, then
    exit with code 2 to signal run.sh to write the BlueZ cache and restart.
    This only runs on first install (no MAC in state file or config override).
    """
    log.info(f"First run — scanning for {DEVICE_NAME} to discover MAC address...")
    log.info("This may take up to 30 seconds.")

    found = asyncio.Event()
    result = [None, None]

    def on_device(device, adv):
        if device.name and DEVICE_NAME in device.name:
            result[0] = device.address
            result[1] = adv.rssi
            found.set()

    async with BleakScanner(detection_callback=on_device):
        await asyncio.wait_for(found.wait(), timeout=60.0)

    address, rssi = result
    log.info(f"Found {DEVICE_NAME} at {address} (RSSI {rssi} dBm)")
    log.info(f"Saving MAC address to {STATE_FILE}")

    state = load_state()
    state["sensor_mac"] = address
    state["sensor_name"] = DEVICE_NAME
    save_state(state)

    log.info("MAC saved — restarting to apply BlueZ cache and connect...")
    # Exit code 2 signals run.sh to write the cache and restart the addon
    sys.exit(2)


async def find_device(address: str) -> tuple[str, int]:
    """Confirm a known device is advertising and return its current RSSI."""
    log.info(f"Scanning for {address}...")
    found = asyncio.Event()
    result = [None, None]

    def on_device(device, adv):
        if device.address.upper() == address.upper():
            result[0] = device.address
            result[1] = adv.rssi
            found.set()

    async with BleakScanner(detection_callback=on_device):
        await asyncio.wait_for(found.wait(), timeout=30.0)

    log.info(f"Found {result[0]} (RSSI {result[1]} dBm)")
    return result[0], result[1]


# ── main ──────────────────────────────────────────────────────────────────────

async def main():
    mqtt_connect()
    await asyncio.sleep(2)

    # Determine MAC address — priority: config override → state file → first-run scan
    if SENSOR_MAC_OVERRIDE:
        address = SENSOR_MAC_OVERRIDE
        log.info(f"Using MAC from config override: {address}")
    else:
        state = load_state()
        if "sensor_mac" in state:
            address = state["sensor_mac"].upper()
            log.info(f"Using saved MAC address: {address}")
        else:
            # First run — scan, save, restart
            await find_and_save_device()
            return  # unreachable — find_and_save_device exits

    failures = 0

    while True:
        try:
            log.info(f"Connecting to {address}...")
            async with BleakClient(address, timeout=15.0) as client:
                log.info("Connected")
                mqtt_client.publish(TOPIC_AVAILABILITY, "online", retain=True)

                if not await authenticate(client):
                    failures += 1
                    await asyncio.sleep(3)
                    continue

                failures = 0

                battery = await read_battery(client)
                has_battery = battery is not None
                if has_battery:
                    publish_discovery_battery()
                    publish_battery(battery)

                await monitor(client, address, has_battery)

        except asyncio.TimeoutError:
            log.warning("Scan timed out — retrying")
            await asyncio.sleep(2)

        except BleakError as e:
            failures += 1
            log.error(f"BLE error ({failures}): {e}")
            if failures % 3 == 0:
                log.warning("3 consecutive failures — will retry")
            await asyncio.sleep(3)

        except Exception as e:
            log.error(f"Unexpected error: {e}", exc_info=True)
            await asyncio.sleep(3)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        mqtt_client.publish(TOPIC_AVAILABILITY, "offline", retain=True)
        mqtt_client.disconnect()
