#!/usr/bin/with-contenv bashio

export MQTT_HOST=$(bashio::config 'mqtt_host')
export MQTT_PORT=$(bashio::config 'mqtt_port')
export MQTT_USERNAME=$(bashio::config 'mqtt_username')
export MQTT_PASSWORD=$(bashio::config 'mqtt_password')
export MQTT_TOPIC_PREFIX=$(bashio::config 'mqtt_topic_prefix')
export DRY_TIMEOUT=$(bashio::config 'dry_timeout')
export LOG_LEVEL=$(bashio::config 'log_level')
export SENSOR_MAC=$(bashio::config 'sensor_mac')

bashio::log.info "Starting Malem Alarm listener..."
bashio::log.info "MQTT: ${MQTT_HOST}:${MQTT_PORT}, prefix: ${MQTT_TOPIC_PREFIX}"

# ── Write BlueZ GATT service cache ────────────────────────────────────────────
# Pre-populating the cache with correct handle numbers (derived from debug logs
# of a live connection) tells BlueZ the full service layout so it can skip BLE
# service discovery on connect. This is critical because the Malem sensor
# disconnects if auth doesn't complete within ~3 seconds.
#
# Handles are fixed by device firmware and never change between connections.

write_cache() {
    local MAC="$1"
    local MAC_NODASH="${MAC//:/_}"

    for ADAPTER_DIR in /var/lib/bluetooth/*/; do
        [ -d "$ADAPTER_DIR" ] || continue
        CACHE_DIR="${ADAPTER_DIR}cache"
        mkdir -p "$CACHE_DIR"
        CACHE_FILE="${CACHE_DIR}/${MAC}"

        bashio::log.info "Writing BlueZ service cache: ${CACHE_FILE}"

        cat > "$CACHE_FILE" << CACHE
[General]
Name=MALEM ALARM

[ServiceChanged]
Value=

[/org/bluez/hci0/dev_${MAC_NODASH}/service0001]
UUID=00001800-0000-1000-8000-00805f9b34fb
Primary=true
Device=/org/bluez/hci0/dev_${MAC_NODASH}

[/org/bluez/hci0/dev_${MAC_NODASH}/service0001/char0002]
UUID=00002a00-0000-1000-8000-00805f9b34fb
Value=
Flags=read

[/org/bluez/hci0/dev_${MAC_NODASH}/service000c]
UUID=00001801-0000-1000-8000-00805f9b34fb
Primary=true
Device=/org/bluez/hci0/dev_${MAC_NODASH}

[/org/bluez/hci0/dev_${MAC_NODASH}/service0010]
UUID=0000180a-0000-1000-8000-00805f9b34fb
Primary=true
Device=/org/bluez/hci0/dev_${MAC_NODASH}

[/org/bluez/hci0/dev_${MAC_NODASH}/service0023]
UUID=0000180f-0000-1000-8000-00805f9b34fb
Primary=true
Device=/org/bluez/hci0/dev_${MAC_NODASH}

[/org/bluez/hci0/dev_${MAC_NODASH}/service0023/char0024]
UUID=00002a19-0000-1000-8000-00805f9b34fb
Value=
Flags=read,notify

[/org/bluez/hci0/dev_${MAC_NODASH}/service0028]
UUID=0000fff0-0000-1000-8000-00805f9b34fb
Primary=true
Device=/org/bluez/hci0/dev_${MAC_NODASH}

[/org/bluez/hci0/dev_${MAC_NODASH}/service0028/char0029]
UUID=0000fff1-0000-1000-8000-00805f9b34fb
Value=
Flags=read,write

[/org/bluez/hci0/dev_${MAC_NODASH}/service0028/char002c]
UUID=0000fff2-0000-1000-8000-00805f9b34fb
Value=
Flags=read

[/org/bluez/hci0/dev_${MAC_NODASH}/service0028/char002f]
UUID=0000fff3-0000-1000-8000-00805f9b34fb
Value=
Flags=write

[/org/bluez/hci0/dev_${MAC_NODASH}/service0028/char0032]
UUID=0000fff4-0000-1000-8000-00805f9b34fb
Value=
Flags=read,notify

[/org/bluez/hci0/dev_${MAC_NODASH}/service0028/char0032/desc0034]
UUID=00002902-0000-1000-8000-00805f9b34fb
Value=0000

[/org/bluez/hci0/dev_${MAC_NODASH}/service0028/char0036]
UUID=0000fff5-0000-1000-8000-00805f9b34fb
Value=
Flags=read
CACHE
    done
}

# Determine MAC to cache — config override takes priority, then state file
CACHE_MAC=""
if [ -n "$SENSOR_MAC" ]; then
    CACHE_MAC="$SENSOR_MAC"
    bashio::log.info "Using MAC from config: ${CACHE_MAC}"
elif [ -f "/config/malem_state.json" ]; then
    CACHE_MAC=$(python3 -c "import json; d=json.load(open('/config/malem_state.json')); print(d.get('sensor_mac',''))" 2>/dev/null)
    if [ -n "$CACHE_MAC" ]; then
        bashio::log.info "Using saved MAC from state file: ${CACHE_MAC}"
    fi
fi

if [ -n "$CACHE_MAC" ]; then
    write_cache "$CACHE_MAC"
else
    bashio::log.info "No MAC known yet — will discover on first run"
fi

exec python3 /app/malem_alarm.py
