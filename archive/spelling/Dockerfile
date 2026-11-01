ARG BUILD_FROM=ghcr.io/home-assistant/base:3.23
FROM ${BUILD_FROM}

RUN apk add --no-cache python3

# Only the server script goes into the image. The spelling files are NOT copied;
# they are read at request time from /share/spelling (mapped read-only).
COPY server.py /server.py

CMD ["python3", "-u", "/server.py"]

LABEL \
    io.hass.type="addon" \
    org.opencontainers.image.title="Home Assistant Add-on: Spelling" \
    org.opencontainers.image.source="https://github.com/bcart180/ha-addons-public"
