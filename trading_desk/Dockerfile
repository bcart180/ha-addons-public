ARG BUILD_FROM=ghcr.io/home-assistant/base:3.23
FROM ${BUILD_FROM}

RUN apk add --no-cache python3 py3-aiohttp tzdata

# Copy the whole build context, then install rootfs/ from it. If rootfs/ did not
# reach the builder, print what did arrive so the Supervisor log shows the cause.
COPY . /tmp/addon_src/
RUN set -e; \
    if [ ! -f /tmp/addon_src/rootfs/app/server.py ]; then \
      echo "TRADING DESK BUILD: rootfs/app/server.py missing from build context. Context received:"; \
      find /tmp/addon_src -maxdepth 4 -exec ls -ld {} \; ; \
      exit 1; \
    fi; \
    cp -a /tmp/addon_src/rootfs/. /; \
    rm -rf /tmp/addon_src

# Fails the image build if any order-write code (POST/PATCH/PUT/DELETE, order
# submit/replace/cancel, position close) appears anywhere in the app source.
RUN python3 /app/selfcheck.py

WORKDIR /app
CMD ["python3", "-u", "/app/server.py"]

LABEL \
    io.hass.type="addon" \
    org.opencontainers.image.title="Home Assistant Add-on: Trading Desk" \
    org.opencontainers.image.description="Read-only Alpaca desk dashboard" \
    org.opencontainers.image.source="https://github.com/bcart180/ha-addons-public"
