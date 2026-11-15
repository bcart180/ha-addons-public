ARG BUILD_FROM=ghcr.io/home-assistant/base:3.23
FROM ${BUILD_FROM}

# aiohttp floor: 3.13.5 carries the 3.13.4 security fixes.  Tests also run on 3.13.5.
RUN apk add --no-cache python3 "py3-aiohttp>=3.13.5" tzdata

COPY rootfs/ /
RUN chmod a+x /etc/services.d/callcatch_line/run /etc/services.d/callcatch_line/finish

WORKDIR /app
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8791/healthz', timeout=4).read()"

LABEL \
    io.hass.type="addon" \
    org.opencontainers.image.title="Home Assistant Add-on: CallCatch Line" \
    org.opencontainers.image.description="MCP server for the CallCatch phone agent" \
    org.opencontainers.image.source="https://github.com/bcart180/ha-addons-public"
