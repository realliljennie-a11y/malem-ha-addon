"""
Malem Alarm — Home Assistant Add-on
Listens for Malem BLE moisture sensor events and publishes to MQTT
with Home Assistant MQTT discovery, so the sensor auto-appears in HA.

On first run the add-on scans for the sensor by name and saves its MAC
address to /config/malem_state.json. On all subsequent runs the MAC is
loaded from that file — no manual configuration required.

Connection strategy:
  The Malem device disconnects if auth doesn't complete within ~3 seconds.
  Bleak's normal connect() does two slow things: a pre-connect BLE scan, and
  service discovery (waiting for BlueZ ServicesResolved). We bypass both:

  1. Pre-set _device_path so the pre-connect scan is skipped.
  2. Auth using raw D-Bus calls (GetManagedObjects to find characteristic
     paths, then ReadValue/WriteValue directly) — this is fast because it
     uses the D-Bus object manager cache, not BLE traffic.
  3. After auth, trigger normal service discovery at leisure (no time limit).

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
from bleak.backends.service import BleakGATTServiceCollection
from bleak.exc import BleakError

# ── config ────────────────────────────────────────────────────────────────────

MQTT_HOST           = os.environ.get("MQTT_HOST", "core-mosquitto")
MQTT_PORT           = int(os.environ.get("MQTT_PORT", 1883))
MQTT_USERNAME       = os.environ.get("MQTT_USERNAME", "")
MQTT_PASSWORD       = os.environ.get("MQTT_PASSWORD", "")
TOPIC_PREFIX        = os.environ.get("MQTT_TOPIC_PREFIX", "malem")
DRY_TIMEOUT         = int(os.environ.get("DRY_TIMEOUT", 15))
LOG_LEVEL           = os.environ.get("LOG_LEVEL", "info").upper()
SENSOR_MAC_OVERRIDE = os.environ.get("SENSOR_MAC", "").strip().upper()

STATE_FILE = "/config/malem_state.json"

# ── logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("malem")

# ── BLE constants ─────────────────────────────────────────────────────────────

DEVICE_NAME         = "MALEM ALARM"
CHAR_AUTH_WRITE     = "0000fff1-0000-1000-8000-00805f9b34fb"
CHAR_AUTH_CHALLENGE = "0000fff2-0000-1000-8000-00805f9b34fb"
CHAR_STATUS         = "0000fff4-0000-1000-8000-00805f9b34fb"
CHAR_BATTERY        = "00002a19-0000-1000-8000-00805f9b34fb"

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

# ── auth key generation ───────────────────────────────────────────────────────

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


# ── BLE connection ────────────────────────────────────────────────────────────

async def connect_fast(address: str) -> BleakClient:
    """
    Connect to the device with the pre-connect scan bypassed.
    Service discovery is NOT bypassed here — instead auth_via_dbus() does
    auth before discovery completes, then discovery finishes at leisure.
    """
    mac_nodash = address.upper().replace(":", "_")
    device_path = f"/org/bluez/hci0/dev_{mac_nodash}"

    client = BleakClient(address, timeout=15.0)
    # Pre-set device path to skip BleakScanner.find_device_by_address()
    client._backend._device_path = device_path

    # Use cache if available (dangerous_use_bleak_cache skips BLE re-discovery
    # if BlueZ has previously cached this device's services)
    await client.connect(dangerous_use_bleak_cache=True)

    if not client.is_connected:
        raise BleakError("Failed to connect")

    return client


async def auth_via_dbus(client: BleakClient) -> bool:
    """
    Authenticate using raw D-Bus calls, bypassing the service collection.

    Uses BlueZ's GetManagedObjects to find characteristic D-Bus paths for
    our specific UUIDs — one fast D-Bus roundtrip with no BLE traffic.
    This completes well within the 3-second auth window even when service
    discovery is still in progress.
    """
    from dbus_fast.message import Message
    from dbus_fast.signature import Variant
    from bleak.backends.bluezdbus import defs

    bus = client._backend._bus
    device_path = client._backend._device_path

    if bus is None or device_path is None:
        log.error("Auth failed: no D-Bus connection")
        return False

    # Get all BlueZ objects in one D-Bus call — uses kernel object cache,
    # not BLE traffic, so this is fast regardless of discovery state
    try:
        reply = await bus.call(
            Message(
                destination=defs.BLUEZ_SERVICE,
                path="/",
                interface=defs.OBJECT_MANAGER_INTERFACE,
                member="GetManagedObjects",
            )
        )
    except Exception as e:
        log.error(f"GetManagedObjects failed: {e}")
        return False

    if reply.message_type.name == "ERROR":
        log.error(f"GetManagedObjects error: {reply.error_name}")
        return False

    objects = reply.body[0]

    # Find D-Bus paths for our two auth characteristics
    challenge_path = None
    write_path = None

    for path, interfaces in objects.items():
        if not path.startswith(device_path):
            continue
        char_iface = interfaces.get(defs.GATT_CHARACTERISTIC_INTERFACE)
        if char_iface is None:
            continue
        uuid_variant = char_iface.get("UUID", {})
        uuid_val = uuid_variant.value if hasattr(uuid_variant, "value") else str(uuid_variant)
        if uuid_val.lower() == CHAR_AUTH_CHALLENGE.lower():
            challenge_path = path
        elif uuid_val.lower() == CHAR_AUTH_WRITE.lower():
            write_path = path

    if not challenge_path or not write_path:
        log.error("Auth characteristics not found in D-Bus objects")
        log.debug(f"Paths under {device_path}: "
                  f"{[p for p in objects if p.startswith(device_path)]}")
        return False

    log.debug(f"Challenge: {challenge_path}")
    log.debug(f"Write:     {write_path}")

    try:
        t0 = time.monotonic()

        # Read challenge byte
        reply = await bus.call(
            Message(
                destination=defs.BLUEZ_SERVICE,
                path=challenge_path,
                interface=defs.GATT_CHARACTERISTIC_INTERFACE,
                member="ReadValue",
                signature="a{sv}",
                body=[{}],
            )
        )
        if reply.message_type.name == "ERROR":
            log.error(f"Challenge read failed: {reply.error_name}")
            return False

        tempid = reply.body[0][0] & 0xFF
        response = generate_auth_response(tempid)

        # Write auth response
        reply = await bus.call(
            Message(
                destination=defs.BLUEZ_SERVICE,
                path=write_path,
                interface=defs.GATT_CHARACTERISTIC_INTERFACE,
                member="WriteValue",
                signature="aya{sv}",
                body=[bytes(response), {"type": Variant("s", "request")}],
            )
        )
        if reply.message_type.name == "ERROR":
            log.error(f"Auth write failed: {reply.error_name}")
            return False

        log.info(f"Authenticated in {time.monotonic()-t0:.2f}s")
        return True

    except Exception as e:
        log.error(f"Auth failed: {e}")
        return False


async def wait_for_services(client: BleakClient, timeout: float = 10.0) -> bool:
    """
    Wait for service discovery to complete after auth.
    No time pressure here — device stays connected indefinitely once authed.
    """
    if client.services is not None:
        return True

    log.info("Waiting for service discovery to complete...")
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        if client.services is not None:
            log.info("Services resolved")
            return True
        await asyncio.sleep(0.2)

    # Try forcing discovery via _get_services
    try:
        await client._backend._get_services()
        log.info("Services resolved via _get_services()")
        return True
    except Exception as e:
        log.error(f"Service discovery failed: {e}")
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
    """First-run: scan by name, save MAC, exit so s6 restarts with MAC known."""
    log.info(f"First run — scanning for {DEVICE_NAME} to discover MAC address...")
    log.info("This may take up to 60 seconds.")

    found = asyncio.Event()
    result = [None, None]

    def on_device(device, adv):
        if device.name and DEVICE_NAME in device.name:
            result[0] = device.address
            result[1] = adv.rssi
            found.set()

    async with BleakScanner(detection_callback=on_device):
        await asyncio.wait_for(found.wait(), timeout=60.0)

    address = result[0]
    log.info(f"Found {DEVICE_NAME} at {address} (RSSI {result[1]} dBm)")

    state = load_state()
    state["sensor_mac"] = address
    state["sensor_name"] = DEVICE_NAME
    save_state(state)
    log.info(f"MAC address saved to {STATE_FILE}")
    log.info("Restarting to connect...")
    sys.exit(2)


# ── main ──────────────────────────────────────────────────────────────────────

async def main():
    mqtt_connect()
    await asyncio.sleep(2)

    # Determine MAC — priority: config override → state file → first-run scan
    if SENSOR_MAC_OVERRIDE:
        address = SENSOR_MAC_OVERRIDE
        log.info(f"Using MAC from config: {address}")
    else:
        state = load_state()
        if "sensor_mac" in state:
            address = state["sensor_mac"].upper()
            log.info(f"Using saved MAC: {address}")
        else:
            await find_and_save_device()
            return  # unreachable

    failures = 0

    while True:
        try:
            log.info(f"Connecting to {address}...")
            client = await connect_fast(address)
            log.info("Connected")

            try:
                mqtt_client.publish(TOPIC_AVAILABILITY, "online", retain=True)

                # Auth via raw D-Bus before service discovery completes
                if not await auth_via_dbus(client):
                    failures += 1
                    await asyncio.sleep(3)
                    continue

                failures = 0

                # Now wait for service discovery to complete — no time pressure
                if not await wait_for_services(client):
                    log.error("Service discovery failed — reconnecting")
                    continue

                battery = await read_battery(client)
                has_battery = battery is not None
                if has_battery:
                    publish_discovery_battery()
                    publish_battery(battery)

                await monitor(client, address, has_battery)

            finally:
                if client.is_connected:
                    await client.disconnect()

        except asyncio.TimeoutError:
            log.warning("Connection timed out — retrying")
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
