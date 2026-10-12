"""Authenticated bridge between Hermes Agent tools and the local gateway browser-control API.

Architecture
------------
The Hermes gateway (default 127.0.0.1:8642) already exposes two browser-control
endpoints built for the Chrome extension's controller service worker:

  POST /v1/browser-control/register   -> returns { ticket, controller_id, ... }
  WS   /v1/browser-control/ws         -> JSON-RPC frames (command/result/cancel)

``GatewayBrowserBridge`` opens ONE persistent WebSocket per Hermes session and
sends ``browser.controller.command`` frames on demand.  The extension already has
``chrome.debugger`` wired; the gateway routes commands from the WS to the registered
extension controller which calls CDP.

Lifecycle
---------
- ``connect()``       – register + open WS; idempotent (returns existing if healthy).
- ``send_command()``  – enqueue a browser-control command frame; wait for the result.
- ``disconnect()``    – close the WS gracefully; clears the singleton.
- Auto-reconnect      – ``send_command()`` re-registers/re-connects on any dead socket.

Security
--------
- Binds to 127.0.0.1 only (enforced by URL validation).
- Reads the gateway URL and API key from env/config; never logs or returns secret values.
- ``API_SERVER_ENABLED`` must be ``true`` or the bridge stays dormant.
- Every HTTP request carries ``Authorization: Bearer <key>``.
- All outbound frames include ``session_id`` and ``controller_id`` so the gateway can
  reject impersonation.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

_REGISTER_PATH = "/v1/browser-control/register"
_WS_PATH = "/v1/browser-control/ws"
_WS_PROTOCOLS = ["hermes-browser-control-v1"]

_DEFAULT_GATEWAY_URL = "http://127.0.0.1:8642"
_COMMAND_TIMEOUT_S = 30.0
_CONNECT_TIMEOUT_S = 10.0
_RECONNECT_COOLDOWN_S = 5.0
_MAX_PENDING = 32
_PRODUCT = {
    "id": "chromium",
    "engine": "chromium",
    "label": "Hermes Browser Companion",
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _loopback_only(url: str) -> str:
    """Raise if URL does not point at a loopback address; return it unchanged."""
    import urllib.parse as _up
    parsed = _up.urlparse(url)
    host = parsed.hostname or ""
    if host not in ("127.0.0.1", "localhost", "::1", "[::1]") and not host.startswith("127."):
        raise ValueError(
            f"Gateway bridge requires a loopback URL; got {host!r}. "
            "Set HERMES_GATEWAY_URL to http://127.0.0.1:8642 (or similar)."
        )
    if parsed.scheme not in ("http", "https", "ws", "wss"):
        raise ValueError(f"Gateway URL scheme must be http/https; got {parsed.scheme!r}.")
    return url


def _read_env_config() -> dict[str, str]:
    """Return gateway config from environment variables, never raising."""
    return {
        "gateway_url": os.environ.get("HERMES_GATEWAY_URL", _DEFAULT_GATEWAY_URL).rstrip("/"),
        "api_key": os.environ.get("HERMES_API_SERVER_KEY", os.environ.get("API_SERVER_KEY", "")),
        "enabled": os.environ.get("API_SERVER_ENABLED", "true").strip().lower(),
    }


def _api_enabled(cfg: dict[str, str]) -> bool:
    return cfg.get("enabled", "true") not in ("false", "0", "no", "off")


def _auth_headers(api_key: str) -> dict[str, str]:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _http_post(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    """Synchronous HTTP POST; returns parsed JSON or raises."""
    body = json.dumps(payload).encode("utf-8")
    req = Request(url, data=body, headers=headers, method="POST")  # noqa: S310
    try:
        with urlopen(req, timeout=_CONNECT_TIMEOUT_S) as resp:  # noqa: S310
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except HTTPError as exc:
        raw = exc.read() if exc.fp else b""
        try:
            body_json = json.loads(raw)
        except Exception:
            body_json = {"detail": raw.decode("utf-8", errors="replace")[:500]}
        raise BridgeError(f"HTTP {exc.code} from {url}: {body_json}") from exc
    except URLError as exc:
        raise BridgeError(f"Cannot reach gateway at {url}: {exc.reason}") from exc


# ── Exceptions ─────────────────────────────────────────────────────────────────

class BridgeError(RuntimeError):
    """Raised for gateway communication failures (not tool-level errors)."""


class BridgeDisabledError(BridgeError):
    """Raised when API_SERVER_ENABLED is false or the feature is disabled."""


# ── WebSocket shim (stdlib only; optional websockets upgrade) ──────────────────

try:
    import websockets.sync.client as _ws_sync  # type: ignore[import-untyped]
    _HAS_WEBSOCKETS = True
except ImportError:
    _HAS_WEBSOCKETS = False


class _WebSocketAdapter:
    """Thin wrapper so we can swap ws backends without touching call sites.

    websockets ≥ 13 sync API notes
    --------------------------------
    - `ClientConnection` does NOT expose a stable public `.socket` attribute.
      Calling `.socket.settimeout()` or inspecting `.socket.fileno()` directly
      corrupts the library's internal I/O state and must not be done.
    - `recv()` is blocking with no timeout parameter in the sync API; we rely
      on the reader daemon thread being terminated (bridge.disconnect()) to
      unblock any pending recv() call rather than using socket-level timeouts.
    - Closed-state detection uses the `closed` property added in websockets 13
      (`ClientConnection.closed`), falling back to a zero-byte send probe.
    """

    def __init__(self, uri: str, additional_headers: dict[str, str], subprotocols: list[str]) -> None:
        if not _HAS_WEBSOCKETS:
            raise BridgeError(
                "The 'websockets' package is required for browser control. "
                "Install it with:  pip install websockets>=13"
            )
        self._conn = _ws_sync.connect(
            uri,
            additional_headers=additional_headers,
            subprotocols=subprotocols,  # type: ignore[arg-type]
            open_timeout=_CONNECT_TIMEOUT_S,
        )

    def send(self, data: str) -> None:
        self._conn.send(data)

    def recv(self, timeout: float | None = None) -> str:  # noqa: ARG002  (timeout unused — see class note)
        # The sync recv() is blocking; timeout is intentionally ignored here.
        # The reader daemon thread is terminated via bridge.disconnect() which
        # closes the connection and causes recv() to raise, exiting the loop.
        return self._conn.recv()  # type: ignore[return-value]

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    @property
    def closed(self) -> bool:
        # Prefer the public `.closed` bool added in websockets 13.
        # Fall back to a harmless zero-byte send probe on older builds.
        try:
            closed_attr = getattr(self._conn, "closed", None)
            if isinstance(closed_attr, bool):
                return closed_attr
            # Probe: send raises ConnectionClosed if the socket is gone.
            # Use a ping/keepalive if available, otherwise rely on the exception.
            self._conn.ping()  # type: ignore[attr-defined]
            return False
        except Exception:
            return True


# ── Core bridge ────────────────────────────────────────────────────────────────

class GatewayBrowserBridge:
    """One bridge per Hermes session.  Thread-safe.

    Call ``connect()`` before sending commands.  ``send_command()`` will
    attempt one auto-reconnect on a dead socket.
    """

    def __init__(self, session_id: str, cfg: dict[str, str] | None = None) -> None:
        self._cfg = cfg or _read_env_config()
        self._session_id = session_id
        self._controller_id: str = ""
        self._browser_profile_id: str = ""
        self._ws: _WebSocketAdapter | None = None
        self._lock = threading.Lock()
        self._pending: dict[str, threading.Event] = {}   # command_id -> Event
        self._results: dict[str, dict[str, Any]] = {}    # command_id -> result
        self._reader_thread: threading.Thread | None = None
        self._connected = False
        self._last_connect_attempt = 0.0
        self._generation = 1

    # ── Public API ──────────────────────────────────────────────────────────

    def connect(self) -> dict[str, Any]:
        """Register and open the controller WebSocket.  Idempotent."""
        with self._lock:
            if self._connected and self._ws and not self._ws.closed:
                return {"ok": True, "controller_id": self._controller_id, "reused": True}
            return self._connect_locked()

    def send_command(
        self,
        action: str,
        arguments: dict[str, Any] | None = None,
        tab_id: int | None = None,
        deadline_at: float | None = None,
        timeout: float = _COMMAND_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Send a browser-control command frame; block until result or timeout.

        Auto-reconnects once on a dead socket.
        """
        if not _api_enabled(self._cfg):
            return {"ok": False, "error": {"code": "api_disabled", "message": "API_SERVER_ENABLED is false."}}

        with self._lock:
            if not self._connected or not self._ws or self._ws.closed:
                # Attempt one reconnect with cooldown.
                now = time.monotonic()
                if now - self._last_connect_attempt < _RECONNECT_COOLDOWN_S:
                    return {"ok": False, "error": {"code": "not_connected", "message": "Bridge not connected; reconnect cooldown active."}}
                try:
                    self._connect_locked()
                except BridgeError as exc:
                    return {"ok": False, "error": {"code": "connect_failed", "message": str(exc)}}

        command_id = secrets.token_hex(16)
        event = threading.Event()
        with self._lock:
            if len(self._pending) >= _MAX_PENDING:
                return {"ok": False, "error": {"code": "too_many_pending", "message": "Too many in-flight commands."}}
            self._pending[command_id] = event

        frame = self._build_command_frame(command_id, action, arguments or {}, tab_id, deadline_at, timeout)
        try:
            with self._lock:
                if not self._ws or self._ws.closed:
                    self._pending.pop(command_id, None)
                    return {"ok": False, "error": {"code": "not_connected", "message": "WebSocket closed before send."}}
                self._ws.send(json.dumps(frame))
        except Exception as exc:
            with self._lock:
                self._pending.pop(command_id, None)
            return {"ok": False, "error": {"code": "send_failed", "message": str(exc)}}

        fired = event.wait(timeout=timeout + 2.0)  # extra buffer for round-trip
        with self._lock:
            self._pending.pop(command_id, None)
            result = self._results.pop(command_id, None)

        if not fired or result is None:
            return {"ok": False, "error": {"code": "timeout", "message": f"Command {command_id} timed out after {timeout}s."}}
        return result

    def disconnect(self) -> None:
        with self._lock:
            self._connected = False
            if self._ws:
                self._ws.close()
                self._ws = None

    def is_connected(self) -> bool:
        with self._lock:
            return self._connected and bool(self._ws) and not self._ws.closed

    @property
    def controller_id(self) -> str:
        return self._controller_id

    @property
    def session_id(self) -> str:
        return self._session_id

    # ── Internal ────────────────────────────────────────────────────────────

    def _connect_locked(self) -> dict[str, Any]:
        """Must be called with self._lock held."""
        self._last_connect_attempt = time.monotonic()
        gateway_url = _loopback_only(self._cfg["gateway_url"])
        api_key = self._cfg["api_key"]

        if not _api_enabled(self._cfg):
            raise BridgeDisabledError("API_SERVER_ENABLED is false; browser control disabled.")

        # 1. Register: obtain a one-shot WS ticket.
        reg_url = f"{gateway_url}{_REGISTER_PATH}"
        if not self._controller_id:
            self._controller_id = f"hermes-companion-{secrets.token_hex(8)}"
        if not self._browser_profile_id:
            self._browser_profile_id = f"companion-profile-{secrets.token_hex(8)}"

        payload = {
            "controller_id": self._controller_id,
            "browser_profile_id": self._browser_profile_id,
            "session_id": self._session_id,
            "capabilities": [
                "controller.noop",
                "browser_back", "browser_click", "browser_drag", "browser_hover",
                "browser_fill", "browser_navigate", "browser_press", "browser_screenshot",
                "browser_scroll", "browser_scroll_to", "browser_select", "browser_snapshot",
                "browser_tab_activate", "browser_tab_close", "browser_tab_create",
                "browser_tab_group", "browser_tab_ungroup", "browser_tabs",
                "browser_type",
            ],
            "protocol_version": 1,
            "product": _PRODUCT,
        }
        headers = _auth_headers(api_key)
        try:
            reg_result = _http_post(reg_url, payload, headers)
        except BridgeError:
            raise
        ticket = str(reg_result.get("ticket") or reg_result.get("ws_ticket") or "")
        if not ticket:
            raise BridgeError(
                f"Gateway did not return a WebSocket ticket. Response keys: {list(reg_result.keys())}"
            )

        # 2. Open WS with the one-shot ticket as a subprotocol.
        ws_url = (
            gateway_url
            .replace("http://", "ws://")
            .replace("https://", "wss://")
        ) + _WS_PATH
        subprotocols = [
            "hermes-browser-control-v1",
            f"hermes-browser-control-ticket.{ticket}",
        ]
        ws_headers: dict[str, str] = {}  # ticket is in subprotocol, not header
        ws = _WebSocketAdapter(ws_url, ws_headers, subprotocols)
        self._ws = ws
        self._connected = True

        # 3. Start background reader thread.
        reader = threading.Thread(target=self._reader_loop, args=(ws,), daemon=True, name="hermes-bridge-reader")
        self._reader_thread = reader
        reader.start()

        return {
            "ok": True,
            "controller_id": self._controller_id,
            "browser_profile_id": self._browser_profile_id,
            "reused": False,
        }

    def _build_command_frame(
        self,
        command_id: str,
        action: str,
        arguments: dict[str, Any],
        tab_id: int | None,
        deadline_at: float | None,
        timeout: float,
    ) -> dict[str, Any]:
        now = time.time()
        frame: dict[str, Any] = {
            "method": "browser.controller.command",
            "params": {
                "command_id": command_id,
                "controller_id": self._controller_id,
                "session_id": self._session_id,
                "action": action,
                "arguments": arguments,
                "deadline_at": deadline_at if deadline_at is not None else now + timeout,
            },
        }
        if tab_id is not None:
            frame["params"]["tab_id"] = tab_id
        return frame

    def _reader_loop(self, ws: _WebSocketAdapter) -> None:
        """Background thread: receive frames and settle pending promises."""
        while True:
            try:
                raw = ws.recv(timeout=None)  # blocking; socket-level timeout handles stalls
            except Exception:
                break
            try:
                frame = json.loads(raw)
            except Exception:
                continue
            if not isinstance(frame, dict):
                continue
            method = frame.get("method") or ""
            params = frame.get("params") or {}
            if method == "browser.controller.result" or method == "result":
                command_id = str(params.get("command_id") or "")
                if command_id:
                    result: dict[str, Any]
                    if params.get("ok") is True:
                        result = {"ok": True, "result": params.get("result") or {}}
                    else:
                        result = {"ok": False, "error": params.get("error") or {"code": "unknown"}}
                    with self._lock:
                        self._results[command_id] = result
                        ev = self._pending.get(command_id)
                    if ev:
                        ev.set()
        # Reader exited — mark disconnected and unblock all pending waiters.
        with self._lock:
            self._connected = False
            pending_ids = list(self._pending.keys())
        for cid in pending_ids:
            with self._lock:
                if cid not in self._results:
                    self._results[cid] = {"ok": False, "error": {"code": "disconnected", "message": "WebSocket closed while waiting for result."}}
                ev = self._pending.get(cid)
            if ev:
                ev.set()


# ── Session-scoped singleton registry ─────────────────────────────────────────

_registry_lock = threading.Lock()
_bridges: dict[str, GatewayBrowserBridge] = {}


def get_bridge(session_id: str, cfg: dict[str, str] | None = None) -> GatewayBrowserBridge:
    """Return (creating if needed) the bridge for this Hermes session."""
    with _registry_lock:
        existing = _bridges.get(session_id)
        if existing:
            return existing
        bridge = GatewayBrowserBridge(session_id, cfg)
        _bridges[session_id] = bridge
        return bridge


def release_bridge(session_id: str) -> None:
    """Disconnect and remove the bridge for a session (e.g., on session end)."""
    with _registry_lock:
        bridge = _bridges.pop(session_id, None)
    if bridge:
        bridge.disconnect()
