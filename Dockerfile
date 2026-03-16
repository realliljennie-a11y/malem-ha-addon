ARG BUILD_FROM
FROM $BUILD_FROM

# Install Python and build deps for bleak
RUN apk add --no-cache \
    python3 \
    py3-pip \
    bluez \
    bluez-dev \
    dbus \
    && pip3 install --no-cache-dir \
    bleak \
    paho-mqtt

# Copy add-on files
WORKDIR /app
COPY run.sh /
COPY malem_alarm.py /app/

RUN chmod +x /run.sh

CMD ["/run.sh"]
