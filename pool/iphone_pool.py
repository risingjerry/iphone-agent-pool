#!/usr/bin/env python3
"""iphone-agent-pool: a wireless pool of real iPhones for AI agents.

Each phone gets its own state dir ($IPHONE_POOL_HOME/devices/<name>), its own
WebDriverAgent loopback port and its own MCP port. WDA is reached through the
CoreDevice tunnel Xcode keeps to the phone (USB or same Wi-Fi), so no cable
and no VPN on the phone is needed. A launchd watchdog restarts WDA when it
drops and re-signs it before a free-team profile expires.

Every command prints one JSON object on stdout; exit code 0 = ok, 1 = failed,
2 = usage error.

  devices                         list iPhones Xcode can see
  add NAME --udid U --team-id T --bundle-id B   configure, fetch, build WDA
  start NAME / stop NAME          start or stop one phone's WDA
  install [--expose]              launchd services for every phone (+ Tailscale)
  expose NAME on|off              publish/withdraw one phone on your tailnet
  uninstall                       remove launchd services and Tailscale serves
  status                          one line per phone
  tick                            one watchdog pass (launchd runs this)
  resign NAME                     rebuild/re-sign now, then restart
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETUP = ROOT / "server/wda_setup.py"
HOME = Path(os.environ.get("IPHONE_POOL_HOME", Path.home() / ".local/share/iphone-use")).expanduser()
DEVICES = HOME / "devices"
LOGS = HOME / "pool-logs"
AGENTS = Path.home() / "Library/LaunchAgents"
LABEL = "dev.iphone-agent-pool"
WDA_BASE, MCP_BASE = 18100, 18200
# Personal Team profiles last 7 days; re-sign when fewer than this remain.
RESIGN_BEFORE = dt.timedelta(days=2)
# Xcode keeps handing back the cached profile until it is close to expiry; a re-sign that did not
# move the expiry is not repeated for this long, or the watchdog restarts WDA on every tick.
RESIGN_RETRY = dt.timedelta(hours=12)
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")


class PoolError(Exception):
    pass


def log(message):
    print(dt.datetime.now().strftime("%F %T"), message, file=sys.stderr, flush=True)


def run(argv, timeout=60):
    return subprocess.run(argv, text=True, capture_output=True, timeout=timeout)


def setup(state, *args, port=None):
    """wda_setup.py for one phone; its base URL must carry that phone's own WDA port."""
    if port is None:
        try:
            port = json.loads((state / "config.json").read_text())["local_port"]
        except (OSError, ValueError, KeyError):
            port = WDA_BASE
    result = run([sys.executable, str(SETUP), "--state-dir", str(state), "--base-url", f"http://127.0.0.1:{port}", *args], timeout=120)
    try:
        return json.loads(result.stdout)
    except ValueError:
        return {"ok": False, "error": (result.stderr or result.stdout)[-500:]}


def phones():
    for config in sorted(DEVICES.glob("*/config.json")):
        yield config.parent.name, config.parent, json.loads(config.read_text())


def phone(name):
    state = DEVICES / name
    if not (state / "config.json").is_file():
        raise PoolError(f"Unknown phone {name!r}; run add first.")
    return state, json.loads((state / "config.json").read_text())


def on_demand(state):
    """On-demand phones (e.g. a personal phone with a passcode) are never auto-started or exposed."""
    try:
        return bool(json.loads((state / "pool.json").read_text()).get("on_demand"))
    except (OSError, ValueError):
        return False


def options(state):
    try:
        return json.loads((state / "pool.json").read_text())
    except (OSError, ValueError):
        return {}


def exposed(state):
    """Pool phones are published by install --expose; on-demand phones only after `expose NAME on`."""
    return options(state).get("expose", not on_demand(state))


def signature_due(state):
    expiry = profile_expiry(state)
    now = dt.datetime.now(dt.timezone.utc)
    if expiry is None or expiry - now >= RESIGN_BEFORE:
        return False
    try:
        last = json.loads((state / "resigned.json").read_text())
        if last["expiry"] == expiry.isoformat() and now - dt.datetime.fromisoformat(last["at"]) < RESIGN_RETRY:
            return False
    except (OSError, ValueError, KeyError):
        pass
    return True


def mcp_port(config):
    return config["local_port"] - WDA_BASE + MCP_BASE


