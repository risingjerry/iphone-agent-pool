import importlib.util
import json
import os
import sys
import time
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_pool(home):
    with mock.patch.dict(os.environ, {"IPHONE_POOL_HOME": str(home)}):
        spec = importlib.util.spec_from_file_location("iphone_pool", ROOT / "pool/iphone_pool.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class DecideTest(unittest.TestCase):
    def setUp(self):
        self.pool = load_pool(Path(tempfile.mkdtemp()))

    def test_ready_and_fresh_needs_nothing(self):
        self.assertIsNone(self.pool.decide(True, False, None, True))

    def test_down_and_reachable_starts(self):
        self.assertEqual(self.pool.decide(False, False, None, True), "start")

    def test_down_but_off_wifi_waits(self):
        self.assertIsNone(self.pool.decide(False, False, None, False))

    def test_running_start_job_is_left_alone(self):
        self.assertIsNone(self.pool.decide(False, False, {"action": "start"}, True))

    def test_build_in_progress_is_left_alone(self):
        self.assertIsNone(self.pool.decide(False, True, {"action": "build"}, True))

    def test_expiring_signature_resigns_even_when_ready(self):
        self.assertEqual(self.pool.decide(True, True, {"action": "start"}, True), "resign")

    def test_expiring_signature_waits_for_reachability(self):
        self.assertIsNone(self.pool.decide(True, True, None, False))


class ServicesTest(unittest.TestCase):
    def test_one_watchdog_and_one_gateway_per_phone(self):
        home = Path(tempfile.mkdtemp())
        for name, port in (("iphone12", 18100), ("iphone15", 18101)):
            (home / "devices" / name).mkdir(parents=True)
            (home / "devices" / name / "config.json").write_text(json.dumps({"udid": "x" * 25, "local_port": port}))
        pool = load_pool(home)
        services = dict(pool.services())
        self.assertEqual(sorted(services), ["dev.iphone-agent-pool.mcp.iphone12", "dev.iphone-agent-pool.mcp.iphone15",
                                            "dev.iphone-agent-pool.watchdog"])
        self.assertEqual(services["dev.iphone-agent-pool.watchdog"]["StartInterval"], 30)
        self.assertEqual(services["dev.iphone-agent-pool.mcp.iphone15"]["ProgramArguments"][-2:], ["iphone15", "18201"])
        self.assertTrue(services["dev.iphone-agent-pool.mcp.iphone12"]["KeepAlive"])
        self.assertEqual(services["dev.iphone-agent-pool.watchdog"]["EnvironmentVariables"]["IPHONE_POOL_HOME"], str(home))


class CliTest(unittest.TestCase):
    def test_bad_name_is_rejected_as_json(self):
        pool = load_pool(Path(tempfile.mkdtemp()))
        with mock.patch("builtins.print") as printed:
            code = pool.main(["add", "Bad Name", "--udid", "u", "--team-id", "T", "--bundle-id", "a.b.c"])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(printed.call_args[0][0])["ok"])

    def test_unknown_phone(self):
        pool = load_pool(Path(tempfile.mkdtemp()))
        with mock.patch("builtins.print") as printed:
            self.assertEqual(pool.main(["start", "nope"]), 1)
        self.assertIn("Unknown phone", json.loads(printed.call_args[0][0])["error"])

    def test_status_with_no_phones(self):
        pool = load_pool(Path(tempfile.mkdtemp()))
        with mock.patch("builtins.print") as printed:
            self.assertEqual(pool.main(["status"]), 0)
        self.assertEqual(json.loads(printed.call_args[0][0]), {"ok": True, "phones": []})


class DeviceHostTest(unittest.TestCase):
    def setUp(self):
        import sys
        sys.path.insert(0, str(ROOT / "server"))
        from wda_setup import SetupManager
        self.manager = SetupManager(Path(tempfile.mkdtemp()))
        self.args = {"udid": "00000000-0000000000000000", "team_id": "ABCDE12345", "bundle_id": "com.example.wda"}

    def test_tunnel_host_is_stored(self):
        result = self.manager.setup("configure", device_host="tunnel", **self.args)
        self.assertTrue(result["ok"])
        self.assertEqual(result["config"]["device_host"], "tunnel")

    def test_usb_default_has_no_host(self):
        self.assertNotIn("device_host", self.manager.setup("configure", **self.args)["config"])

    def test_injection_shaped_host_is_rejected(self):
        self.assertFalse(self.manager.setup("configure", device_host="a;rm -rf /", **self.args)["ok"])


class OnDemandTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        for name, port, demand in (("pool1", 18100, False), ("mine", 18101, True)):
            state = self.home / "devices" / name
            state.mkdir(parents=True)
            (state / "config.json").write_text(json.dumps({"udid": "x" * 25, "local_port": port}))
            (state / "pool.json").write_text(json.dumps({"on_demand": demand}))
        self.pool = load_pool(self.home)

    def test_watchdog_never_starts_on_demand_phones(self):
        with mock.patch.object(self.pool, "ready", return_value=False), \
             mock.patch.object(self.pool, "profile_expiry", return_value=None), \
             mock.patch.object(self.pool, "active_job", return_value=None), \
             mock.patch.object(self.pool, "reachable", return_value=True), \
             mock.patch.object(self.pool, "setup", return_value={"ok": True}) as setup:
            result = self.pool.cmd_tick(None)
        self.assertEqual([a["name"] for a in result["actions"]], ["pool1"])
        self.assertTrue(all("mine" not in str(call) for call in setup.call_args_list))

    def test_status_reports_mode(self):
        with mock.patch.object(self.pool, "ready", return_value=False), \
             mock.patch.object(self.pool, "profile_expiry", return_value=None):
            rows = {row["name"]: row["on_demand"] for row in self.pool.cmd_status(None)["phones"]}
        self.assertEqual(rows, {"mine": True, "pool1": False})

    def test_expose_defaults_and_override(self):
        self.assertTrue(self.pool.exposed(self.home / "devices" / "pool1"))
        self.assertFalse(self.pool.exposed(self.home / "devices" / "mine"))
        with mock.patch.object(self.pool.shutil, "which", return_value="/usr/bin/tailscale"), \
             mock.patch.object(self.pool, "run", return_value=mock.Mock(returncode=0, stdout="100.64.0.1\n", stderr="")):
            result = self.pool.cmd_expose(__import__("argparse").Namespace(name="mine", state="on"))
        self.assertTrue(result["exposed"])
        self.assertTrue(self.pool.exposed(self.home / "devices" / "mine"))
        self.assertTrue(self.pool.on_demand(self.home / "devices" / "mine"))

    def test_start_resigns_first_when_signature_is_due(self):
        import datetime as dt
        soon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
        with mock.patch.object(self.pool, "profile_expiry", return_value=soon), \
             mock.patch.object(self.pool, "build", return_value=("succeeded", "")) as build, \
             mock.patch.object(self.pool, "setup", return_value={"ok": True, "job_id": "j"}):
            result = self.pool.cmd_start(__import__("argparse").Namespace(name="mine"))
        build.assert_called_once()
        self.assertTrue(result["resigned"])

    def test_resign_that_kept_the_same_expiry_is_not_repeated(self):
        import datetime as dt
        soon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
        state = self.pool.DEVICES / "mine"
        with mock.patch.object(self.pool, "profile_expiry", return_value=soon):
            self.assertTrue(self.pool.signature_due(state))
            (state / "resigned.json").write_text(json.dumps({"expiry": soon.isoformat(), "at": dt.datetime.now(dt.timezone.utc).isoformat()}))
            self.assertFalse(self.pool.signature_due(state))
        with mock.patch.object(self.pool, "profile_expiry", return_value=soon + dt.timedelta(hours=1)):
            self.assertTrue(self.pool.signature_due(state))


class SetupPortTest(unittest.TestCase):
    def test_each_phone_uses_its_own_wda_port(self):
        home = Path(tempfile.mkdtemp())
        state = home / "devices" / "second"
        state.mkdir(parents=True)
        (state / "config.json").write_text(json.dumps({"udid": "x" * 25, "local_port": 18101}))
        pool = load_pool(home)
        with mock.patch.object(pool, "run", return_value=mock.Mock(stdout='{"ok": true}', stderr="")) as run:
            pool.setup(state, "status")
            pool.setup(home / "devices" / "new", "configure", port=18102)
        self.assertIn("http://127.0.0.1:18101", run.call_args_list[0][0][0])
        self.assertIn("http://127.0.0.1:18102", run.call_args_list[1][0][0])


class NotifyTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.state = self.home / "devices" / "p"
        (self.state / "logs").mkdir(parents=True)
        (self.state / "config.json").write_text(json.dumps({"udid": "x" * 25, "local_port": 18100}))
        self.pool = load_pool(self.home)

    def test_detects_untrusted_certificate(self):
        (self.state / "logs" / "a.log").write_text("... Developer App Certificate is not trusted ...")
        self.assertEqual(self.pool.needs_human(self.state)[0], "trust")

    def test_clean_log_needs_nobody(self):
        (self.state / "logs" / "a.log").write_text("ServerURLHere->http://x:8100<-ServerURLHere")
        self.assertIsNone(self.pool.needs_human(self.state))

    def test_notifies_once_then_throttles_and_runs_hook(self):
        with mock.patch.dict(os.environ, {"IPHONE_POOL_NOTIFY": "/bin/echo hi"}), \
             mock.patch.object(self.pool, "run") as run:
            self.assertTrue(self.pool.notify(self.state, "p", "trust", "Trust {name}"))
            self.assertFalse(self.pool.notify(self.state, "p", "trust", "Trust {name}"))
        commands = [call[0][0] for call in run.call_args_list]
        self.assertEqual(commands[0][0], "osascript")
        self.assertEqual(commands[1], ["/bin/echo", "hi", "Trust p"])
        self.assertEqual(len(commands), 2)

    def test_tick_notifies_when_blocked(self):
        (self.state / "logs" / "a.log").write_text("not trusted")
        with mock.patch.object(self.pool, "ready", return_value=False), \
             mock.patch.object(self.pool, "profile_expiry", return_value=None), \
             mock.patch.object(self.pool, "active_job", return_value=None), \
             mock.patch.object(self.pool, "reachable", return_value=True), \
             mock.patch.object(self.pool, "setup", return_value={"ok": True}), \
             mock.patch.object(self.pool, "notify") as notify:
            self.pool.cmd_tick(None)
        notify.assert_called_once()
        self.assertEqual(notify.call_args[0][2], "trust")


class BreakerTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.state = self.home / "devices" / "p"
        (self.state / "logs").mkdir(parents=True)
        (self.state / "config.json").write_text(json.dumps({"udid": "x" * 25, "local_port": 18100}))
        self.pool = load_pool(self.home)

    def tick(self, ready=False, link="localNetwork"):
        with mock.patch.object(self.pool, "ready", return_value=ready), \
             mock.patch.object(self.pool, "profile_expiry", return_value=None), \
             mock.patch.object(self.pool, "active_job", return_value=None), \
             mock.patch.object(self.pool, "reachable", return_value=True), \
             mock.patch.object(self.pool, "transport", return_value=link), \
             mock.patch.object(self.pool, "idle_lock", return_value=False), \
             mock.patch.object(self.pool, "notify") as notify, \
             mock.patch.object(self.pool, "setup", return_value={"ok": True}) as setup:
            self.pool.cmd_tick(None)
        starts = sum(1 for call in setup.call_args_list if call[0][1:] == ("start",))
        return starts, notify

    def test_stops_restarting_after_repeated_failures_and_notifies_once(self):
        starts = [self.tick()[0] for _ in range(self.pool.BREAKER_AFTER + 10)]
        self.assertEqual(sum(starts), self.pool.BREAKER_AFTER)
        record = json.loads((self.state / "breaker.json").read_text())
        self.assertEqual(record["trips"], 1)
        self.assertEqual(record["transport"], "localNetwork")

    def test_trip_notifies_owner_with_usb_advice(self):
        for _ in range(self.pool.BREAKER_AFTER - 1):
            self.tick()
        _, notify = self.tick()
        notify.assert_called_once()
        self.assertEqual(notify.call_args[0][2], "breaker")
        self.assertIn("USB", notify.call_args[0][3])

    def test_retries_after_the_pause_and_doubles_it(self):
        import datetime as dt
        for _ in range(self.pool.BREAKER_AFTER):
            self.tick()
        record = json.loads((self.state / "breaker.json").read_text())
        record["until"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)).isoformat()
        (self.state / "breaker.json").write_text(json.dumps(record))
        self.assertEqual(self.tick()[0], 1)
        self.assertEqual(self.tick()[0], 0)
        record = json.loads((self.state / "breaker.json").read_text())
        self.assertEqual(record["trips"], 2)
        pause = dt.datetime.fromisoformat(record["until"]) - dt.datetime.now(dt.timezone.utc)
        self.assertGreater(pause, self.pool.BREAKER_BASE * 1.9)

    def test_pause_is_capped(self):
        (self.state / "breaker.json").write_text(json.dumps({"failures": 99, "trips": 300}))
        self.tick()
        import datetime as dt
        until = dt.datetime.fromisoformat(json.loads((self.state / "breaker.json").read_text())["until"])
        self.assertLessEqual(until - dt.datetime.now(dt.timezone.utc), self.pool.BREAKER_MAX)

    def test_plugging_in_usb_gets_an_immediate_retry(self):
        for _ in range(self.pool.BREAKER_AFTER):
            self.tick()
        self.assertEqual(self.tick(link="localNetwork")[0], 0)
        self.assertEqual(self.tick(link="wired")[0], 1)

    def test_ready_wda_closes_the_breaker(self):
        for _ in range(self.pool.BREAKER_AFTER):
            self.tick()
        self.tick(ready=True)
        self.assertFalse((self.state / "breaker.json").exists())
        self.assertEqual(self.tick()[0], 1)

    def test_manual_start_closes_the_breaker(self):
        (self.state / "breaker.json").write_text(json.dumps({"failures": 9, "trips": 1, "until": "2999-01-01T00:00:00+00:00"}))
        with mock.patch.object(self.pool, "profile_expiry", return_value=None), \
             mock.patch.object(self.pool, "setup", return_value={"ok": True, "job_id": "j"}):
            self.pool.cmd_start(__import__("argparse").Namespace(name="p"))
        self.assertFalse((self.state / "breaker.json").exists())

    def test_status_shows_the_pause(self):
        (self.state / "breaker.json").write_text(json.dumps({"failures": 5, "trips": 1, "until": "2999-01-01T00:00:00+00:00"}))
        with mock.patch.object(self.pool, "ready", return_value=False), \
             mock.patch.object(self.pool, "profile_expiry", return_value=None):
            row = self.pool.cmd_status(None)["phones"][0]
        self.assertEqual(row["restart_failures"], 5)
        self.assertEqual(row["restarts_paused_until"], "2999-01-01T00:00:00+00:00")


