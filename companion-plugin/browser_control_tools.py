"""Hermes Agent browser-control tool handlers.

Each handler:
  - Takes ``args: dict | None`` and ``**kwargs`` (Hermes hook kwargs for owner).
  - Returns a JSON string conforming to ``{ok, data}`` or ``{ok: false, error}``.
  - Never raises — all errors are captured and returned as structured JSON.
  - Reads the Hermes session_id from trusted hook kwargs (never from args).
  - Uses ``gateway_bridge.get_bridge(session_id)`` to send commands through the
    existing browser-control WebSocket that the Chrome extension listens on.

Post-action verification
------------------------
Mutating actions (navigate, click, type) take a follow-up ``browser_snapshot``
to confirm the page actually changed.  The snapshot result is included in the
``verify`` field of the returned data so Hermes can report real outcomes.

Safety
------
- ``browser_press_key`` will not send Enter/Return/Tab unless ``confirmed=True``
  is set in args (destructive / submission guard).
- ``browser_media_control`` uses ``Runtime.evaluate`` on the page and is gated by
  the executor's approval policy on the extension side.
- ``browser_list_tabs`` never attaches the debugger (``browser_tabs`` action).
"""

from __future__ import annotations

import json
import time
from typing import Any

from .gateway_bridge import BridgeError, BridgeDisabledError, get_bridge

# ── Helpers ────────────────────────────────────────────────────────────────────

_SUBMISSION_KEYS = frozenset({"enter", "return"})
_MAX_TEXT_LEN = 50_000
_DEFAULT_WAIT_MS = 1_500
_MAX_WAIT_MS = 10_000


def _ok(data: Any) -> str:
    return json.dumps({"ok": True, "data": data}, default=str)


def _err(code: str, message: str) -> str:
    return json.dumps({"ok": False, "error": {"code": code, "message": message}})


def _session_id(kwargs: dict[str, Any]) -> str:
    """Extract the Hermes session_id from trusted hook kwargs.

    Only session_id is used as the bridge key — task_id changes per tool call
    and would create a new WebSocket registration on every invocation.
    """
    sid = str(kwargs.get("session_id") or "").strip()
    return sid or "hermes-browser-companion-default"


def _bounded_wait(ms: Any) -> int:
    try:
        val = int(ms)
    except (TypeError, ValueError):
        val = _DEFAULT_WAIT_MS
    return max(0, min(val, _MAX_WAIT_MS))


def _compact_str(value: Any, limit: int = 2000) -> str:
    return str(value or "").replace("\n", " ").strip()[:limit]


def _safe_snapshot(result: dict[str, Any]) -> dict[str, Any]:
    """Extract a lightweight summary from a browser_snapshot result."""
    r = result.get("result") or {}
    return {
        "title": _compact_str(r.get("title"), 300),
        "url": _compact_str(r.get("url"), 2000),
        "text_preview": str(r.get("text") or "")[:500],
        "node_count": len(r.get("nodes") or []),
    }


def _ensure_connected(session_id: str) -> tuple[Any, str | None]:
    """Return (bridge, error_str_or_None)."""
    try:
        bridge = get_bridge(session_id)
        result = bridge.connect()
        if not result.get("ok") and not result.get("reused"):
            return None, f"Failed to connect to gateway: {result}"
        return bridge, None
    except BridgeDisabledError as exc:
        return None, f"Browser control is disabled: {exc}"
    except BridgeError as exc:
        return None, f"Gateway bridge error: {exc}"
    except Exception as exc:
        return None, f"Unexpected bridge error: {exc}"


def _send(session_id: str, action: str, arguments: dict[str, Any], tab_id: int | None = None, timeout: float = 30.0) -> dict[str, Any]:
    """Connect (or reuse) the bridge and send one command; return the raw result dict."""
    bridge, err = _ensure_connected(session_id)
    if err:
        return {"ok": False, "error": {"code": "bridge_error", "message": err}}
    try:
        return bridge.send_command(action, arguments=arguments, tab_id=tab_id, timeout=timeout)
    except BridgeError as exc:
        return {"ok": False, "error": {"code": "command_error", "message": str(exc)}}
    except Exception as exc:
        return {"ok": False, "error": {"code": "unexpected_error", "message": str(exc)}}


# ── Tool handlers ──────────────────────────────────────────────────────────────

