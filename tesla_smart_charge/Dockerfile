ARG BUILD_FROM=ghcr.io/home-assistant/base:3.23
FROM ${BUILD_FROM}
RUN apk add --no-cache python3 tzdata
COPY rootfs/ /
RUN chmod a+x /etc/services.d/tesla_smart_charge/run /etc/services.d/tesla_smart_charge/finish
WORKDIR /app
# No CMD: S6 runs /etc/services.d/tesla_smart_charge/run under with-contenv (SUPERVISOR_TOKEN visible).
LABEL io.hass.type="addon" \
    org.opencontainers.image.title="Home Assistant Add-on: Tesla Smart Charge" \
    org.opencontainers.image.source="https://github.com/bcart180/ha-addons-public"