def ready(config):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{config['local_port']}/status", timeout=3) as response:
            return bool(json.load(response).get("value", {}).get("ready"))
    except Exception:
        return False


def profile_expiry(state):
    profiles = list((state / "derived_data/Build/Products").glob("*-iphoneos/WebDriverAgentRunner-Runner.app/embedded.mobileprovision"))
    if not profiles:
        return None
    decoded = subprocess.run(["security", "cms", "-D", "-i", str(profiles[0])], capture_output=True, timeout=20)
    expiry = plistlib.loads(decoded.stdout)["ExpirationDate"]
    return expiry.replace(tzinfo=dt.timezone.utc) if expiry.tzinfo is None else expiry


def device_details(udid):
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "d.json"
        run(["xcrun", "devicectl", "device", "info", "details", "--device", udid, "-q", "--json-output", str(output)], timeout=30)
        try:
            return json.loads(output.read_text()).get("result", {})
        except (OSError, ValueError):
            return {}


def reachable(udid):
    """The CoreDevice tunnel is up (cable or same Wi-Fi), so WDA can be launched and reached."""
    return bool(device_details(udid).get("connectionProperties", {}).get("tunnelIPAddress"))


def transport(udid):
    """'wired', 'localNetwork' or None; plugging in a cable is a new chance for a stuck phone."""
    return device_details(udid).get("connectionProperties", {}).get("transportType")


def active_job(state):
    jobs = setup(state, "status").get("jobs", [])
    return next((job for job in reversed(jobs) if job.get("state") in ("queued", "running")), None)


def wait_job(state, job_id, limit=1200):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        job = setup(state, "status", "--job-id", job_id).get("jobs", [{}])[0]
        if job.get("state") not in ("queued", "running"):
            return job.get("state"), job.get("log_tail", "")[-800:]
        time.sleep(5)
    return "timeout", ""


def build(state):
    setup(state, "stop")
    job = setup(state, "build")
    if not job.get("ok"):
        return "failed", job.get("error", "")
    outcome, tail = wait_job(state, job["job_id"])
    if outcome == "succeeded" and (expiry := profile_expiry(state)):
        (state / "resigned.json").write_text(json.dumps({"expiry": expiry.isoformat(), "at": dt.datetime.now(dt.timezone.utc).isoformat()}) + "\n")
    return outcome, tail


# Failures only the phone's owner can fix; the watchdog tells them instead of retrying silently.
NEEDS_HUMAN = (
    ("not trusted", "trust", "Trust the developer certificate on {name}: Settings > General > VPN & Device Management."),
    ("Timed out while enabling automation mode", "automation", "Enable UI Automation on {name}: Settings > Developer, then confirm on the phone."),
)
NOTIFY_EVERY = dt.timedelta(hours=6)
# Pool phones are locked after this many idle seconds (0 = never), so agents need not lock
# after every task and back-to-back tasks skip the ~3.5 s unlock.
IDLE_LOCK = int(os.environ.get("IPHONE_POOL_IDLE_LOCK", "180"))
# `hold NAME --minutes N` keeps the idle lock off until then (raw WDA/curl calls never touch
# last_activity, so long flows like posting a video would otherwise be locked mid-way).
# The expiry is the idle_hold file's mtime, so a forgotten hold lapses on its own.
HOLD_MAX_MINUTES = 240


def needs_human(state):
    """The latest WDA log's blocking reason that requires a person, or None."""
    logs = sorted((state / "logs").glob("*.log"), key=lambda path: path.stat().st_mtime)
    if not logs:
        return None
    tail = logs[-1].read_text(errors="replace")[-20000:]
    return next(((kind, message) for needle, kind, message in NEEDS_HUMAN if needle in tail), None)


def notify(state, name, kind, message):
    """macOS notification plus optional $IPHONE_POOL_NOTIFY command, at most once per kind per 6 h."""
    sent_file = state / "notified.json"
    try:
        sent = json.loads(sent_file.read_text())
    except (OSError, ValueError):
        sent = {}
    now = dt.datetime.now(dt.timezone.utc)
    last = sent.get(kind)
    if last and now - dt.datetime.fromisoformat(last) < NOTIFY_EVERY:
        return False
    text = message.format(name=name)
    run(["osascript", "-e", f"display notification {json.dumps(text)} with title \"iPhone Agent Pool\""])
    command = os.environ.get("IPHONE_POOL_NOTIFY")
    if command:
        run([*command.split(), text])
    sent[kind] = now.isoformat()
    sent_file.write_text(json.dumps(sent) + "\n")
    log(f"{name}: notified owner ({kind})")
    return True


