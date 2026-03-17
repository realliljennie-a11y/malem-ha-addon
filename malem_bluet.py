"""
Malem BlueT — Home Assistant Add-on
Listens for Malem BlueT BLE moisture sensor events and publishes to MQTT
with Home Assistant MQTT discovery, so the sensor auto-appears in HA.

Connection strategy:
  The Malem device disconnects if auth doesn't complete within ~3 seconds
  of connection. client.connect() blocks until BlueZ service discovery
  completes (~3-8s on a cold connection) — too slow.

  Fix: run connect() as a background task, poll until the BLE link is up
  (~0.2s), then immediately auth via hardcoded D-Bus paths (~0.1s). Let
  discovery finish in the background with no time pressure.

  D-Bus paths (fixed by device firmware, confirmed from live D-Bus trace):
    fff1 (auth write):     service0028/char0029
    fff2 (auth challenge): service0028/char002c
    fff4 (status):         service0028/char0032
    2a19 (battery):        service0023/char0024

Recovery strategy:
  - After clean disconnect: remove device from BlueZ, rescan, reconnect
  - After discovery failures < 20s: retry without disturbing BlueZ state
  - After discovery failures > 20s: remove device, wait, retry
  - After adapter appears stuck: btmgmt power cycle
"""

import asyncio
import json
import logging
import os
import random
import subprocess
import sys
import time
from datetime import datetime

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion
from bleak import BleakScanner, BleakClient
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

# ── BLE / D-Bus constants ─────────────────────────────────────────────────────

DEVICE_NAME         = "MALEM ALARM"
CHAR_AUTH_WRITE     = "0000fff1-0000-1000-8000-00805f9b34fb"
CHAR_AUTH_CHALLENGE = "0000fff2-0000-1000-8000-00805f9b34fb"
CHAR_STATUS         = "0000fff4-0000-1000-8000-00805f9b34fb"
CHAR_BATTERY        = "00002a19-0000-1000-8000-00805f9b34fb"

KEY_TABLE = [0x79, 0xA8, 0xBF, 0x90, 0x88, 0x3E, 0x45, 0x13,
             0x46, 0x6C, 0xF9, 0xD7, 0x20, 0xCA, 0x58, 0xDA]

POLL_INTERVAL         = 1.5
BATTERY_POLL_INTERVAL = 300


def dbus_paths(address: str) -> dict:
    """D-Bus object paths for Malem characteristics. Fixed by device firmware."""
    mac = address.upper().replace(":", "_")
    dev = f"/org/bluez/hci0/dev_{mac}"
    return {
        "device":         dev,
        "auth_write":     f"{dev}/service0028/char0029",  # fff1
        "auth_challenge": f"{dev}/service0028/char002c",  # fff2
        "status":         f"{dev}/service0028/char0032",  # fff4
        "status_cccd":    f"{dev}/service0028/char0032/desc0034",
        "battery":        f"{dev}/service0023/char0024",  # 2a19
    }


# ── MQTT topics ───────────────────────────────────────────────────────────────

TOPIC_MOISTURE_STATE = f"{TOPIC_PREFIX}/moisture/state"
TOPIC_MOISTURE_ATTRS = f"{TOPIC_PREFIX}/moisture/attributes"
TOPIC_AVAILABILITY   = f"{TOPIC_PREFIX}/moisture/availability"
TOPIC_BATTERY_STATE  = f"{TOPIC_PREFIX}/battery/state"
TOPIC_DISC_MOISTURE  = f"homeassistant/binary_sensor/{TOPIC_PREFIX}_moisture/config"
TOPIC_DISC_BATTERY   = f"homeassistant/sensor/{TOPIC_PREFIX}_battery/config"

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


