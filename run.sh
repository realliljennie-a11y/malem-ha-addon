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
# Pre-populating the cache tells BlueZ the service layout so it skips BLE
# discovery on connect — critical because the Malem sensor drops the connection
# if auth doesn't complete within ~3 seconds.
#
# MAC source priority: config override → state file → skip (first run)

write_cache() {
    local MAC="$1"
    local MAC_NODASH="${MAC//:/_}"

    for ADAPTER_DIR in /var/lib/bluetooth/*/; do
        [ -d "$ADAPTER_DIR" ] || continue
        CACHE_DIR="${ADAPTER_DIR}cache"
        mkdir -p "$CACHE_DIR"
        CACHE_FILE="${CACHE_DIR}/${MAC}"

        bashio::log.info "Writing BlueZ cache for ${MAC}: ${CACHE_FILE}"

        cat > "$CACHE_FILE" << CACHE
[General]
Name=MALEM ALARM

[ServiceChanged]
Value=

[/org/bluez/hci0/dev_${MAC_NODASH}/service0001]
UUID=00001800-0000-1000-8000-00805f9b34fb
Primary=true
Device=/org/bluez/hci0/dev_${MAC_NODASH}

[/org/bluez/hci0/dev_${MAC_NODASH}/service0014]
UUID=0000fff0-0000-1000-8000-00805f9b34fb
Primary=true
Device=/org/bluez/hci0/dev_${MAC_NODASH}

[/org/bluez/hci0/dev_${MAC_NODASH}/service0014/char0015]
UUID=0000fff1-0000-1000-8000-00805f9b34fb
Value=
Flags=write

[/org/bluez/hci0/dev_${MAC_NODASH}/service0014/char0017]
UUID=0000fff2-0000-1000-8000-00805f9b34fb
Value=
Flags=read

[/org/bluez/hci0/dev_${MAC_NODASH}/service0014/char0019]
UUID=0000fff4-0000-1000-8000-00805f9b34fb
Value=
Flags=read,notify

[/org/bluez/hci0/dev_${MAC_NODASH}/service0014/char0019/desc001b]
UUID=00002902-0000-1000-8000-00805f9b34fb
Value=0000
CACHE
    done
}

# Determine MAC to cache
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
    bashio::log.info "No MAC known yet — first run, will scan and restart"
fi

# ── Run the Python script ─────────────────────────────────────────────────────
# Exit code 2 means the script discovered the MAC on a first run and saved it.
# We write the cache and restart immediately in that case.

while true; do
    python3 /app/malem_alarm.py
    EXIT_CODE=$?

    if [ $EXIT_CODE -eq 2 ]; then
        bashio::log.info "MAC discovered — writing cache and restarting..."
        CACHE_MAC=$(python3 -c "import json; d=json.load(open('/config/malem_state.json')); print(d.get('sensor_mac',''))" 2>/dev/null)
        if [ -n "$CACHE_MAC" ]; then
            write_cache "$CACHE_MAC"
        fi
        bashio::log.info "Restarting..."
        sleep 1
        # Loop continues, restarting the Python script with cache now in place
    else
        # Any other exit code — let s6 handle the restart
        exit $EXIT_CODE
    fi
done
