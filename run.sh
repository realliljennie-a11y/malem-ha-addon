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
# bluetoothctl completes service discovery in ~1s (vs 8s+ for bleak).
# By connecting here first, BlueZ caches the services in memory.
# The Python script then connects to an already-known device and auth
# succeeds immediately.
#
# We do this for the known MAC if available, otherwise Python handles
# first-run discovery.

SENSOR_MAC_RESOLVED=""
if [ -n "$SENSOR_MAC" ]; then
    SENSOR_MAC_RESOLVED="$SENSOR_MAC"
elif [ -f "/config/malem_state.json" ]; then
    SENSOR_MAC_RESOLVED=$(python3 -c "import json; d=json.load(open('/config/malem_state.json')); print(d.get('sensor_mac',''))" 2>/dev/null)
fi

if [ -n "$SENSOR_MAC_RESOLVED" ]; then
    bashio::log.info "Pre-connecting via bluetoothctl to warm BlueZ cache..."
    # Connect and immediately disconnect — we just need service discovery to run
    bluetoothctl connect "$SENSOR_MAC_RESOLVED" 2>/dev/null || true
    sleep 0.5
    bluetoothctl disconnect "$SENSOR_MAC_RESOLVED" 2>/dev/null || true
    sleep 0.5
    bashio::log.info "BlueZ cache warmed"
else
    bashio::log.info "No MAC known yet — skipping pre-connect"
fi

exec python3 /app/malem_bluet.py