async def auth_via_dbus(client: BleakClient, address: str) -> bool:
    """
    Authenticate using raw D-Bus calls with hardcoded characteristic paths.
    Completes in ~0.1s regardless of discovery state.
    """
    from dbus_fast.message import Message
    from dbus_fast.signature import Variant
    from bleak.backends.bluezdbus import defs

    bus = client._backend._bus
    if bus is None:
        log.error("Auth: no D-Bus bus")
        return False

    paths = dbus_paths(address)

    try:
        t0 = time.monotonic()

        reply = await bus.call(Message(
            destination=defs.BLUEZ_SERVICE,
            path=paths["auth_challenge"],
            interface=defs.GATT_CHARACTERISTIC_INTERFACE,
            member="ReadValue",
            signature="a{sv}",
            body=[{}],
        ))
        if reply.message_type.name == "ERROR":
            log.error(f"Challenge read failed: {reply.error_name} — {reply.body}")
            return False

        tempid = reply.body[0][0] & 0xFF
        log.debug(f"Challenge tempid={tempid}")
        response = generate_auth_response(tempid)

        reply = await bus.call(Message(
            destination=defs.BLUEZ_SERVICE,
            path=paths["auth_write"],
            interface=defs.GATT_CHARACTERISTIC_INTERFACE,
            member="WriteValue",
            signature="aya{sv}",
            body=[bytes(response), {"type": Variant("s", "request")}],
        ))
        if reply.message_type.name == "ERROR":
            log.error(f"Auth write failed: {reply.error_name} — {reply.body}")
            return False

        log.info(f"Authenticated in {time.monotonic()-t0:.2f}s")
        return True

    except Exception as e:
        log.error(f"Auth failed: {e}")
        return False


# ── BlueZ management ──────────────────────────────────────────────────────────

async def remove_device(address: str):
    """Remove device from BlueZ, clearing stale connection/service state."""
    from dbus_fast.message import Message
    from bleak.backends.bluezdbus import defs
    from bleak.backends.bluezdbus.manager import get_global_bluez_manager

    try:
        manager = await get_global_bluez_manager()
        adapter_path = manager.get_default_adapter()
        mac_nodash = address.upper().replace(":", "_")
        device_path = f"{adapter_path}/dev_{mac_nodash}"
        assert manager._bus is not None
        await manager._bus.call(Message(
            destination=defs.BLUEZ_SERVICE,
            path=adapter_path,
            interface=defs.ADAPTER_INTERFACE,
            member="RemoveDevice",
            signature="o",
            body=[device_path],
        ))
        log.info(f"Removed {address} from BlueZ")
    except Exception as e:
        log.debug(f"Remove device (non-fatal): {e}")


async def reset_adapter():
    """Power-cycle BT adapter to flush stuck firmware state."""
    try:
        log.warning("Resetting Bluetooth adapter...")
        subprocess.run(["btmgmt", "power", "off"], capture_output=True, timeout=5)
        await asyncio.sleep(2)
        subprocess.run(["btmgmt", "power", "on"], capture_output=True, timeout=5)
        await asyncio.sleep(3)
        log.warning("Bluetooth adapter reset complete")
    except Exception as e:
        log.error(f"Adapter reset failed: {e}")


# ── connection ────────────────────────────────────────────────────────────────

async def scan_for_device(address: str):
    """Scan until device is seen, returning BLEDevice with correct D-Bus path."""
    log.info(f"Scanning for {address}...")
    found = asyncio.Event()
    result = [None]

    def on_device(device, adv):
        if device.address.upper() == address.upper():
            result[0] = device
            found.set()

    async with BleakScanner(detection_callback=on_device):
        await asyncio.wait_for(found.wait(), timeout=30.0)

    log.info(f"Device found (RSSI {getattr(result[0], 'rssi', '?')} dBm)")
    return result[0]


async def is_already_connected(address: str) -> bool:
    """Check if BlueZ already has an active connection to this device."""
    try:
        from bleak.backends.bluezdbus.manager import get_global_bluez_manager
        manager = await get_global_bluez_manager()
        mac_nodash = address.upper().replace(":", "_")
        adapter_path = manager.get_default_adapter()
        device_path = f"{adapter_path}/dev_{mac_nodash}"
        return manager.is_connected(device_path)
    except Exception:
        return False


