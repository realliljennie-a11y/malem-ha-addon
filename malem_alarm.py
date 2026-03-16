"""
Malem Alarm — Home Assistant Add-on
Listens for Malem BLE moisture sensor events and publishes to MQTT
with Home Assistant MQTT discovery, so the sensor auto-appears in HA.

MQTT topics published:
  {prefix}/sensor/state          "wet" | "dry"
  {prefix}/sensor/attributes     JSON: last_wet_duration, last_wet_time, rssi
  {prefix}/sensor/availability   "online" | "offline"

HA discovery topic:
  homeassistant/binary_sensor/{prefix}/config
"""

import asyncio
import json
import logging
import os
import random
import time
from datetime import datetime

import paho.mqtt.client as mqtt
from bleak import BleakScanner, BleakClient
from bleak.exc import BleakError

# ── config from environment (set by run.sh from HA options) ──────────────────

MQTT_HOST         = os.environ.get("MQTT_HOST", "core-mosquitto")
MQTT_PORT         = int(os.environ.get("MQTT_PORT", 1883))
MQTT_USERNAME     = os.environ.get("MQTT_USERNAME", "")
MQTT_PASSWORD     = os.environ.get("MQTT_PASSWORD", "")
TOPIC_PREFIX      = os.environ.get("MQTT_TOPIC_PREFIX", "malem")
DRY_TIMEOUT       = int(os.environ.get("DRY_TIMEOUT", 15))
LOG_LEVEL         = os.environ.get("LOG_LEVEL", "info").upper()

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

KEY_TABLE = [0x79, 0xA8, 0xBF, 0x90, 0x88, 0x3E, 0x45, 0x13,
             0x46, 0x6C, 0xF9, 0xD7, 0x20, 0xCA, 0x58, 0xDA]

POLL_INTERVAL = 1.5

# ── MQTT topics ───────────────────────────────────────────────────────────────

TOPIC_STATE        = f"{TOPIC_PREFIX}/sensor/state"
TOPIC_ATTRIBUTES   = f"{TOPIC_PREFIX}/sensor/attributes"
TOPIC_AVAILABILITY = f"{TOPIC_PREFIX}/sensor/availability"
TOPIC_DISCOVERY    = f"homeassistant/binary_sensor/{TOPIC_PREFIX}/config"

# ── MQTT client ───────────────────────────────────────────────────────────────

mqtt_client = mqtt.Client(client_id=f"malem_addon")

def mqtt_connect():
    if MQTT_USERNAME:
        mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

    # Publish "offline" as last will if we disconnect unexpectedly
    mqtt_client.will_set(TOPIC_AVAILABILITY, "offline", retain=True)

    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            log.info(f"MQTT connected to {MQTT_HOST}:{MQTT_PORT}")
            publish_discovery()
            client.publish(TOPIC_AVAILABILITY, "online", retain=True)
        else:
            log.error(f"MQTT connection failed: rc={rc}")

    def on_disconnect(client, userdata, rc):
        if rc != 0:
            log.warning("MQTT disconnected unexpectedly, will retry")

    mqtt_client.on_connect = on_connect
    mqtt_client.on_disconnect = on_disconnect
    mqtt_client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
    mqtt_client.loop_start()


def publish_discovery():
    """
    Publish HA MQTT discovery config so the sensor auto-appears in HA
    as a binary moisture sensor with availability tracking.
    """
    payload = {
        "name": "Malem Moisture Sensor",
        "unique_id": f"{TOPIC_PREFIX}_moisture",
        "device_class": "moisture",
        "state_topic": TOPIC_STATE,
        "payload_on": "wet",
        "payload_off": "dry",
        "availability_topic": TOPIC_AVAILABILITY,
        "payload_available": "online",
        "payload_not_available": "offline",
        "json_attributes_topic": TOPIC_ATTRIBUTES,
        "device": {
            "identifiers": [TOPIC_PREFIX],
            "name": "Malem Alarm",
            "model": "Malem BLE Moisture Sensor",
            "manufacturer": "Malem",
        },
        "icon": "mdi:water-alert",
    }
    mqtt_client.publish(TOPIC_DISCOVERY, json.dumps(payload), retain=True)
    log.info("Published MQTT discovery config")


def publish_state(state: str, attributes: dict = None):
    mqtt_client.publish(TOPIC_STATE, state, retain=True)
    if attributes:
        mqtt_client.publish(TOPIC_ATTRIBUTES, json.dumps(attributes), retain=True)
    log.info(f"State → {state}" + (f"  attrs={attributes}" if attributes else ""))


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


# ── monitor ───────────────────────────────────────────────────────────────────

async def monitor(client: BleakClient, device_address: str):
    wet = False
    wet_start = None
    last_ab = None

    pending = []
    def on_notify(sender, data):
        pending.append(bytes(data))

    await client.start_notify(CHAR_STATUS, on_notify)
    await asyncio.sleep(0.3)

    publish_state("dry", {"sensor_address": device_address, "status": "monitoring"})

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
                    publish_state("wet", {
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
                    local_secs = int(time.monotonic() - wet_start)
                    wet = False
                    pending.clear()
                    log.info(f"Dry — duration {device_secs}s")
                    publish_state("dry", {
                        "sensor_address": device_address,
                        "last_wet_duration": device_secs,
                        "last_wet_time": datetime.now().isoformat(),
                        "status": "dry",
                    })

        # Timeout fallback
        if wet and last_ab is not None:
            if time.monotonic() - last_ab > DRY_TIMEOUT:
                local_secs = int(time.monotonic() - wet_start)
                wet = False
                log.warning(f"Dry (timeout fallback) — local duration {local_secs}s")
                publish_state("dry", {
                    "sensor_address": device_address,
                    "last_wet_duration": local_secs,
                    "last_wet_time": datetime.now().isoformat(),
                    "status": "dry (timeout)",
                })

        await asyncio.sleep(POLL_INTERVAL)

    log.warning("BLE connection lost")
    mqtt_client.publish(TOPIC_AVAILABILITY, "offline", retain=True)


# ── scanner ───────────────────────────────────────────────────────────────────

async def find_device() -> tuple[str, int]:
    """Returns (address, rssi)."""
    log.info(f"Scanning for {DEVICE_NAME}...")
    found = asyncio.Event()
    result = [None, None]

    def on_device(device, adv):
        if device.name and DEVICE_NAME in device.name:
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
    # Give MQTT a moment to connect before we start publishing
    await asyncio.sleep(2)

    address = None
    rssi = None
    failures = 0

    while True:
        try:
            if address is None:
                address, rssi = await find_device()

            log.info(f"Connecting to {address}...")
            async with BleakClient(address, timeout=15.0) as client:
                log.info("Connected")
                mqtt_client.publish(TOPIC_AVAILABILITY, "online", retain=True)

                if not await authenticate(client):
                    failures += 1
                    await asyncio.sleep(3)
                    continue

                failures = 0
                await monitor(client, address)

        except asyncio.TimeoutError:
            log.warning("Scan timed out — rescanning")
            address = None
            await asyncio.sleep(2)

        except BleakError as e:
            failures += 1
            log.error(f"BLE error ({failures}): {e}")
            if failures % 3 == 0:
                log.warning("3 consecutive failures — rescanning")
                address = None
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
