ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip openssl \
    && pip3 install --no-cache-dir --break-system-packages paho-mqtt==2.1.0 \
    && apt-get purge -y python3-pip && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY hisense_local /app/hisense_local
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
