ARG BUILD_FROM=ghcr.io/home-assistant/base:3.23
FROM ${BUILD_FROM}

RUN apk add --no-cache python3 tzdata

COPY rootfs/ /
# Fails the image build if the app could call X, or the labeled starter snapshot is invalid.
RUN python3 /app/selfcheck.py

WORKDIR /app
CMD ["python3", "-u", "/app/server.py"]

LABEL \
    io.hass.type="addon" \
    org.opencontainers.image.title="Home Assistant Add-on: Sports News" \
    org.opencontainers.image.description="Read-only personal sports snapshot" \
    org.opencontainers.image.source="https://github.com/bcart180/ha-addons-public"