def idle_lock(name, state, config):
    """Lock a ready pool phone nobody has used for IDLE_LOCK seconds; True if it was locked."""
    try:
        last = (state / "last_activity").stat().st_mtime
    except OSError:
        return False
    marker = state / "idle_locked"
    try:
        if (state / "idle_hold").stat().st_mtime > time.time():
            return False
    except OSError:
        pass
    if not IDLE_LOCK or time.time() - last < IDLE_LOCK or (marker.exists() and marker.stat().st_mtime >= last):
        return False
    sys.path.insert(0, str(ROOT / "pool"))
    from phone_client import Session
    session = Session(f"http://127.0.0.1:{mcp_port(config)}/mcp", 30)
    try:
        result = session.call("tools/call", {"name": "pua_press_button", "arguments": {"name": "lock"}})
    finally:
        session.close()
    marker.touch()  # newer than the activity this lock call itself recorded
    log(f"{name}: idle {IDLE_LOCK}s, locked (error={bool(result.get('isError'))})")
    return not result.get("isError")


# Circuit breaker: a phone that never becomes ready (Wi-Fi-only first launch, untrusted
# certificate, wedged automation mode) is not restarted forever. After BREAKER_AFTER starts
# in a row without a ready WDA, restarts pause for BREAKER_BASE, doubling up to BREAKER_MAX
# per further failed retry. A ready WDA, a manual `start NAME` or a change of connection
# (e.g. the owner plugged in USB) closes it again.
BREAKER_AFTER = int(os.environ.get("IPHONE_POOL_BREAKER_AFTER", "5"))
BREAKER_BASE = dt.timedelta(minutes=30)
BREAKER_MAX = dt.timedelta(hours=6)


def breaker(state):
    try:
        return json.loads((state / "breaker.json").read_text())
    except (OSError, ValueError):
        return {}


def breaker_open(state, udid):
    """True while restarts of this phone are paused."""
    record = breaker(state)
    if record.get("failures", 0) < BREAKER_AFTER or not record.get("until"):
        return False
    if dt.datetime.now(dt.timezone.utc) >= dt.datetime.fromisoformat(record["until"]):
        return False
    return transport(udid) == record.get("transport")


def breaker_count(name, state, udid):
    """Record one more start without a ready WDA; trip the breaker at BREAKER_AFTER."""
    record = breaker(state)
    record["failures"] = record.get("failures", 0) + 1
    if record["failures"] >= BREAKER_AFTER:
        record["trips"] = record.get("trips", 0) + 1
        pause = min(BREAKER_BASE * 2 ** min(record["trips"] - 1, 10), BREAKER_MAX)
        until = dt.datetime.now(dt.timezone.utc) + pause
        record.update(until=until.isoformat(), transport=transport(udid))
        local = until.astimezone().strftime("%F %T")
        log(f"{name}: {record['failures']} starts in a row never got WDA ready; pausing restarts until {local} "
            f"(transport={record['transport']}). Run `start {name}` or reconnect the phone to retry sooner.")
        hint = needs_human(state)
        advice = hint[1] if hint else ("Connect it by USB, unlocked, so the first launch is not over Wi-Fi."
                                       if record["transport"] != "wired" else "Unlock it and check Settings > Developer.")
        notify(state, name, "breaker", f"{{name}}: WDA failed to start {record['failures']} times; watchdog paused until {local}. {advice}")
    (state / "breaker.json").write_text(json.dumps(record) + "\n")


def breaker_reset(name, state, reason):
    record = breaker(state)
    (state / "breaker.json").unlink(missing_ok=True)
    if record.get("trips"):
        log(f"{name}: restart breaker closed ({reason})")


def decide(is_ready, due, job, is_reachable):
    """Watchdog policy for one phone: None, 'resign' or 'start'."""
    if is_ready and not due:
        return None
    if job and job.get("action") in ("build", "fetch"):
        return None
    if job and job.get("action") == "start" and not due:
        return None  # starting, or the tunnel is briefly down
    if not is_reachable:
        return None
    return "resign" if due else "start"