async def connect_and_auth(address: str, cache_warmed: bool = False) -> BleakClient:
    """
    Connect and authenticate.

    run.sh pre-connects via bluetoothctl (1.26s) and leaves the connection
    open. We detect the existing connection and attach to it — BlueZ skips
    the BLE connect step and service discovery is instant since it already
    happened. Auth via hardcoded D-Bus paths completes in ~0.1s.

    On reconnect (after disconnect), falls back to concurrent auth approach.
    """
    # Check if bluetoothctl left us a live connection
    already_connected = await is_already_connected(address)
    if already_connected:
        log.info(f"Device already connected via bluetoothctl — attaching...")

    ble_device = await scan_for_device(address)
    log.info(f"Connecting to {address}...")

    client = BleakClient(ble_device, timeout=15.0)

    if already_connected:
        # Attach to existing connection — connect() sees device is already
        # connected and skips the BLE Connect call, going straight to
        # service lookup which is instant.
        try:
            await asyncio.wait_for(
                client.connect(dangerous_use_bleak_cache=True),
                timeout=5.0
            )
            if client.is_connected:
                log.info("Attached to existing connection")
                if await auth_via_dbus(client, address):
                    return client
                await client.disconnect()
                raise BleakError("Authentication failed")
        except asyncio.TimeoutError:
            log.warning("Attach timed out — falling through to fresh connect")
            if client.is_connected:
                await client.disconnect()
        except BleakError as e:
            if "Authentication failed" in str(e):
                raise
            log.warning(f"Attach failed: {e} — falling through to fresh connect")
            if client.is_connected:
                await client.disconnect()

    # Fresh connect — use concurrent auth approach
    ble_device = await scan_for_device(address)
    client = BleakClient(ble_device, timeout=15.0)

    connect_task = asyncio.ensure_future(
        client.connect(dangerous_use_bleak_cache=True)
    )

    auth_ok = False
    try:
        # Wait for BLE link
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            if client._backend._is_connected:
                break
        else:
            raise BleakError("Timed out waiting for BLE connection")

        log.info("Connected — polling for GATT objects...")

        paths = dbus_paths(address)
        from dbus_fast.message import Message
        from bleak.backends.bluezdbus import defs

        challenge_ready = False
        gatt_deadline = time.monotonic() + 2.5
        while time.monotonic() < gatt_deadline:
            await asyncio.sleep(0.1)
            try:
                bus = client._backend._bus
                if bus is None:
                    continue
                reply = await bus.call(Message(
                    destination=defs.BLUEZ_SERVICE,
                    path=paths["auth_challenge"],
                    interface="org.freedesktop.DBus.Properties",
                    member="Get",
                    signature="ss",
                    body=[defs.GATT_CHARACTERISTIC_INTERFACE, "UUID"],
                ))
                if reply.message_type.name != "ERROR":
                    challenge_ready = True
                    break
            except Exception:
                pass

        if not challenge_ready:
            log.warning("GATT objects not ready within 2.5s — attempting auth anyway")

        auth_ok = await auth_via_dbus(client, address)

    finally:
        if not auth_ok:
            try:
                await asyncio.wait_for(asyncio.shield(connect_task), timeout=5.0)
            except Exception:
                pass
            try:
                if client.is_connected:
                    await client.disconnect()
            except Exception:
                pass
            if not auth_ok:
                raise BleakError("Authentication failed")

    try:
        await asyncio.wait_for(asyncio.shield(connect_task), timeout=12.0)
    except (asyncio.TimeoutError, BleakError, asyncio.CancelledError) as e:
        log.warning(f"Service discovery: {type(e).__name__} — will wait separately")

    return client


