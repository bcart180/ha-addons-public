#!/usr/bin/env bash
# Box/CI test: proves LOGIN/AUTHENTICATE are refused before STARTTLS and never reach Bridge.
# Same tests run inside the Docker build (guard stage). Needs Go >= 1.26.
set -euo pipefail
cd "$(dirname "$0")"
go vet ./...
go test -race -v -count=1 ./...