# ---- commands -------------------------------------------------------------

def cmd_devices(args):
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "l.json"
        run(["xcrun", "devicectl", "list", "devices", "-q", "--json-output", str(output)], timeout=60)
        listed = json.loads(output.read_text()).get("result", {}).get("devices", [])
    devices = []
    for item in listed:
        hardware, props = item.get("hardwareProperties", {}), item.get("deviceProperties", {})
        if hardware.get("platform") != "iOS" or hardware.get("reality") == "virtual":
            continue
        version = props.get("osVersionNumber", "")
        devices.append({"udid": hardware.get("udid"), "model": hardware.get("marketingName") or hardware.get("productType"),
                        "ios": version or None, "state": item.get("connectionProperties", {}).get("tunnelState"),
                        "pairing": item.get("connectionProperties", {}).get("pairingState"),
                        "supported": bool(version) and int(version.split(".")[0]) >= 17})
    return {"devices": devices, "note": "iOS 17+ is required (CoreDevice tunnel). Unsupported phones cannot be driven by current Xcode."}


def cmd_add(args):
    if not NAME.fullmatch(args.name):
        raise PoolError("NAME must be lowercase letters, digits or dashes, e.g. iphone12.")
    state = DEVICES / args.name
    taken = {config["local_port"] for name, _, config in phones() if name != args.name}
    port = next(p for p in range(WDA_BASE, WDA_BASE + 100) if p not in taken)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    shared = next((Path(config["source_dir"]) for name, _, config in phones() if name != args.name), None)
    configure = ["configure", "--udid", args.udid, "--team-id", args.team_id, "--bundle-id", args.bundle_id,
                 "--local-port", str(port), "--device-port", "8100", "--device-host", "tunnel"]
    if shared and (shared / "WebDriverAgent.xcodeproj").is_dir():
        configure += ["--source-dir", str(shared)]
    result = setup(state, *configure, port=port)
    if not result.get("ok"):
        raise PoolError(result.get("error", "configure failed"))
    if not (Path(result["config"]["source_dir"]) / "WebDriverAgent.xcodeproj").is_dir():
        job = setup(state, "fetch")
        outcome, tail = wait_job(state, job.get("job_id", ""), 600) if job.get("ok") else ("failed", job.get("error"))
        if outcome != "succeeded":
            raise PoolError(f"Fetching WebDriverAgent failed: {tail}")
    outcome, tail = build(state)
    if outcome != "succeeded":
        raise PoolError(f"Building/signing WDA failed: {tail}")
    (state / "pool.json").write_text(json.dumps({"on_demand": args.on_demand}) + "\n")
    return {"name": args.name, "on_demand": args.on_demand, "wda_port": port, "mcp_port": port - WDA_BASE + MCP_BASE,
            "next_steps": ["First time only: on the iPhone open Settings > General > VPN & Device Management and trust your developer certificate.",
                           f"Run: iphone_pool.py start {args.name}",
                           "Then: iphone_pool.py install --expose  (background services + Tailscale)"]}


def cmd_start(args):
    state, _ = phone(args.name)
    breaker_reset(args.name, state, "manual start")
    resigned = False
    if signature_due(state):
        outcome, tail = build(state)
        if outcome != "succeeded":
            raise PoolError(f"Re-signing before start failed (phone must be on the Mac's Wi-Fi or USB): {tail}")
        resigned = True
    result = setup(state, "start")
    if not result.get("ok"):
        raise PoolError(result.get("error", "start failed"))
    return {"name": args.name, "resigned": resigned, "job_id": result.get("job_id"), "service": result.get("service"),
            "next_step": f"Poll iphone_pool.py status until {args.name} is ready (WDA takes ~10-30 s)."}


def cmd_stop(args):
    state, _ = phone(args.name)
    return {"name": args.name, **setup(state, "stop")}


