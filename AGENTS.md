# Agent guide — iphone-agent-pool

A wireless pool of real iPhones for AI agents. `server/` is the iPhone Use MCP server (upstream, MIT); `pool/` adds the multi-phone pool: wireless transport through the CoreDevice tunnel, auto-unlock, a launchd watchdog that restarts and re-signs WebDriverAgent, and one Streamable HTTP MCP endpoint per phone.

## Layout

- `pool/iphone_pool.py` — pool CLI (`devices`, `add`, `start`, `stop`, `install [--expose]`, `uninstall`, `status`, `tick`, `resign`). One JSON object on stdout, logs on stderr, exit 0/1/2.
- `pool/serve.sh NAME PORT` — supergateway wrapper that serves one phone's MCP on `127.0.0.1:PORT/mcp`.
- `server/` — MCP server (`iphone_use.py`), WDA client/controller, setup jobs (`wda_setup.py`).
- `tooling/forward.mjs`, `tooling/screen-stream.mjs` — WDA and MJPEG forwarders; `device_host` `"tunnel"` resolves the CoreDevice tunnel address per connection.
- `skills/` — `iphone-agent-pool` (pool setup), `iphone-use` (driving apps), `iphone-use-setup` (single-phone setup).
- `docs/` — upstream single-phone guide, demo and social-preview sources. Not shipped in plugin packages.

## Test

```sh
npm ci --prefix ui --no-audit --no-fund   # once, for the widget test
sh scripts/check.sh                        # unit tests + package validation + syntax checks
python3 -m unittest tests.test_pool        # pool only
```

Tests never touch a phone. Live checks need a real iOS 17+ iPhone paired with Xcode on macOS.

## Rules

- Never add telemetry tokens or default-on analytics. `server/posthog.json` and `server/ga4.json` must not exist.
- Keep `WDA_URL` loopback-only; remote reach goes through `device_host` (forwarder) or `tailscale serve`, never by binding services to `0.0.0.0`.
- Do not commit device UDIDs, Team IDs, bundle IDs, provisioning profiles, screenshots of personal screens, or anything under `~/.local/share/iphone-use`.
- Phone actions (sending messages, purchases, settings changes) are outward-facing: do them only on the user's explicit request; passwords, Face ID and payments stay with the human.
- `add` builds and signs with the user's own Apple account; never reuse someone else's Team ID or bundle ID.
- Every pool command must keep the single-JSON-object output contract.

For using the phones (not developing this repo), read `skills/iphone-agent-pool/SKILL.md`.
