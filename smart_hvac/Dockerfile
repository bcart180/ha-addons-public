ARG BUILD_FROM=ghcr.io/home-assistant/base:3.23
FROM ${BUILD_FROM}
RUN apk add --no-cache python3 py3-aiohttp tzdata
COPY rootfs/ /
RUN chmod a+x /etc/services.d/smart_hvac/run /etc/services.d/smart_hvac/finish
WORKDIR /app
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8099/api/health', timeout=5).read()"
# No CMD: S6 runs /etc/services.d/smart_hvac/run under with-contenv (SUPERVISOR_TOKEN visible).
LABEL io.hass.type="addon" \
    org.opencontainers.image.title="Home Assistant Add-on: Smart HVAC" \
    org.opencontainers.image.source="https://github.com/bcart180/ha-addons-public"
