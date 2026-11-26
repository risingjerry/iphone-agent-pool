#!/bin/sh
# Expose one phone's MCP as Streamable HTTP on loopback; `iphone_pool.py
# install --expose` publishes the port on your tailnet only. Usage: serve.sh NAME PORT
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export IPHONE_USE_STATE_DIR="${IPHONE_POOL_HOME:-$HOME/.local/share/iphone-use}/devices/$1"
# Pool phones have no passcode: wake and swipe up before acting. On-demand
# (personal) phones keep their passcode, so they are never auto-unlocked.
grep -q '"on_demand": true' "$IPHONE_USE_STATE_DIR/pool.json" 2>/dev/null || export IPHONE_USE_AUTO_UNLOCK=1
# W3C touch taps are ~40% faster than /wda/tap on iOS 26.
export IPHONE_USE_FAST_TAP=1
# Remote agents usually sit behind a rate-limited relay: ~55 KB screenshots instead of ~120 KB.
export IPHONE_USE_IMAGE_SHORT_SIDE=512 IPHONE_USE_IMAGE_QUALITY=65
exec npx -y supergateway@4.2.0 --stdio "python3 $root/server/iphone_use.py" \
  --outputTransport streamableHttp --stateful --host 127.0.0.1 --port "$2" \
  --healthEndpoint /healthz --logLevel none