def plist(label, arguments, **extra):
    path = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin:" + os.path.dirname(shutil.which("node") or "/usr/local/bin/node")
    environment = {"PATH": path, "IPHONE_POOL_HOME": str(HOME)}
    if os.environ.get("IPHONE_POOL_IDLE_LOCK"):
        environment["IPHONE_POOL_IDLE_LOCK"] = os.environ["IPHONE_POOL_IDLE_LOCK"]
    if os.environ.get("IPHONE_POOL_NOTIFY"):
        environment["IPHONE_POOL_NOTIFY"] = os.environ["IPHONE_POOL_NOTIFY"]
    return {"Label": label, "ProgramArguments": arguments, "EnvironmentVariables": environment,
            "StandardOutPath": str(LOGS / f"{label}.log"), "StandardErrorPath": str(LOGS / f"{label}.log"), "RunAtLoad": True, **extra}


def services():
    yield f"{LABEL}.watchdog", plist(f"{LABEL}.watchdog", [sys.executable, str(Path(__file__).resolve()), "tick"], StartInterval=30)
    for name, _, config in phones():
        label = f"{LABEL}.mcp.{name}"
        yield label, plist(label, ["/bin/sh", str(ROOT / "pool/serve.sh"), name, str(mcp_port(config))], KeepAlive=True)


def cmd_install(args):
    LOGS.mkdir(parents=True, exist_ok=True)
    AGENTS.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    installed = []
    for label, document in services():
        path = AGENTS / f"{label}.plist"
        run(["launchctl", "bootout", f"{domain}/{label}"])
        path.write_bytes(plistlib.dumps(document))
        for _ in range(10):  # bootout finishes asynchronously; bootstrap fails with EIO until it does
            result = run(["launchctl", "bootstrap", domain, str(path)])
            if result.returncode == 0:
                break
            time.sleep(0.5)
        if result.returncode:
            raise PoolError(f"launchctl bootstrap {label} failed: {result.stderr.strip()}")
        installed.append(label)
    published = []
    if args.expose:
        if not shutil.which("tailscale"):
            raise PoolError("Install Tailscale on this Mac first (https://tailscale.com/download).")
        for name, state, config in phones():
            if not exposed(state):
                continue  # on-demand phones stay on this Mac unless `expose NAME on`
            port = mcp_port(config)
            result = run(["tailscale", "serve", "--bg", f"--tcp={port}", f"tcp://127.0.0.1:{port}"])
            if result.returncode:
                raise PoolError(f"tailscale serve failed: {result.stderr.strip() or result.stdout.strip()}")
            published.append({"name": name, "url": f"http://{tailnet_ip()}:{port}/mcp"})
    local = [{"name": name, "url": f"http://127.0.0.1:{mcp_port(config)}/mcp"} for name, _, config in phones()]
    return {"services": installed, "local": local, "tailnet": published}


def tailnet_ip():
    result = run(["tailscale", "ip", "-4"])
    return result.stdout.strip().splitlines()[0] if result.returncode == 0 and result.stdout.strip() else "<tailscale-ip>"


def cmd_uninstall(args):
    domain = f"gui/{os.getuid()}"
    removed = []
    for path in sorted(AGENTS.glob(f"{LABEL}.*.plist")):
        run(["launchctl", "bootout", f"{domain}/{path.stem}"])
        path.unlink()
        removed.append(path.stem)
    if shutil.which("tailscale"):
        for _, _, config in phones():
            run(["tailscale", "serve", f"--tcp={mcp_port(config)}", "off"])
    return {"removed": removed, "note": "Phone configs, builds and WDA on the phones are kept; stop WDA with stop NAME."}


def cmd_expose(args):
    state, config = phone(args.name)
    port = mcp_port(config)
    if not shutil.which("tailscale"):
        raise PoolError("Install Tailscale on this Mac first (https://tailscale.com/download).")
    if args.state == "on":
        result = run(["tailscale", "serve", "--bg", f"--tcp={port}", f"tcp://127.0.0.1:{port}"])
    else:
        result = run(["tailscale", "serve", f"--tcp={port}", "off"])
    if result.returncode:
        raise PoolError(f"tailscale serve failed: {result.stderr.strip() or result.stdout.strip()}")
    (state / "pool.json").write_text(json.dumps({**options(state), "expose": args.state == "on"}) + "\n")
    return {"name": args.name, "exposed": args.state == "on",
            "tailnet_url": f"http://{tailnet_ip()}:{port}/mcp" if args.state == "on" else None}


