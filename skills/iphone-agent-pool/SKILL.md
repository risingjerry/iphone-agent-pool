---
name: iphone-agent-pool
description: Set up and run a wireless pool of real iPhones that AI agents control over MCP (no USB cable, auto-unlock, auto re-signing, Tailscale remote access). Use when the user wants Claude Code / Codex to control their iPhone, add a phone to the pool, make a phone reachable from another computer, fix "iPhone not ready / phone service down / signature expired", or says 让 Agent 操控 iPhone、把手机加进设备池、手机无线连接、远程控制手机.
---

# iPhone Agent Pool

Repo: https://github.com/risingjerry/iphone-agent-pool (macOS host + full Xcode; phones on iOS 17+).
All commands: `python3 pool/iphone_pool.py <command>` from the repo root. Each prints one JSON object (`ok`, then fields or `error`); exit 0 ok, 1 failed, 2 usage.

## First contact

1. `status` — phones already in the pool, `ready`, `mcp_url`, `signature_expires`. If a phone is ready, connect to its `mcp_url` and use the `iphone-use` skill for the actual task.
2. Nothing configured → `devices` to find the phone (`supported: true` needs iOS 17+). If the phone is missing, the user must plug it in once, unlock it and tap Trust.

## Mental model

- Each phone: own state dir, the phone service (PUA, WebDriverAgent) on loopback port 181xx, MCP on 182xx (`http://127.0.0.1:182xx/mcp`).
- PUA is reached through the CoreDevice tunnel (USB or same Wi-Fi as the Mac). A VPN on the phone does not matter; the phone does not need Tailscale.
- launchd watchdog (`tick`, every 2 min) restarts PUA and re-signs 2 days before a free 7-day profile expires.
- Locked passcode-less phones are woken and swiped open automatically. Phones with a passcode cannot be unlocked by automation.

## Add a phone

1. User does on the phone (never do these for them — they are security settings): Developer Mode on; for a dedicated phone turn off Stolen Device Protection, Reset Face ID, Turn Passcode Off (that order).
2. Ask for their Apple Team ID (Xcode → Settings → Accounts) and a unique bundle ID they own, e.g. `com.<their-name>.pua`. Never invent or reuse someone else's.
3. `add <name> --udid <UDID> --team-id <TEAMID> --bundle-id <ID>` — fetch + build + sign, takes a few minutes.
4. First time per phone: the user trusts the developer certificate (Settings → General → VPN & Device Management). `start <name>` fails with "Developer App Certificate is not trusted" until then.
5. `start <name>`, then poll `status` until `ready: true` (10–30 s).
6. `install` (add `--expose` only if the user wants other computers on their tailnet to reach it). Report the `local` / `tailnet` URLs.
7. Connect the agent: `claude mcp add --transport http <name> <mcp_url>`; a new session loads the tools.

## Personal phone (on-demand)

For the user's own phone with a passcode: `add <name> ... --on-demand`. Never ask them to remove the passcode. It is never auto-started or auto-unlocked, and stays off the tailnet unless the user explicitly asks: `expose <name> on` (warn that every tailnet device can then drive it while unlocked). Workflow: user unlocks the phone → `start <name>` (re-signs first if due; it waits while the phone is locked, never launches locked) → task. Leave it running afterwards: the runner survives lock/unlock, and stopping/restarting is what risks failure. `stop <name>` only when the user asks. After an iPhone reboot the first start must be over USB; a code 74 / "Exiting due to IDE disconnection" start failure means: USB start, else the user reboots the phone.

## Errors → next step

| Symptom | Do |
|---|---|
| `Unknown phone` | `status` for names; `add` it first |
| `ready: false`, watchdog installed | wait one tick (2 min); phone must be on the Mac's Wi-Fi. Then `start <name>` once |
| start log: certificate not trusted | user trusts the certificate on the phone (iOS may reset it after passcode/Face ID changes), then `start` |
| `signature_expires` past / launch rejected | `resign <name>` (phone on the Mac's Wi-Fi) |
| start log: "Timed out while enabling automation mode" | passcode phone: user turns on Settings → Developer → Enable UI Automation and confirms on the phone; `stop`, then `start` |
| `phone_locked` from MCP tools | phone has a passcode: user unlocks it, or removes the passcode for a dedicated pool phone |
| `devices` shows `supported: false` | iOS ≤ 16 (iPhone X/8): not supported by current Xcode; say so |
| Personal Team "maximum number of apps" | user removes old dev apps from the phone themselves |

## Notifications

`install` copies `IPHONE_POOL_NOTIFY` (a command; the message is appended as the last argument) into the watchdog, which calls it when a phone needs the user (certificate trust, UI Automation). Set it if the user has a notification channel.

## Speed and power

Fewer steps beat faster steps: batch known actions with `pua_batch`; pass `"observe": "tree"` to actions instead of a separate observe; prefer tree over screenshots; call `pua_ready` once per task; do not lock after every task: the watchdog locks pool phones after 3 idle minutes; `pua_press_button {"name": "lock"}` locks on demand (needs a Shortcuts shortcut named `Lock Screen`; pool phones auto-unlock on the next action; each unlock is logged to `unlock.log` in the phone's state dir).

## Shell-only agents

No MCP support (or only public-URL connectors, like Grok Bot)? Download `pool/phone_client.py` and run `python3 phone_client.py --url http://<mac-tailscale-ip>:<port>/mcp call <tool> '<json>'`; screenshots are saved to `--out`. Read the `iphone-use` skill for how to use the tools.

## Safety

- `install`/`uninstall` change launchd and Tailscale serve config; `--expose` makes the phone controllable by every device on the tailnet (no auth). Ask before exposing.
- Messages, purchases, account and settings changes on the phone only on explicit user request; passwords, Face ID and payment are always the user's.
- Never paste UDIDs, Team IDs or screenshots of personal screens into public places.

Driving apps: see the `iphone-use` skill. Full reference: README.md.
