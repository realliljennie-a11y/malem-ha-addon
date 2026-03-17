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

SENSOR_MAC_RESOLVED=""
if [ -n "$SENSOR_MAC" ]; then
    SENSOR_MAC_RESOLVED="$SENSOR_MAC"
elif [ -f "/config/malem_state.json" ]; then
    SENSOR_MAC_RESOLVED=$(python3 -c "import json; d=json.load(open('/config/malem_state.json')); print(d.get('sensor_mac',''))" 2>/dev/null)
fi

if [ -n "$SENSOR_MAC_RESOLVED" ]; then
    bashio::log.info "Connecting to ${SENSOR_MAC_RESOLVED}..."
    CONNECTED=0
    while [ $CONNECTED -eq 0 ]; do
        # Scan first to register device with BlueZ, then connect
        bluetoothctl --timeout 5 scan on 2>/dev/null || true
        if bluetoothctl connect "$SENSOR_MAC_RESOLVED" 2>&1 | grep -q "Connection successful"; then
            CONNECTED=1
            bashio::log.info "Connected — handing off to Python"
        else
            bashio::log.info "Device not ready — retrying in 5s"
            sleep 5
        fi
    done
else
    bashio::log.info "No MAC known yet — Python will handle first-run discovery"
fi

exec python3 /app/malem_bluet.py
