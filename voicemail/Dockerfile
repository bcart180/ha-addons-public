# Per-arch bases live in build.yaml. The default is the multi-arch image
# so a local `docker build` still works without --build-arg.
ARG BUILD_FROM=ghcr.io/home-assistant/base:3.23
FROM ${BUILD_FROM}

RUN apk add --no-cache python3 py3-aiohttp tzdata openssl

COPY . /tmp/addon_src/
RUN set -e; \
    if [ ! -f /tmp/addon_src/rootfs/app/server.py ]; then \
      echo "VOICEMAIL BUILD: rootfs/app/server.py missing from build context. Context received:"; \
      find /tmp/addon_src -maxdepth 4 -exec ls -ld {} \; ; \
      exit 1; \
    fi; \
    cp -a /tmp/addon_src/rootfs/. /; \
    rm -rf /tmp/addon_src; \
    chmod a+x /etc/services.d/voicemail/run /etc/services.d/voicemail/finish

WORKDIR /app
# Same probe the old config watchdog URL used. S6 still starts the service.
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8099/api/health', timeout=5).read()"
# No CMD: S6 starts /etc/services.d/voicemail/run via with-contenv so
# SUPERVISOR_TOKEN is visible. A bare CMD would run with an empty env.

LABEL \
    io.hass.type="addon" \
    org.opencontainers.image.title="Home Assistant Add-on: CallCatch" \
    org.opencontainers.image.description="Your Personalized Voicemail" \
    org.opencontainers.image.source="https://github.com/bcart180/ha-addons-public"
