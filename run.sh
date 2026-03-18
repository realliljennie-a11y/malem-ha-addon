#!/usr/bin/with-contenv bashio

export MQTT_HOST=$(bashio::config 'mqtt_host')
export MQTT_PORT=$(bashio::config 'mqtt_port')
export MQTT_USERNAME=$(bashio::config 'mqtt_username')
export MQTT_PASSWORD=$(bashio::config 'mqtt_password')
export MQTT_TOPIC_PREFIX=$(bashio::config 'mqtt_topic_prefix')
export DRY_TIMEOUT=$(bashio::config 'dry_timeout')
export LOG_LEVEL=$(bashio::config 'log_level')
export SENSOR_MAC=$(bashio::config 'sensor_mac')

bashio::log.info "Starting Malem BlueT listener..."
bashio::log.info "MQTT: ${MQTT_HOST}:${MQTT_PORT}, prefix: ${MQTT_TOPIC_PREFIX}"

# ── Write BlueZ GATT cache ────────────────────────────────────────────────────
# HA OS has no persistent /var/lib/bluetooth, so BlueZ loses its service cache
# on every restart and does full GATT discovery on every connect (~8s).
# Writing the correct cache file (handles confirmed from live D-Bus trace)
# makes connect() return in ~1s, matching Android's behavior.

SENSOR_MAC_RESOLVED=""
if [ -n "$SENSOR_MAC" ]; then
    SENSOR_MAC_RESOLVED="$SENSOR_MAC"
elif [ -f "/config/malem_state.json" ]; then
    SENSOR_MAC_RESOLVED=$(python3 -c "import json; d=json.load(open('/config/malem_state.json')); print(d.get('sensor_mac',''))" 2>/dev/null)
fi

if [ -n "$SENSOR_MAC_RESOLVED" ]; then
    MAC="$SENSOR_MAC_RESOLVED"
    MAC_NODASH="${MAC//:/_}"

    # Find the adapter directory
    for ADAPTER_DIR in /var/lib/bluetooth/*/; do
        [ -d "$ADAPTER_DIR" ] || continue
        CACHE_DIR="${ADAPTER_DIR}cache"
        mkdir -p "$CACHE_DIR"
        CACHE_FILE="${CACHE_DIR}/${MAC}"

        bashio::log.info "Writing BlueZ service cache for ${MAC}..."

        cat > "$CACHE_FILE" << CACHE
[ServiceChanged]
Value=

[/org/bluez/hci0/dev_${MAC_NODASH}/service0001]
UUID=00001800-0000-1000-8000-00805f9b34fb
Primary=true
Device=/org/bluez/hci0/dev_${MAC_NODASH}

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

[/org/bluez/hci0/dev_${MAC_NODASH}/service0028/char0036]
UUID=0000fff5-0000-1000-8000-00805f9b34fb
Value=
Flags=read

[/org/bluez/hci0/dev_${MAC_NODASH}/service0039]
UUID=0000ffe0-0000-1000-8000-00805f9b34fb
Primary=true
Device=/org/bluez/hci0/dev_${MAC_NODASH}

[/org/bluez/hci0/dev_${MAC_NODASH}/service0039/char003a]
UUID=0000ffe1-0000-1000-8000-00805f9b34fb
Value=
Flags=notify
CACHE
        bashio::log.info "Cache written to ${CACHE_FILE}"
    done
else
    bashio::log.info "No MAC known yet — skipping cache write"
fi

exec python3 /app/malem_bluet.py