class InstallTest(unittest.TestCase):
    def test_install_expose_publishes_only_exposed_phones(self):
        home = Path(tempfile.mkdtemp())
        for name, port, opts in (("pool1", 18100, {}), ("mine", 18101, {"on_demand": True})):
            state = home / "devices" / name
            state.mkdir(parents=True)
            (state / "config.json").write_text(json.dumps({"udid": "x" * 25, "local_port": port}))
            (state / "pool.json").write_text(json.dumps(opts))
        pool = load_pool(home)
        pool.AGENTS = home / "LaunchAgents"
        with mock.patch.object(pool.shutil, "which", return_value="/usr/bin/tailscale"), \
             mock.patch.object(pool, "run", return_value=mock.Mock(returncode=0, stdout="100.64.0.1\n", stderr="")) as run:
            result = pool.cmd_install(__import__("argparse").Namespace(expose=True))
        self.assertEqual([row["name"] for row in result["tailnet"]], ["pool1"])
        served = [call[0][0] for call in run.call_args_list if call[0][0][:2] == ["tailscale", "serve"]]
        self.assertEqual(served, [["tailscale", "serve", "--bg", "--tcp=18200", "tcp://127.0.0.1:18200"]])
        self.assertEqual(len(list(pool.AGENTS.glob("*.plist"))), 3)


class PuaClientTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("phone_client", ROOT / "pool/phone_client.py")
        self.client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.client)

    def test_flatten_parses_json_and_saves_images(self):
        import base64
        out = tempfile.mkdtemp()
        result = {"content": [{"type": "text", "text": '{"ready": true}'},
                              {"type": "image", "mimeType": "image/jpeg", "data": base64.b64encode(b"jpg").decode()}]}
        parts = self.client.flatten(result, out)
        self.assertEqual(parts[0], {"ready": True})
        self.assertEqual(Path(parts[1]["image"]).read_bytes(), b"jpg")

    def test_unreachable_endpoint_is_a_json_error(self):
        with mock.patch("builtins.print") as printed:
            code = self.client.main(["--url", "http://127.0.0.1:9/mcp", "--timeout", "2", "--direct", "tools"])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(printed.call_args[0][0])["ok"])

    def test_proxies_are_never_used(self):
        # http.client never consults proxy variables; the call must fail on the real address.
        with mock.patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1", "HTTP_PROXY": "http://127.0.0.1:1"}):
            with mock.patch("builtins.print") as printed:
                self.client.main(["--url", "http://127.0.0.1:9/mcp", "--timeout", "2", "--direct", "tools"])
        self.assertIn("refused", json.loads(printed.call_args[0][0])["error"].lower())

    def test_helper_keeps_one_session_across_calls(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        seen = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *args): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append((body.get("method"), self.headers.get("Mcp-Session-Id")))
                if "id" not in body:
                    self.send_response(202); self.send_header("Content-Length", "0"); self.end_headers(); return
                result = {"tools": []} if body["method"] == "tools/list" else {"protocolVersion": "x"}
                data = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Mcp-Session-Id", "s1"); self.send_header("Content-Length", str(len(data)))
                self.end_headers(); self.wfile.write(data)
            def do_DELETE(self):
                self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers()

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_port}/mcp"
        home = tempfile.mkdtemp(dir="/tmp")
        try:
            with mock.patch.dict(os.environ, {"HOME": home}):
                for _ in range(3):
                    with mock.patch("builtins.print"):
                        self.assertEqual(self.client.main(["--url", url, "--timeout", "5", "tools"]), 0)
                with mock.patch("builtins.print"):
                    self.client.main(["--url", url, "close"])
                # Without a helper (sandboxes that kill background processes) the cached id is reused.
                for _ in range(3):
                    with mock.patch("builtins.print"):
                        self.assertEqual(self.client.main(["--url", url, "--timeout", "5", "--direct", "tools"]), 0)
        finally:
            server.shutdown()
        methods = [method for method, _ in seen]
        self.assertEqual(methods.count("initialize"), 2)
        self.assertEqual(methods.count("tools/list"), 6)

class IdleLockTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.state = self.home / "devices" / "p"
        self.state.mkdir(parents=True)
        self.config = {"udid": "x" * 25, "local_port": 18100}
        self.pool = load_pool(self.home)

    def lock(self):
        session = mock.Mock()
        session.call.return_value = {"content": []}
        with mock.patch.dict(sys.modules, {"phone_client": mock.Mock(Session=mock.Mock(return_value=session))}):
            return self.pool.idle_lock("p", self.state, self.config), session

    def test_never_used_phone_is_left_alone(self):
        self.assertFalse(self.lock()[0])

    def test_recent_activity_is_left_alone(self):
        (self.state / "last_activity").touch()
        self.assertFalse(self.lock()[0])

    def test_idle_phone_is_locked_once(self):
        activity = self.state / "last_activity"
        activity.touch()
        old = time.time() - self.pool.IDLE_LOCK - 5
        os.utime(activity, (old, old))
        locked, session = self.lock()
        self.assertTrue(locked)
        self.assertEqual(session.call.call_args[0][1]["arguments"], {"name": "lock"})
        self.assertFalse(self.lock()[0])  # marker is newer than the last activity

    def make_idle(self):
        activity = self.state / "last_activity"
        activity.touch()
        old = time.time() - self.pool.IDLE_LOCK - 5
        os.utime(activity, (old, old))

    def test_hold_keeps_idle_phone_unlocked(self):
        # Raw WDA calls (curl) never touch last_activity; a hold covers a long manual flow.
        self.make_idle()
        with mock.patch("builtins.print"):
            self.assertEqual(self.pool.main(["hold", "p", "--minutes", "30"]), 0)
        locked, session = self.lock()
        self.assertFalse(locked)
        session.call.assert_not_called()

    def test_expired_hold_is_ignored(self):
        self.make_idle()
        hold = self.state / "idle_hold"
        hold.touch()
        past = time.time() - 1
        os.utime(hold, (past, past))
        self.assertTrue(self.lock()[0])

    def test_hold_zero_releases(self):
        self.make_idle()
        with mock.patch("builtins.print"):
            self.pool.main(["hold", "p", "--minutes", "30"])
            self.assertEqual(self.pool.main(["hold", "p", "--minutes", "0"]), 0)
        self.assertFalse((self.state / "idle_hold").exists())
        self.assertTrue(self.lock()[0])

    def test_hold_is_capped(self):
        with mock.patch("builtins.print"):
            self.pool.main(["hold", "p", "--minutes", "100000"])
        expiry = (self.state / "idle_hold").stat().st_mtime
        self.assertLessEqual(expiry, time.time() + self.pool.HOLD_MAX_MINUTES * 60 + 5)


if __name__ == "__main__":
    unittest.main()
