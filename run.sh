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
# The Malem sensor disconnects if auth doesn't complete within ~3s of connect.
# BlueZ's service discovery consumes most of that window. Pre-writing the cache
# tells BlueZ the service layout so it skips BLE discovery entirely.
# Only written if sensor_mac is set in config — if empty, script discovers by name.

if [ -n "$SENSOR_MAC" ]; then
    SENSOR_MAC_NODASH="${SENSOR_MAC//:/_}"
    bashio::log.info "Pre-caching BlueZ services for ${SENSOR_MAC}"

    for ADAPTER_DIR in /var/lib/bluetooth/*/; do
        if [ -d "$ADAPTER_DIR" ]; then
            CACHE_DIR="${ADAPTER_DIR}cache"
            mkdir -p "$CACHE_DIR"
            CACHE_FILE="${CACHE_DIR}/${SENSOR_MAC}"

            cat > "$CACHE_FILE" << CACHE
[General]
Name=MALEM ALARM

[ServiceChanged]
Value=

[/org/bluez/hci0/dev_${SENSOR_MAC_NODASH}/service0001]
UUID=00001800-0000-1000-8000-00805f9b34fb
Primary=true
Device=/org/bluez/hci0/dev_${SENSOR_MAC_NODASH}

[/org/bluez/hci0/dev_${SENSOR_MAC_NODASH}/service0014]
UUID=0000fff0-0000-1000-8000-00805f9b34fb
Primary=true
Device=/org/bluez/hci0/dev_${SENSOR_MAC_NODASH}

[/org/bluez/hci0/dev_${SENSOR_MAC_NODASH}/service0014/char0015]
UUID=0000fff1-0000-1000-8000-00805f9b34fb
Value=
Flags=write

[/org/bluez/hci0/dev_${SENSOR_MAC_NODASH}/service0014/char0017]
UUID=0000fff2-0000-1000-8000-00805f9b34fb
Value=
Flags=read

[/org/bluez/hci0/dev_${SENSOR_MAC_NODASH}/service0014/char0019]
UUID=0000fff4-0000-1000-8000-00805f9b34fb
Value=
Flags=read,notify

[/org/bluez/hci0/dev_${SENSOR_MAC_NODASH}/service0014/char0019/desc001b]
UUID=00002902-0000-1000-8000-00805f9b34fb
Value=0000
CACHE
            bashio::log.info "Cache written: ${CACHE_FILE}"
        fi
    done
else
    bashio::log.info "No sensor_mac set — skipping cache (will discover by name)"
fi

exec python3 /app/malem_alarm.py
