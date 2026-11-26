#!/usr/bin/env python3
"""Minimal MCP client for one pool phone, standard library only.

For agents that can run shell commands but cannot attach an MCP server
themselves (e.g. a cloud VM on your tailnet).

  phone_client.py --url http://<mac-tailscale-ip>:18200/mcp tools
  phone_client.py --url ... call pua_ready '{}'
  phone_client.py --url ... call pua_observe '{"mode": "screenshot"}' --out ./shots
  phone_client.py --url ... close

Each call prints one JSON object ({"ok", "result"|"error"}); image content
is saved to --out and replaced by its file path. Exit code 0 ok, 1 error.

Speed: the first call starts a small helper process (one per URL, exits after
15 idle minutes) that keeps the HTTP connection and the MCP session open, so
later calls cost a single round trip and the phone server stays warm. If the
helper cannot run (some sandboxes kill background processes), calls go direct
and still reuse the MCP session by its cached id. `close` ends both.
"""
import argparse
import base64
import hashlib
import http.client
import json
import os
import socket
import subprocess
import sys
import time
import urllib.parse

PROTOCOL = "2025-06-18"
IDLE_SECONDS = 900


class Session:
    """One MCP session over a kept-alive HTTP connection (never via proxies: the
    endpoint lives on a tailnet/LAN address)."""

    def __init__(self, url, timeout):
        parts = urllib.parse.urlsplit(url)
        self.host, self.port, self.path = parts.hostname, parts.port or 80, parts.path or "/"
        self.timeout, self.session_id, self.connection = timeout, None, None
        self.next_id = int(time.time() * 1000) % 10**9

    def http(self, method, body=None):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": PROTOCOL}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        for attempt in (0, 1):
            if self.connection is None:
                self.connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
            try:
                self.connection.request(method, self.path, body, headers)
                response = self.connection.getresponse()
                return response, response.read().decode()
            except (http.client.HTTPException, ConnectionError, BrokenPipeError):
                # A kept-alive connection the server closed: reconnect once.
                self.connection.close()
                self.connection = None
                if attempt:
                    raise

    def post(self, message):
        response, body = self.http("POST", json.dumps(message).encode())
        if response.status == 404 or response.status == 400 and self.session_id:
            raise SessionExpired(body[:200])
        if response.status >= 300:
            raise RuntimeError(f"HTTP {response.status}: {body[:200]}")
        self.session_id = response.getheader("Mcp-Session-Id", self.session_id)
        if "id" not in message:
            return None
        if response.getheader("Content-Type", "").startswith("text/event-stream"):
            for line in body.splitlines():
                if line.startswith("data:"):
                    reply = json.loads(line[5:])
                    if reply.get("id") == message["id"]:
                        return reply
            raise RuntimeError("No response in event stream.")
        return json.loads(body)

    def request(self, method, params=None):
        self.next_id += 1
        reply = self.post({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}})
        if "error" in reply:
            message = reply["error"].get("message", str(reply["error"]))
            if "session" in message.lower():
                raise SessionExpired(message)
            raise RuntimeError(message)
        return reply["result"]

    def open(self):
        self.session_id = None
        self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                    "clientInfo": {"name": "phone_client", "version": "2"}})
        self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, method, params=None):
        """Use the open session; if the server expired it, open a fresh one once."""
        if self.session_id:
            try:
                return self.request(method, params)
            except SessionExpired:
                pass
        self.open()
        return self.request(method, params)

    def close(self):
        if self.session_id:
            try:
                self.http("DELETE")
            except (OSError, http.client.HTTPException):
                pass
        self.session_id = None


class SessionExpired(Exception):
    pass


def socket_path(url):
    directory = os.path.expanduser("~/.cache/phone_client")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    return os.path.join(directory, hashlib.sha256(url.encode()).hexdigest()[:16] + ".sock")