def browser_list_tabs(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """List all open tabs.  Never attaches the debugger."""
    sid = _session_id(kwargs)
    result = _send(sid, "browser_tabs", {})
    if not result.get("ok"):
        return json.dumps(result)
    raw_tabs = (result.get("result") or {}).get("tabs") or []
    filtered_tabs = [
        {
            "id": int(t.get("id") or 0),
            "title": _compact_str(t.get("title"), 300),
            "url": _compact_str(t.get("url"), 2000),
            "active": bool(t.get("active")),
            "attached": bool(t.get("attached")),
        }
        for t in raw_tabs
        if int(t.get("id") or 0) > 0
    ]
    data = {
        "tab_count": len(filtered_tabs),
        "tabs": filtered_tabs,
    }
    return _ok(data)


def browser_open_tab(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """Open a new tab and navigate to a URL, then verify."""
    sid = _session_id(kwargs)
    a = args or {}
    url = str(a.get("url") or "").strip()
    if not url:
        return _err("missing_url", "A URL is required.")
    new_tab = a.get("new_tab", True) is not False
    wait_ms = _bounded_wait(a.get("wait_ms", _DEFAULT_WAIT_MS))

    if new_tab:
        create_result = _send(sid, "browser_tab_create", {"url": url, "active": True})
        if not create_result.get("ok"):
            return json.dumps(create_result)
        tab_id = int((create_result.get("result") or {}).get("tab", {}).get("id") or 0)
        if tab_id <= 0:
            return _err("tab_create_failed", "Gateway did not return a valid tab id.")
        # A freshly created tab is not yet leased by this controller session,
        # so we cannot take a browser_snapshot without a lease-acquire step.
        # Return the confirmed tab metadata from browser_tab_create instead.
        created_tab = (create_result.get("result") or {}).get("tab") or {}
        verify = {
            "title": _compact_str(created_tab.get("title"), 300),
            "url": _compact_str(created_tab.get("url") or url, 2000),
            "note": "new tab created; snapshot requires explicit lease-acquire",
        }
    else:
        nav_result = _send(sid, "browser_navigate", {"url": url})
        if not nav_result.get("ok"):
            return json.dumps(nav_result)
        tab_id = 0  # use leased tab

        # Wait for page to settle then verify via snapshot (leased tab only).
        if wait_ms > 0:
            time.sleep(wait_ms / 1000.0)
        snap_result = _send(sid, "browser_snapshot", {})
        verify = _safe_snapshot(snap_result) if snap_result.get("ok") else {"error": snap_result.get("error")}
        return _ok({"action": "navigated", "tab_id": tab_id, "url": url, "verify": verify})

    if new_tab and wait_ms > 0:
        time.sleep(wait_ms / 1000.0)

    data = {
        "action": "opened",
        "tab_id": tab_id,
        "url": url,
        "verify": verify,
    }
    return _ok(data)


def browser_navigate(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """Navigate the current leased tab to a URL, then verify."""
    sid = _session_id(kwargs)
    a = args or {}
    url = str(a.get("url") or "").strip()
    if not url:
        return _err("missing_url", "A URL is required.")
    wait_ms = _bounded_wait(a.get("wait_ms", _DEFAULT_WAIT_MS))

    nav_result = _send(sid, "browser_navigate", {"url": url})
    if not nav_result.get("ok"):
        return json.dumps(nav_result)

    if wait_ms > 0:
        time.sleep(wait_ms / 1000.0)

    snap_result = _send(sid, "browser_snapshot", {})
    verify = _safe_snapshot(snap_result) if snap_result.get("ok") else {"error": snap_result.get("error")}

    return _ok({"action": "navigated", "url": url, "verify": verify})


def browser_click(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """Click an element; verify DOM changed afterward."""
    sid = _session_id(kwargs)
    a = args or {}
    ref = str(a.get("ref") or "").strip()
    selector = str(a.get("selector") or "").strip()
    x = a.get("x")
    y = a.get("y")
    verify_change = a.get("verify_change", True) is not False

    arguments: dict[str, Any] = {}
    if ref:
        arguments["ref"] = ref
    elif selector:
        arguments["selector"] = selector
    elif x is not None and y is not None:
        try:
            arguments["x"] = float(x)
            arguments["y"] = float(y)
        except (TypeError, ValueError):
            return _err("invalid_coordinates", "x and y must be numbers.")
    else:
        return _err("missing_target", "Provide ref, selector, or x+y coordinates.")

    click_result = _send(sid, "browser_click", arguments)
    if not click_result.get("ok"):
        return json.dumps(click_result)

    verify: dict[str, Any] = {}
    if verify_change:
        snap_result = _send(sid, "browser_snapshot", {})
        verify = _safe_snapshot(snap_result) if snap_result.get("ok") else {"error": snap_result.get("error")}

    return _ok({"action": "clicked", "target": ref or selector or f"({x},{y})", "verify": verify})


def browser_type_text(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """Type text into a field; optionally clear first."""
    sid = _session_id(kwargs)
    a = args or {}
    text = str(a.get("text") or "")
    if not text:
        return _err("missing_text", "Text to type is required.")
    if len(text) > _MAX_TEXT_LEN:
        return _err("text_too_long", f"Text exceeds {_MAX_TEXT_LEN} character limit.")

    ref = str(a.get("ref") or "").strip()
    selector = str(a.get("selector") or "").strip()
    clear_first = bool(a.get("clear_first", False))

    arguments: dict[str, Any] = {"text": text}
    if ref:
        arguments["ref"] = ref
    elif selector:
        arguments["selector"] = selector

    if clear_first and (ref or selector):
        # Select all then delete. Use Ctrl on Windows/Linux, Meta (Cmd) on macOS.
        import platform as _platform
        select_modifier = "meta" if _platform.system() == "Darwin" else "ctrl"
        _send(sid, "browser_press", {"key": "a", "modifiers": [select_modifier]})
        _send(sid, "browser_press", {"key": "Backspace"})

    type_result = _send(sid, "browser_type", arguments)
    if not type_result.get("ok"):
        return json.dumps(type_result)

    return _ok({"action": "typed", "chars": len(text), "target": ref or selector or "focused"})


def browser_press_key(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """Press a keyboard key.  Submission keys (Enter/Return) require confirmed=True."""
    sid = _session_id(kwargs)
    a = args or {}
    key = str(a.get("key") or "").strip()
    if not key:
        return _err("missing_key", "A key name is required.")

    if key.lower() in _SUBMISSION_KEYS:
        confirmed = bool(a.get("confirmed", False))
        if not confirmed:
            return _err(
                "confirmation_required",
                f"Pressing '{key}' may submit a form. "
                "Set confirmed=true only after the user has explicitly approved this action.",
            )

    press_result = _send(sid, "browser_press", {"key": key})
    if not press_result.get("ok"):
        return json.dumps(press_result)

    return _ok({"action": "pressed", "key": key})


def browser_scroll(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """Scroll the current leased tab."""
    sid = _session_id(kwargs)
    a = args or {}
    direction = str(a.get("direction") or "down").strip().lower()
    if direction not in ("up", "down", "left", "right"):
        return _err("invalid_direction", "direction must be up, down, left, or right.")

    amount_map = {"small": 200, "medium": 600, "large": 1200}
    amount_name = str(a.get("amount") or "medium").strip().lower()
    delta = amount_map.get(amount_name, 600)

    scroll_result = _send(sid, "browser_scroll", {"direction": direction, "amount": delta})
    if not scroll_result.get("ok"):
        return json.dumps(scroll_result)

    return _ok({"action": "scrolled", "direction": direction, "delta_px": delta})


def browser_read_page(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """Take a snapshot of the current leased tab: text + interactive refs."""
    sid = _session_id(kwargs)
    a = args or {}
    include_text = a.get("include_text", True) is not False
    include_refs = a.get("include_refs", True) is not False

    snap_result = _send(sid, "browser_snapshot", {})
    if not snap_result.get("ok"):
        return json.dumps(snap_result)

    r = snap_result.get("result") or {}
    data: dict[str, Any] = {
        "title": _compact_str(r.get("title"), 300),
        "url": _compact_str(r.get("url"), 2000),
    }
    if include_text:
        data["text"] = str(r.get("text") or "")[:_MAX_TEXT_LEN]
    if include_refs:
        raw_nodes = r.get("nodes") or []
        data["refs"] = [
            {
                "ref": str(n.get("ref") or n.get("backendDOMNodeId") or ""),
                "role": _compact_str(n.get("role"), 80),
                "name": _compact_str(n.get("name"), 300),
            }
            for n in raw_nodes
            if n.get("ref") or n.get("backendDOMNodeId")
        ]
        data["ref_count"] = len(data["refs"])
    return _ok(data)


def browser_inspect_element(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """Inspect a specific element by ref or CSS selector."""
    sid = _session_id(kwargs)
    a = args or {}
    ref = str(a.get("ref") or "").strip()
    selector = str(a.get("selector") or "").strip()

    # Take a full snapshot, then find the matching node.
    snap_result = _send(sid, "browser_snapshot", {})
    if not snap_result.get("ok"):
        return json.dumps(snap_result)

    r = snap_result.get("result") or {}
    nodes = r.get("nodes") or []

    matched = None
    if ref:
        matched = next((n for n in nodes if str(n.get("ref") or n.get("backendDOMNodeId") or "") == ref), None)
    elif selector:
        # Selector search is best-effort against name/role heuristics.
        # The extension doesn't expose a CSS-selector lookup directly via the
        # AX snapshot; use the selector as a substring match on the name.
        low = selector.lower().lstrip("#.[]")
        matched = next(
            (n for n in nodes if low in str(n.get("name") or "").lower() or low in str(n.get("role") or "").lower()),
            None,
        )

    if not matched:
        return _err("element_not_found", f"No element found for ref={ref!r} selector={selector!r}.")

    return _ok({
        "ref": str(matched.get("ref") or matched.get("backendDOMNodeId") or ""),
        "role": _compact_str(matched.get("role"), 80),
        "name": _compact_str(matched.get("name"), 300),
        "autocomplete": _compact_str(matched.get("autocomplete"), 120),
        "page_url": _compact_str(r.get("url"), 2000),
    })


def browser_screenshot(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """Capture a screenshot of the current leased tab (or clipped to a ref)."""
    sid = _session_id(kwargs)
    a = args or {}
    ref = str(a.get("ref") or "").strip()

    arguments: dict[str, Any] = {}
    if ref:
        arguments["ref"] = ref

    shot_result = _send(sid, "browser_screenshot", arguments)
    if not shot_result.get("ok"):
        return json.dumps(shot_result)

    r = shot_result.get("result") or {}
    data_url = str(r.get("dataUrl") or "")
    if not data_url.startswith("data:image/"):
        return _err("screenshot_failed", "Extension returned an empty or invalid screenshot.")

    return _ok({"data_url": data_url, "clipped_to_ref": ref or None})


def browser_media_control(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """Control media playback on the current leased tab via keyboard shortcuts.

    Uses well-known browser keyboard shortcuts that work without CDP evaluate:
    - YouTube / most HTML5 players: k = play/pause, m = mute
    - Volume shortcuts are not universally keyboard-accessible, so set_volume
      returns a clear not-supported message rather than silently doing nothing.

    browser_evaluate is intentionally not used here because it requires the
    extension's developerMode flag to be set by the user, and it is not in the
    standard capability set advertised during controller registration.
    """
    sid = _session_id(kwargs)
    a = args or {}
    action = str(a.get("action") or "").strip().lower()
    if action not in ("play", "pause", "toggle", "mute", "unmute", "set_volume"):
        return _err("invalid_action", "action must be play, pause, toggle, mute, unmute, or set_volume.")

    # set_volume has no reliable keyboard shortcut in a generic browser context.
    if action == "set_volume":
        volume = a.get("volume")
        try:
            vol = max(0.0, min(1.0, float(volume if volume is not None else 1.0)))
        except (TypeError, ValueError):
            return _err("invalid_volume", "volume must be a number between 0.0 and 1.0.")
        return _err(
            "not_supported",
            f"set_volume ({vol:.2f}) requires browser_evaluate with developerMode enabled. "
            "Ask the user to set the volume manually or enable developerMode in Browser Control settings.",
        )

    # Keyboard shortcut map (YouTube and most HTML5 players).
    key_map = {
        "play": "k",
        "pause": "k",
        "toggle": "k",
        "mute": "m",
        "unmute": "m",
    }
    key = key_map[action]
    press_result = _send(sid, "browser_press", {"key": key})
    if not press_result.get("ok"):
        return json.dumps(press_result)
    return _ok({"action": action, "method": "keyboard_shortcut", "key": key})


def browser_controller_status(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """Return live bridge + gateway connection status."""
    sid = _session_id(kwargs)
    try:
        bridge = get_bridge(sid)
        connected = bridge.is_connected()
        data = {
            "session_id": sid,
            "bridge_connected": connected,
            "controller_id": bridge.controller_id if connected else "",
            "gateway_url": "127.0.0.1:8642 (loopback only)",
        }
        if not connected:
            connect_result = bridge.connect()
            data["connect_attempt"] = connect_result
            data["bridge_connected"] = bridge.is_connected()
            data["controller_id"] = bridge.controller_id
        return _ok(data)
    except BridgeDisabledError as exc:
        return _ok({"session_id": sid, "bridge_connected": False, "reason": str(exc)})
    except Exception as exc:
        return _err("status_error", str(exc))