async def wait_for_services(client: BleakClient, timeout: float = 15.0) -> bool:
    """Wait for service discovery to complete after auth. No time pressure."""
    if client.services is not None:
        return True

    log.info("Waiting for service discovery...")
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        if not client.is_connected:
            return False
        if client.services is not None:
            log.info("Services resolved")
            return True
        await asyncio.sleep(0.3)

    log.error("Service discovery timed out")
    return False


# ── battery ───────────────────────────────────────────────────────────────────

async def read_battery(client: BleakClient) -> int | None:
    try:
        data = await client.read_gatt_char(CHAR_BATTERY)
        level = data[0] & 0xFF
        log.info(f"Battery level: {level}%")
        return level
    except BleakError:
        log.info("Battery characteristic not available")
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
            "name": "Malem BlueT",
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
            "name": "Malem BlueT",
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


# ── first-run scanner ─────────────────────────────────────────────────────────

async def find_and_save_device():
    """First-run: scan by name, save MAC, exit so s6 restarts with MAC known."""
    log.info(f"First run — scanning for {DEVICE_NAME}...")
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
    log.info(f"MAC saved to {STATE_FILE} — restarting to connect...")
    sys.exit(2)


# ── main ──────────────────────────────────────────────────────────────────────

async def main():
    mqtt_connect()
    await asyncio.sleep(2)

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
            return

    disc_failures = 0
    auth_failures = 0
    first_disc_failure_time = None
    cache_warmed = False  # True after first successful or attempted discovery

    while True:
        client = None
        try:
            client = await connect_and_auth(address, cache_warmed)
            cache_warmed = True  # discovery has run at least once
            disc_failures = 0
            auth_failures = 0
            first_disc_failure_time = None
            mqtt_client.publish(TOPIC_AVAILABILITY, "online", retain=True)

            if not await wait_for_services(client):
                disc_failures += 1
                log.warning(f"Service discovery failed ({disc_failures}) — retrying")
                await asyncio.sleep(2)
                continue

            battery = await read_battery(client)
            has_battery = battery is not None
            if has_battery:
                publish_discovery_battery()
                publish_battery(battery)

            await monitor(client, address, has_battery)

            # Session ended — leave device registered in BlueZ so cache stays warm
            await asyncio.sleep(1)

        except asyncio.TimeoutError:
            log.warning("Scan timed out — retrying")
            await asyncio.sleep(2)

        except BleakError as e:
            err = str(e)
            log.error(f"BLE error: {e}")

            if "failed to discover services" in err:
                cache_warmed = True  # discovery ran even if it failed
                disc_failures += 1
                if first_disc_failure_time is None:
                    first_disc_failure_time = time.monotonic()
                elapsed = time.monotonic() - first_disc_failure_time

                if elapsed > 60.0:
                    # Stuck for a long time — reset adapter
                    log.warning(f"Discovery failing for {elapsed:.0f}s — resetting adapter")
                    await reset_adapter()
                    first_disc_failure_time = None
                    disc_failures = 0
                else:
                    log.info(f"Discovery failure {disc_failures} ({elapsed:.0f}s) — retrying")
                    await asyncio.sleep(3)

            elif "Authentication failed" in err:
                cache_warmed = True  # connection reached auth stage, discovery ran
                auth_failures += 1
                if auth_failures % 6 == 0:
                    log.warning(f"Auth failure {auth_failures} — resetting adapter")
                    await reset_adapter()
                else:
                    log.warning(f"Auth failure {auth_failures} — retrying")
                    await asyncio.sleep(3)

            else:
                disc_failures += 1
                if disc_failures % 6 == 0:
                    log.warning(f"Repeated failures ({disc_failures}) — resetting adapter")
                    await reset_adapter()
                await asyncio.sleep(3)

        except Exception as e:
            log.error(f"Unexpected error: {e}", exc_info=True)
            await asyncio.sleep(3)

        finally:
            if client is not None and client.is_connected:
                try:
                    await client.disconnect()
                except Exception:
                    pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        mqtt_client.publish(TOPIC_AVAILABILITY, "offline", retain=True)
        mqtt_client.disconnect()
