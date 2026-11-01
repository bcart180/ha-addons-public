ARG BUILD_FROM=ghcr.io/home-assistant/base:3.23
FROM ${BUILD_FROM}

RUN apk add --no-cache python3 py3-aiohttp tzdata

# Copy the whole build context, then install rootfs/ from it. If rootfs/ did not
# reach the builder, print what did arrive so the Supervisor log shows the cause.
COPY . /tmp/addon_src/
RUN set -e; \
    if [ ! -f /tmp/addon_src/rootfs/app/server.py ]; then \
      echo "TWILIO HOUSE VOICE BUILD: rootfs/app/server.py missing from build context. Context received:"; \
      find /tmp/addon_src -maxdepth 4 -exec ls -ld {} \; ; \
      exit 1; \
    fi; \
    cp -a /tmp/addon_src/rootfs/. /; \
    rm -rf /tmp/addon_src

WORKDIR /app
CMD ["python3", "-u", "/app/server.py"]

LABEL \
    io.hass.type="addon" \
    org.opencontainers.image.title="Home Assistant Add-on: CallCatch Voice" \
    org.opencontainers.image.description="Twilio Media Streams bridge to xAI Realtime with call audio for CallCatch" \
    org.opencontainers.image.source="https://github.com/bcart180/ha-addons-public" \
    org.opencontainers.image.version="0.2.5"
