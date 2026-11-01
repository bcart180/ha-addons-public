# syntax=docker/dockerfile:1
# BUILD_FROM is passed by HA Supervisor from build.yaml as a PLAIN TAG
# (ghcr.io/home-assistant/{arch}-base-debian:bookworm-2026.10.3). Never put @sha256 in build_from:
# Supervisor rejects it and falls back to Alpine base:latest. Default = aarch64 tag.
ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base-debian:bookworm-2026.10.3
# Base images pinned by multi-arch index digest (tag kept for readability; L2).
# golang:1.26.7-bookworm
ARG GO_IMAGE=golang:1.26.7-bookworm@sha256:e8c859f5632dcfde7b32d2012b4351728f6437930887c2f6a91ea242459e5514

# ---------- builder: Proton's official source, pinned tag + commit ----------
FROM ${GO_IMAGE} AS builder
ARG BRIDGE_TAG=v3.27.1
ARG BRIDGE_SHA=43b9237786204a0215abfd4dcc51ee7bb92f52d4
RUN apt-get update \
 && apt-get install -y --no-install-recommends git make gcc libc6-dev pkg-config \
      libsecret-1-dev libfido2-dev libcbor-dev libssl-dev ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git clone --depth 1 --branch "${BRIDGE_TAG}" https://github.com/ProtonMail/proton-bridge.git . \
 && test "$(git rev-parse HEAD)" = "${BRIDGE_SHA}" \
 || { echo "FATAL: ${BRIDGE_TAG} is not ${BRIDGE_SHA} (tag moved?)"; exit 1; }
# GOTOOLCHAIN=local: never download a different Go toolchain at build time.
ENV GOTOOLCHAIN=local CGO_ENABLED=1
RUN make build-nogui BUILD_ENV=prod \
 && ./bridge --version

# ---------- starttls-guard: refuses LOGIN/AUTHENTICATE before STARTTLS ----------
# Stdlib only (no downloads). The unit tests run here, so every image build (CI included)
# proves: pre-TLS AUTH= stripped + LOGINDISABLED, LOGIN/AUTHENTICATE answered NO and never
# forwarded, STARTTLS injection rejected, post-TLS traffic untouched.
FROM ${GO_IMAGE} AS guard
# No -race here: Go's arm64 race detector (TSAN) needs a 48-bit-VA kernel and aborts on
# 39-bit-VA kernels such as Raspberry Pi / HAOS ("unsupported VMA range", golang/go#29948),
# which broke the Supervisor build. -race runs in CI (guard-race job, native arm64) instead.
# The shipped binary is static (CGO_ENABLED=0).
ENV GOTOOLCHAIN=local GOFLAGS=-mod=mod
WORKDIR /guard
COPY starttls-guard/ ./
RUN go vet ./... && CGO_ENABLED=0 go test -v -count=1 ./... \
 && CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' -o /starttls-guard .

# ---------- ttyd (login-mode terminal only): official static release binary ----------
# Upstream's static build (musl, libwebsockets + libuv linked in, no evlib plugins), pinned
# by sha256 from the release's SHA256SUMS. Replaces the Debian-lws build, which failed at
# runtime: "lws_create_context: unable to load evlib plugin evlib_uv" (Pi re-test #3).
# BUILD_ARCH is passed by HA Supervisor (aarch64|amd64); CI uses the aarch64 default.
FROM ${GO_IMAGE} AS ttyd
ARG BUILD_ARCH=aarch64
ARG TTYD_VERSION=1.7.7
ARG TTYD_SHA256_AARCH64=b38acadd89d1d396a0f5649aa52c539edbad07f4bc7348b27b4f4b7219dd4165
ARG TTYD_SHA256_X86_64=8a217c968aba172e0dbf3f34447218dc015bc4d5e59bf51db2f2cd12b7be4f55
RUN set -eu; \
    case "${BUILD_ARCH}" in \
      aarch64) f=ttyd.aarch64; sum="${TTYD_SHA256_AARCH64}" ;; \
      amd64)   f=ttyd.x86_64;  sum="${TTYD_SHA256_X86_64}" ;; \
      *) echo "FATAL: unsupported BUILD_ARCH ${BUILD_ARCH}"; exit 1 ;; \
    esac; \
    curl -fsSL -o /ttyd "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/${f}"; \
    echo "${sum}  /ttyd" | sha256sum -c -; \
    chmod 0755 /ttyd; /ttyd --version

# ---------- runtime: HA Debian base ----------
FROM ${BUILD_FROM}
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      pass gnupg libsecret-1-0 libfido2-1 libcbor0.8 ca-certificates socat procps \
 && rm -rf /var/lib/apt/lists/*
# Only `bridge` (the app). `proton-bridge` is the launcher, which applies downloaded
# updates; it is deliberately NOT shipped so the image-pinned version always runs.
COPY --from=builder /src/bridge /usr/lib/protonmail/bridge/bridge
COPY --from=guard /starttls-guard /usr/local/bin/starttls-guard
COPY --from=ttyd /ttyd /usr/local/bin/ttyd
COPY rootfs/ /
RUN chmod +x /run.sh /usr/local/bin/imap-gate /usr/local/bin/imap-allowlist /usr/local/bin/imap-greeting-ok /usr/local/bin/starttls-guard
CMD ["/run.sh"]
