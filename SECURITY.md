# Security policy

## Supported versions

The latest `main` and the latest tagged release.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting (Security → Report a vulnerability) instead of a public issue. Expect a first reply within a week.

## Threat model in short

- Anyone who can reach an MCP endpoint can drive that phone. Endpoints listen on `127.0.0.1`; `install --expose` publishes them to your Tailscale tailnet only, without further authentication — keep the tailnet to your own devices.
- Pool phones are meant to run without a passcode; treat them as shared devices and keep personal accounts and payment cards off them.
- Signing uses your own Apple account through Xcode; no credentials are stored by this project. No telemetry is sent.
