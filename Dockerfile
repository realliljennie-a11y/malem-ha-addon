ARG BUILD_FROM
FROM $BUILD_FROM

# Install system dependencies for bleak (needs BlueZ/D-Bus headers)
RUN apk add --no-cache \
    python3 \
    py3-pip \
    bluez \
    bluez-dev \
    dbus \
    gcc \
    musl-dev \
    python3-dev

# PEP 668: base image Python is externally managed, need --break-system-packages
RUN pip3 install --no-cache-dir --break-system-packages \
    bleak \
    paho-mqtt

WORKDIR /app
COPY run.sh /
COPY malem_alarm.py /app/

RUN chmod +x /run.sh

CMD ["/run.sh"]
