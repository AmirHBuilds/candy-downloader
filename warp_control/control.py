"""
A tiny control service for the WARP container, so the bot can ask for a fresh WARP address WITHOUT being given the
Docker socket (which would be root on the host). Only this container has the socket, and it can do exactly one
thing: re-register the container named WARP_CONTAINER and restart it.

  POST /rotate   (header  Authorization: Bearer <WARP_CONTROL_TOKEN>)   -> {"ok": true} once WARP is back up
  GET  /health   -> {"ok": true}

Standard library only. Not reachable from outside: the compose file publishes no port for it.
"""
import hmac
import http.client
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ.get("WARP_CONTROL_TOKEN", "")
CONTAINER = os.environ.get("WARP_CONTAINER", "candy_warp")
SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
READY_TIMEOUT = int(os.environ.get("WARP_READY_TIMEOUT", "90"))

_lock = threading.Lock()          # one rotation at a time


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("localhost")
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(120)
        self.sock.connect(self._path)


def docker(method: str, path: str, body: dict | None = None) -> tuple[int, bytes]:
    conn = _UnixConnection(SOCKET)
    payload = json.dumps(body) if body is not None else None
    conn.request(method, path, payload, {"Content-Type": "application/json"} if payload else {})
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, data


def run_in_container(*cmd: str) -> tuple[int, str]:
    """Run a command inside the WARP container: (exit code, output)."""
    status, data = docker("POST", f"/containers/{CONTAINER}/exec",
                          {"Cmd": list(cmd), "AttachStdout": True, "AttachStderr": True})
    if status >= 300:
        return 1, data.decode("utf-8", "replace")
    exec_id = json.loads(data)["Id"]
    _, output = docker("POST", f"/exec/{exec_id}/start", {"Detach": False, "Tty": True})
    _, info = docker("GET", f"/exec/{exec_id}/json")
    return json.loads(info).get("ExitCode", 1), output.decode("utf-8", "replace")


def warp_ready() -> bool:
    code, out = run_in_container("warp-cli", "--accept-tos", "status")
    return code == 0 and "Connected" in out


def rotate() -> dict:
    """Forget the current registration, restart the container (its start-up registers anew), and wait until WARP is
    connected again. Returns {"ok": bool, "detail": str}."""
    with _lock:
        run_in_container("warp-cli", "--accept-tos", "registration", "delete")
        status, data = docker("POST", f"/containers/{CONTAINER}/restart?t=5")
        if status >= 300:
            return {"ok": False, "detail": f"restart failed ({status})"}
        started = time.time()
        deadline = started + READY_TIMEOUT
        registered_by_hand = False
        while time.time() < deadline:
            time.sleep(3)
            try:
                if warp_ready():
                    return {"ok": True, "detail": "connected"}
                if not registered_by_hand and time.time() - started > 15:
                    # The start-up did not register by itself: do it explicitly, once.
                    run_in_container("warp-cli", "--accept-tos", "registration", "new")
                    run_in_container("warp-cli", "--accept-tos", "connect")
                    registered_by_hand = True
            except OSError:
                continue
        return {"ok": False, "detail": "WARP did not come back in time"}


def authorized(header: str | None) -> bool:
    if not TOKEN or not header or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[len("Bearer "):], TOKEN)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        self._send(200, {"ok": True}) if self.path == "/health" else self._send(404, {"ok": False})

    def do_POST(self):  # noqa: N802
        if self.path != "/rotate":
            return self._send(404, {"ok": False})
        if not authorized(self.headers.get("Authorization")):
            return self._send(401, {"ok": False, "detail": "unauthorized"})
        if _lock.locked():
            return self._send(409, {"ok": False, "detail": "a change is already running"})
        try:
            self._send(200, rotate())
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"ok": False, "detail": str(exc)[:200]})

    def log_message(self, *args):  # keep the logs quiet (and free of tokens)
        pass


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("WARP_CONTROL_TOKEN is not set - refusing to start without a token.")
    ThreadingHTTPServer(("0.0.0.0", 8765), Handler).serve_forever()