def cmd_status(args):
    rows = []
    for name, state, config in phones():
        expiry = profile_expiry(state)
        rows.append({"name": name, "udid": config["udid"], "ready": ready(config), "on_demand": on_demand(state), "exposed": exposed(state), "wda_port": config["local_port"],
                     "mcp_url": f"http://127.0.0.1:{mcp_port(config)}/mcp",
                     "signature_expires": expiry.isoformat() if expiry else None,
                     "restart_failures": breaker(state).get("failures", 0), "restarts_paused_until": breaker(state).get("until")})
    return {"phones": rows}


def cmd_tick(args):
    actions = []
    for name, state, config in phones():
        if on_demand(state):
            continue  # started and stopped by the user; start re-signs if needed
        due = signature_due(state)
        is_ready = ready(config)
        job = None if is_ready and not due else active_job(state)
        if is_ready:
            breaker_reset(name, state, "WDA ready")
        action = decide(is_ready, due, job, (is_ready and not due) or reachable(config["udid"]))
        if action == "start" and breaker_open(state, config["udid"]):
            continue  # paused after repeated failures; the trip was logged once
        if action == "resign":
            outcome, tail = build(state)
            log(f"{name}: re-sign {outcome} {tail[-200:] if outcome != 'succeeded' else ''}")
            if outcome != "succeeded":
                actions.append({"name": name, "action": "resign", "ok": False})
                continue
        if not is_ready and not job:
            blocked = needs_human(state)
            if blocked:
                notify(state, name, *blocked)
        if action:
            result = setup(state, "start")
            log(f"{name}: start ok={result.get('ok')} {result.get('error') or ''}")
            actions.append({"name": name, "action": action, "ok": bool(result.get("ok"))})
            if not is_ready:
                breaker_count(name, state, config["udid"])
        elif is_ready:
            try:
                if idle_lock(name, state, config):
                    actions.append({"name": name, "action": "idle_lock", "ok": True})
            except Exception as error:  # never let locking break the watchdog
                log(f"{name}: idle lock failed: {error}")
    return {"actions": actions}


def cmd_resign(args):
    state, _ = phone(args.name)
    outcome, tail = build(state)
    if outcome != "succeeded":
        raise PoolError(f"Re-sign failed: {tail}")
    return {"name": args.name, "resigned": True, "start": setup(state, "start").get("ok")}


def cmd_hold(args):
    state = DEVICES / args.name
    if not state.is_dir():
        raise PoolError(f"unknown phone {args.name}")
    hold = state / "idle_hold"
    minutes = max(0, min(args.minutes, HOLD_MAX_MINUTES))
    if not minutes:
        hold.unlink(missing_ok=True)
        return {"name": args.name, "hold": None}
    expiry = time.time() + minutes * 60
    hold.touch()
    os.utime(hold, (expiry, expiry))
    return {"name": args.name, "hold_until": dt.datetime.fromtimestamp(expiry, dt.timezone.utc).isoformat()}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="iphone_pool.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("devices", help="list iPhones Xcode can see")
    add = sub.add_parser("add", help="configure, fetch and build WDA for one phone")
    add.add_argument("name")
    add.add_argument("--udid", required=True)
    add.add_argument("--team-id", required=True, help="Apple Team ID (Xcode > Settings > Accounts)")
    add.add_argument("--bundle-id", required=True, help="unique reverse-DNS id you own, e.g. com.yourname.wda")
    add.add_argument("--on-demand", action="store_true", help="personal phone: never auto-started, never exposed on the tailnet")
    for command in ("start", "stop", "resign"):
        sub.add_parser(command).add_argument("name")
    install = sub.add_parser("install", help="launchd services for every phone")
    install.add_argument("--expose", action="store_true", help="also publish each MCP port on your tailnet")
    expose = sub.add_parser("expose", help="publish one phone on your tailnet (on) or withdraw it (off)")
    expose.add_argument("name")
    expose.add_argument("state", choices=("on", "off"))
    hold = sub.add_parser("hold", help="keep the idle auto-lock off for N minutes (0 releases; max 240)")
    hold.add_argument("name")
    hold.add_argument("--minutes", type=int, default=30)
    for command in ("uninstall", "status", "tick"):
        sub.add_parser(command)
    args = parser.parse_args(argv)
    try:
        result = {"ok": True, **globals()["cmd_" + args.command](args)}
        code = 0
    except (PoolError, OSError, subprocess.SubprocessError, ValueError) as error:
        result, code = {"ok": False, "error": str(error)}, 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
