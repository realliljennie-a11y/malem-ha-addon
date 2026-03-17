ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache \
    python3 \
    py3-pip \
    bluez \
    bluez-dev \
    bluez-tools \
    dbus \
    gcc \
    musl-dev \
    python3-dev

RUN pip3 install --no-cache-dir --break-system-packages \
    bleak==0.21.1 \
    paho-mqtt

WORKDIR /app
COPY run.sh /
COPY malem_bluet.py /app/

RUN chmod +x /run.sh

CMD ["/run.sh"]
