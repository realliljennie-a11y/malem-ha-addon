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

# ── Pre-connect via bluetoothctl ──────────────────────────────────────────────
# bluetoothctl completes service discovery in ~1s. We connect and leave the
# connection open so Python can auth immediately without triggering discovery.
# Retry until the device is available — it may be off or charging.

SENSOR_MAC_RESOLVED=""
if [ -n "$SENSOR_MAC" ]; then
    SENSOR_MAC_RESOLVED="$SENSOR_MAC"
elif [ -f "/config/malem_state.json" ]; then
    SENSOR_MAC_RESOLVED=$(python3 -c "import json; d=json.load(open('/config/malem_state.json')); print(d.get('sensor_mac',''))" 2>/dev/null)
fi

if [ -n "$SENSOR_MAC_RESOLVED" ]; then
    bashio::log.info "Waiting for device ${SENSOR_MAC_RESOLVED}..."
    while true; do
        OUTPUT=$(bluetoothctl connect "$SENSOR_MAC_RESOLVED" 2>&1)
        if echo "$OUTPUT" | grep -q "Connection successful"; then
            bashio::log.info "Connected — handing off to Python"
            break
        fi
        bashio::log.info "Device not ready (${OUTPUT##*$'\n'}) — retrying in 5s..."
        sleep 5
    done
else
    bashio::log.info "No MAC known yet — Python will handle first-run discovery"
fi

exec python3 /app/malem_bluet.py
