# Contributing

Thanks for helping! Issues and pull requests are welcome.

1. Fork and clone, then `npm ci --prefix ui --no-audit --no-fund` once.
2. Run `sh scripts/check.sh` — it must pass (it never touches a phone).
3. For behaviour that needs a device, describe what you tested: iPhone model, iOS version, Xcode version, USB or Wi-Fi.
4. Keep pool commands' single-JSON-object output and exit codes stable; add tests in `tests/test_pool.py`.
5. Never commit UDIDs, Team IDs, profiles, personal screenshots or telemetry tokens.

Changes to the MCP server itself may also be worth proposing upstream at https://github.com/zhongerxin/iPhone-use.