def helper(url, timeout):
    """Serve calls for one URL over a Unix socket, keeping the session warm."""
    path = socket_path(url)
    if os.path.exists(path):
        os.unlink(path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    os.chmod(path, 0o600)
    server.listen(4)
    server.settimeout(IDLE_SECONDS)
    session = Session(url, timeout)
    try:
        while True:
            try:
                client, _ = server.accept()
            except socket.timeout:
                break
            with client:
                request = json.loads(read_line(client))
                if request.get("method") == "close":
                    session.close()
                    client.sendall(b'{"result": {"closed": true}}\n')
                    break
                try:
                    reply = {"result": session.call(request["method"], request.get("params"))}
                except Exception as error:  # report every failure to the caller, keep serving
                    session.connection = None
                    reply = {"error": str(error)}
                client.sendall((json.dumps(reply) + "\n").encode())
    finally:
        server.close()
        if os.path.exists(path):
            os.unlink(path)


def read_line(sock):
    data = b""
    while not data.endswith(b"\n"):
        chunk = sock.recv(1 << 20)
        if not chunk:
            break
        data += chunk
    return data


def via_helper(url, timeout, method, params=None):
    """Send one call through the helper, starting it if needed; None if unavailable."""
    path = socket_path(url)
    for attempt in range(2):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(timeout + 5)
                client.connect(path)
                client.sendall((json.dumps({"method": method, "params": params}) + "\n").encode())
                reply = json.loads(read_line(client))
            if "error" in reply:
                raise RuntimeError(reply["error"])
            return reply["result"]
        except (FileNotFoundError, ConnectionRefusedError):
            if attempt or method == "close":
                return None
            subprocess.Popen([sys.executable, os.path.abspath(__file__), "--url", url, "--timeout", str(timeout), "helper"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            for _ in range(40):
                if os.path.exists(path):
                    break
                time.sleep(0.05)
    return None


def flatten(result, out):
    """Text parts are parsed as JSON when possible; images become saved file paths."""
    parts = []
    for item in result.get("content", []):
        if item.get("type") == "image":
            os.makedirs(out, exist_ok=True)
            extension = item.get("mimeType", "image/png").split("/")[-1]
            path = os.path.join(out, f"phone-{time.strftime('%Y%m%d-%H%M%S')}-{len(parts)}.{extension}")
            with open(path, "wb") as stream:
                stream.write(base64.b64decode(item["data"]))
            parts.append({"image": os.path.abspath(path)})
        elif item.get("type") == "text":
            try:
                parts.append(json.loads(item["text"]))
            except ValueError:
                parts.append(item["text"])
    return parts[0] if len(parts) == 1 else parts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.environ.get("PUA_URL"), help="phone MCP URL (or $PUA_URL)")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--out", default="pua-shots", help="directory for screenshots")
    parser.add_argument("--direct", action="store_true", help="skip the helper (one-off session)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("tools", help="list tool names and descriptions")
    sub.add_parser("close", help="end the session and stop the helper")
    sub.add_parser("helper", help=argparse.SUPPRESS)
    call = sub.add_parser("call", help="call one tool")
    call.add_argument("name")
    call.add_argument("arguments", nargs="?", default="{}", help="JSON object")
    call.add_argument("--out", dest="call_out", help="same as the global --out; accepted after call too")
    args = parser.parse_args(argv)
    if not args.url:
        parser.error("--url or $PUA_URL is required")
    if args.command == "helper":
        helper(args.url, args.timeout)
        return 0

    def rpc(method, params=None):
        if not args.direct:
            result = via_helper(args.url, args.timeout, method, params)
            if result is not None:
                return result
        # No helper (some agent sandboxes kill background processes): still reuse the MCP
        # session through its cached id, so the Mac keeps one warm server.
        session = Session(args.url, args.timeout)
        cache = socket_path(args.url)[:-5] + ".session"
        try:
            with open(cache) as stream:
                session.session_id = stream.read().strip() or None
        except OSError:
            pass
        result = session.call(method, params)
        with open(cache, "w") as stream:
            stream.write(session.session_id or "")
        return result

    try:
        if args.command == "close":
            via_helper(args.url, args.timeout, "close")
            try:
                os.unlink(socket_path(args.url)[:-5] + ".session")
            except OSError:
                pass
            output = {"ok": True, "closed": True}
        elif args.command == "tools":
            tools = rpc("tools/list").get("tools", [])
            output = {"ok": True, "tools": [{"name": t["name"], "description": t.get("description", "")[:300]} for t in tools]}
        else:
            result = rpc("tools/call", {"name": args.name, "arguments": json.loads(args.arguments)})
            output = {"ok": not result.get("isError"), "result": flatten(result, args.call_out or args.out)}
    except (OSError, ValueError, RuntimeError, KeyError, http.client.HTTPException, SessionExpired) as error:
        output = {"ok": False, "error": str(error)}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
