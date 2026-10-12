"""Tests for companion-plugin gateway_bridge and browser_control_tools.

Suite: python -m unittest discover -s tests/pytools -v
     or: python -m unittest tests.pytools.test_browser_control_tools -v
"""

import json
import os
import sys
import threading
import time
import types
import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PLUGIN = REPO / "companion-plugin"

# ── Module loader ──────────────────────────────────────────────────────────────
# The companion-plugin directory name contains a hyphen, so Python cannot
# locate it as a regular package.  We use importlib to load each file by
# absolute path and register a synthetic parent package first so that
# relative imports inside submodules (e.g. `from .gateway_bridge import …`)
# resolve correctly.


def _ensure_parent_package() -> None:
    """Register a synthetic companion_plugin parent in sys.modules if needed."""
    if "companion_plugin" not in sys.modules:
        parent = types.ModuleType("companion_plugin")
        parent.__path__ = [str(PLUGIN)]        # type: ignore[assignment]
        parent.__package__ = "companion_plugin"
        parent.__spec__ = None                 # type: ignore[assignment]
        sys.modules["companion_plugin"] = parent


def _load_module(dotted_name: str, path: Path):
    """Load a single .py file as a named module, re-executing it fresh."""
    _ensure_parent_package()
    spec = spec_from_file_location(dotted_name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {dotted_name} from {path}")
    mod = module_from_spec(spec)
    mod.__package__ = dotted_name.rsplit(".", 1)[0] if "." in dotted_name else dotted_name
    sys.modules[dotted_name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# Load modules once at collection time.
_ensure_parent_package()
_policy = _load_module("companion_plugin.policy", PLUGIN / "policy.py")
_gateway_bridge = _load_module("companion_plugin.gateway_bridge", PLUGIN / "gateway_bridge.py")
# browser_control_tools has `from .gateway_bridge import …`; the parent and
# gateway_bridge must already be in sys.modules before exec_module runs.
_bct = _load_module("companion_plugin.browser_control_tools", PLUGIN / "browser_control_tools.py")
_schemas = _load_module("companion_plugin.browser_control_schemas", PLUGIN / "browser_control_schemas.py")


def _parse(json_str: str) -> dict:
    return json.loads(json_str)


# ── policy.py ──────────────────────────────────────────────────────────────────

class TestPolicy(unittest.TestCase):

    def _fresh_policy(self, env_override: dict | None = None) -> object:
        """Reload policy.py with a specific env state."""
        # Patch only HERMES_BROWSER_CONTROL; leave all other env vars intact.
        env_patch = {}
        if env_override and "HERMES_BROWSER_CONTROL" in env_override:
            env_patch["HERMES_BROWSER_CONTROL"] = env_override["HERMES_BROWSER_CONTROL"]
        with patch.dict(os.environ, env_patch, clear=False):
            if not env_override or "HERMES_BROWSER_CONTROL" not in env_override:
                os.environ.pop("HERMES_BROWSER_CONTROL", None)
            return _load_module("companion_plugin.policy", PLUGIN / "policy.py")

    def test_disabled_by_default(self):
        p = self._fresh_policy()
        self.assertFalse(p.BROWSER_CONTROL_ENABLED)
        self.assertFalse(p.browser_control_allowed())

    def test_enabled_via_true(self):
        with patch.dict(os.environ, {"HERMES_BROWSER_CONTROL": "true"}):
            p = _load_module("companion_plugin.policy", PLUGIN / "policy.py")
            self.assertTrue(p.browser_control_allowed())

    def test_truthy_values(self):
        for val in ("1", "yes", "on", "True", "YES"):
            with patch.dict(os.environ, {"HERMES_BROWSER_CONTROL": val}):
                p = _load_module("companion_plugin.policy", PLUGIN / "policy.py")
                self.assertTrue(p.browser_control_allowed(), f"Expected True for {val!r}")

    def test_falsy_values(self):
        for val in ("false", "0", "no", "off", "False"):
            p = self._fresh_policy({"HERMES_BROWSER_CONTROL": val})
            self.assertFalse(p.browser_control_allowed(), f"Expected False for {val!r}")

    def test_action_policy_disabled(self):
        p = self._fresh_policy()
        policy = p.action_policy()
        self.assertFalse(policy["browser_control"])
        self.assertTrue(policy["context_caching"])

    def test_action_policy_enabled(self):
        with patch.dict(os.environ, {"HERMES_BROWSER_CONTROL": "true"}):
            p = _load_module("companion_plugin.policy", PLUGIN / "policy.py")
            policy = p.action_policy()
        self.assertTrue(policy["browser_control"])
        self.assertTrue(policy["approval_required"])

    def test_browser_control_allowed_reads_live_env(self):
        """browser_control_allowed() must reflect changes after import."""
        p = self._fresh_policy()
        self.assertFalse(p.browser_control_allowed())
        with patch.dict(os.environ, {"HERMES_BROWSER_CONTROL": "true"}):
            self.assertTrue(p.browser_control_allowed())
        self.assertFalse(p.browser_control_allowed())


# ── gateway_bridge helpers ─────────────────────────────────────────────────────

class TestGatewayBridgeHelpers(unittest.TestCase):

    def test_loopback_only_accepts_127(self):
        self.assertEqual(
            _gateway_bridge._loopback_only("http://127.0.0.1:8642"),
            "http://127.0.0.1:8642",
        )

    def test_loopback_only_accepts_localhost(self):
        self.assertEqual(
            _gateway_bridge._loopback_only("http://localhost:8642"),
            "http://localhost:8642",
        )

    def test_loopback_only_rejects_public(self):
        with self.assertRaises(ValueError):
            _gateway_bridge._loopback_only("http://example.com:8642")

    def test_loopback_only_rejects_remote_ip(self):
        with self.assertRaises(ValueError):
            _gateway_bridge._loopback_only("http://192.168.1.1:8642")

    def test_api_enabled_true(self):
        self.assertTrue(_gateway_bridge._api_enabled({"enabled": "true"}))

    def test_api_enabled_false_strings(self):
        for val in ("false", "0", "no", "off"):
            self.assertFalse(_gateway_bridge._api_enabled({"enabled": val}))

    def test_read_env_config_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = _gateway_bridge._read_env_config()
        self.assertIn("127.0.0.1", cfg["gateway_url"])
        self.assertEqual(cfg["api_key"], "")
        self.assertEqual(cfg["enabled"], "true")

    def test_read_env_config_reads_env_vars(self):
        with patch.dict(os.environ, {
            "HERMES_GATEWAY_URL": "http://127.0.0.1:9999",
            "API_SERVER_KEY": "secret-key",
            "API_SERVER_ENABLED": "false",
        }):
            cfg = _gateway_bridge._read_env_config()
        self.assertEqual(cfg["gateway_url"], "http://127.0.0.1:9999")
        self.assertEqual(cfg["api_key"], "secret-key")
        self.assertEqual(cfg["enabled"], "false")


# ── GatewayBrowserBridge unit ─────────────────────────────────────────────────

class TestGatewayBrowserBridgeUnit(unittest.TestCase):

    def setUp(self):
        with _gateway_bridge._registry_lock:
            _gateway_bridge._bridges.clear()

    def _cfg(self, enabled="true"):
        return {"gateway_url": "http://127.0.0.1:8642", "api_key": "k", "enabled": enabled}

    def test_api_disabled_returns_error(self):
        bridge = _gateway_bridge.GatewayBrowserBridge("sess", self._cfg(enabled="false"))
        result = bridge.send_command("browser_tabs", {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "api_disabled")

    def test_is_connected_false_initially(self):
        bridge = _gateway_bridge.GatewayBrowserBridge("sess", self._cfg())
        self.assertFalse(bridge.is_connected())

    def test_session_id_stored(self):
        bridge = _gateway_bridge.GatewayBrowserBridge("my-session", self._cfg())
        self.assertEqual(bridge.session_id, "my-session")

    def test_controller_id_empty_before_connect(self):
        bridge = _gateway_bridge.GatewayBrowserBridge("sess", self._cfg())
        self.assertEqual(bridge.controller_id, "")

    def test_connect_returns_error_when_http_fails(self):
        bridge = _gateway_bridge.GatewayBrowserBridge("sess-fail", self._cfg())
        with patch.object(_gateway_bridge, "_http_post",
                          side_effect=_gateway_bridge.BridgeError("refused")):
            # connect() re-raises BridgeError from _connect_locked — that is the
            # correct fail-closed behaviour; callers (e.g. _ensure_connected) catch it.
            with self.assertRaises(_gateway_bridge.BridgeError):
                bridge.connect()

    def test_connect_idempotent_when_already_connected(self):
        bridge = _gateway_bridge.GatewayBrowserBridge("sess-idem", self._cfg())
        bridge._connected = True
        ws = MagicMock()
        ws.closed = False
        bridge._ws = ws
        result = bridge.connect()
        self.assertTrue(result.get("ok"))
        self.assertTrue(result.get("reused"))

    def test_disconnect_clears_state(self):
        bridge = _gateway_bridge.GatewayBrowserBridge("sess-dc", self._cfg())
        ws = MagicMock()
        ws.closed = False
        bridge._ws = ws
        bridge._connected = True
        bridge.disconnect()
        self.assertFalse(bridge._connected)
        self.assertIsNone(bridge._ws)
        ws.close.assert_called_once()

    def test_cooldown_prevents_rapid_reconnect(self):
        bridge = _gateway_bridge.GatewayBrowserBridge("sess-cd", self._cfg())
        bridge._last_connect_attempt = time.monotonic()  # just now
        result = bridge.send_command("browser_tabs", {}, timeout=0.1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "not_connected")


# ── Bridge registry ────────────────────────────────────────────────────────────

class TestBridgeRegistry(unittest.TestCase):

    def setUp(self):
        with _gateway_bridge._registry_lock:
            _gateway_bridge._bridges.clear()

    def test_same_session_returns_same_instance(self):
        b1 = _gateway_bridge.get_bridge("sess-same")
        b2 = _gateway_bridge.get_bridge("sess-same")
        self.assertIs(b1, b2)

    def test_different_sessions_different_instances(self):
        b1 = _gateway_bridge.get_bridge("sess-a")
        b2 = _gateway_bridge.get_bridge("sess-b")
        self.assertIsNot(b1, b2)

    def test_release_disconnects_and_removes(self):
        bridge = _gateway_bridge.get_bridge("sess-rel")
        ws = MagicMock(); ws.closed = False
        bridge._ws = ws; bridge._connected = True
        _gateway_bridge.release_bridge("sess-rel")
        ws.close.assert_called_once()
        fresh = _gateway_bridge.get_bridge("sess-rel")
        self.assertIsNot(bridge, fresh)

    def test_release_nonexistent_is_noop(self):
        _gateway_bridge.release_bridge("nope-nope")  # must not raise


# ── Recovery: reader loop settling pending commands ────────────────────────────

class TestReaderLoopRecovery(unittest.TestCase):

    def setUp(self):
        with _gateway_bridge._registry_lock:
            _gateway_bridge._bridges.clear()

    def test_reader_settles_pending_on_ws_close(self):
        bridge = _gateway_bridge.GatewayBrowserBridge("sess-reader", {
            "gateway_url": "http://127.0.0.1:8642", "api_key": "k", "enabled": "true",
        })
        event = threading.Event()
        bridge._pending["cmd-001"] = event
        bridge._connected = True

        class _FakeWs:
            def recv(self, timeout=None): raise ConnectionError("closed")
            def close(self): pass
            @property
            def closed(self): return True

        bridge._reader_loop(_FakeWs())

        self.assertTrue(event.is_set())
        result = bridge._results.get("cmd-001")
        self.assertIsNotNone(result)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "disconnected")


# ── browser_control_tools — mock get_bridge ───────────────────────────────────

def _stub_bridge(commands):
    """Return a mock bridge whose send_command returns successive values."""
    it = iter(commands)
    bridge = MagicMock()
    bridge.is_connected.return_value = False
    bridge.connect.return_value = {"ok": True, "reused": False}
    bridge.controller_id = "ctrl-test"
    bridge.send_command.side_effect = lambda *a, **kw: next(it)
    return bridge


class TestBrowserListTabs(unittest.TestCase):

    def _call(self, commands, args=None):
        bridge = _stub_bridge(commands)
        with patch.object(_bct, "get_bridge", return_value=bridge):
            return _parse(_bct.browser_list_tabs(args, session_id="s1"))

    def test_returns_tabs(self):
        tabs = [
            {"id": 1, "title": "YouTube", "url": "https://yt.com", "active": True},
            {"id": 2, "title": "GH", "url": "https://gh.com", "active": False},
        ]
        r = self._call([{"ok": True, "result": {"tabs": tabs}}])
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"]["tab_count"], 2)
        self.assertEqual(r["data"]["tabs"][0]["title"], "YouTube")

    def test_filters_zero_id(self):
        tabs = [{"id": 0, "title": "bad"}, {"id": 5, "title": "ok", "url": "u", "active": False}]
        r = self._call([{"ok": True, "result": {"tabs": tabs}}])
        self.assertEqual(r["data"]["tab_count"], 1)

    def test_bridge_error_propagates(self):
        r = self._call([{"ok": False, "error": {"code": "bridge_error", "message": "down"}}])
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "bridge_error")


class TestBrowserOpenTab(unittest.TestCase):

    def _call(self, commands, args):
        bridge = _stub_bridge(commands)
        with patch.object(_bct, "get_bridge", return_value=bridge), \
             patch.object(_bct.time, "sleep"):
            return _parse(_bct.browser_open_tab(args, session_id="s1"))

    def test_opens_new_tab_returns_tab_id(self):
        r = self._call(
            [{"ok": True, "result": {"tab": {"id": 9, "windowId": 1, "active": True, "url": "https://yt.com"}}}],
            {"url": "https://yt.com"},
        )
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"]["tab_id"], 9)
        self.assertEqual(r["data"]["action"], "opened")
        # New tab: verify note explains snapshot is deferred
        self.assertIn("note", r["data"]["verify"])

    def test_missing_url_returns_error(self):
        r = _parse(_bct.browser_open_tab({}, session_id="s1"))
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "missing_url")

    def test_tab_create_failure_propagates(self):
        r = self._call(
            [{"ok": False, "error": {"code": "tab_create_failed", "message": "no window"}}],
            {"url": "https://yt.com"},
        )
        self.assertFalse(r["ok"])

    def test_navigate_mode_verifies_via_snapshot(self):
        r = self._call(
            [
                {"ok": True, "result": {"status": "navigated"}},
                {"ok": True, "result": {"title": "T", "url": "https://x.com", "text": "", "nodes": []}},
            ],
            {"url": "https://x.com", "new_tab": False},
        )
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"]["action"], "navigated")
        self.assertEqual(r["data"]["verify"]["title"], "T")


class TestBrowserNavigate(unittest.TestCase):

    def _call(self, commands, args):
        bridge = _stub_bridge(commands)
        with patch.object(_bct, "get_bridge", return_value=bridge), \
             patch.object(_bct.time, "sleep"):
            return _parse(_bct.browser_navigate(args, session_id="s1"))

    def test_navigate_and_verify(self):
        r = self._call(
            [
                {"ok": True, "result": {"status": "navigated"}},
                {"ok": True, "result": {"title": "Eg", "url": "https://eg.com", "text": "t", "nodes": []}},
            ],
            {"url": "https://eg.com"},
        )
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"]["verify"]["title"], "Eg")

    def test_missing_url(self):
        r = _parse(_bct.browser_navigate({}, session_id="s1"))
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "missing_url")


class TestBrowserClick(unittest.TestCase):

    def _call(self, commands, args):
        bridge = _stub_bridge(commands)
        with patch.object(_bct, "get_bridge", return_value=bridge):
            return _parse(_bct.browser_click(args, session_id="s1"))

    def test_click_by_ref(self):
        r = self._call(
            [
                {"ok": True, "result": {"status": "clicked"}},
                {"ok": True, "result": {"title": "T", "url": "u", "text": "", "nodes": []}},
            ],
            {"ref": "44"},
        )
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"]["target"], "44")

    def test_click_by_selector(self):
        r = self._call(
            [
                {"ok": True, "result": {"status": "clicked"}},
                {"ok": True, "result": {"title": "T", "url": "u", "text": "", "nodes": []}},
            ],
            {"selector": "#submit"},
        )
        self.assertTrue(r["ok"])

    def test_click_by_coordinates(self):
        r = self._call(
            [
                {"ok": True, "result": {"status": "clicked"}},
                {"ok": True, "result": {"title": "T", "url": "u", "text": "", "nodes": []}},
            ],
            {"x": 100.0, "y": 200.0},
        )
        self.assertTrue(r["ok"])

    def test_missing_target_returns_error(self):
        r = _parse(_bct.browser_click({}, session_id="s1"))
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "missing_target")

    def test_invalid_coordinates_returns_error(self):
        r = _parse(_bct.browser_click({"x": "bad", "y": 5}, session_id="s1"))
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "invalid_coordinates")

    def test_verify_false_skips_snapshot(self):
        seen_actions = []
        bridge = MagicMock()
        bridge.is_connected.return_value = False
        bridge.connect.return_value = {"ok": True}
        bridge.send_command.side_effect = lambda action, **kw: (
            seen_actions.append(action) or {"ok": True, "result": {"status": "clicked"}}
        )
        with patch.object(_bct, "get_bridge", return_value=bridge):
            r = _parse(_bct.browser_click({"ref": "44", "verify_change": False}, session_id="s1"))
        self.assertTrue(r["ok"])
        self.assertNotIn("browser_snapshot", seen_actions)

    def test_click_failure_propagates(self):
        r = self._call(
            [{"ok": False, "error": {"code": "scope_mismatch", "message": "stale"}}],
            {"ref": "44"},
        )
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "scope_mismatch")


class TestBrowserTypeText(unittest.TestCase):

    def _call(self, commands, args):
        bridge = _stub_bridge(commands)
        with patch.object(_bct, "get_bridge", return_value=bridge):
            return _parse(_bct.browser_type_text(args, session_id="s1"))

    def test_type_text(self):
        r = self._call(
            [{"ok": True, "result": {"status": "typed"}}],
            {"text": "Hello"},
        )
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"]["chars"], 5)

    def test_missing_text_returns_error(self):
        r = _parse(_bct.browser_type_text({"ref": "x"}, session_id="s1"))
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "missing_text")

    def test_text_too_long_returns_error(self):
        r = _parse(_bct.browser_type_text({"text": "x" * 60_000}, session_id="s1"))
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "text_too_long")


class TestBrowserPressKey(unittest.TestCase):

    def _call(self, args):
        bridge = _stub_bridge([{"ok": True, "result": {"status": "pressed"}}])
        with patch.object(_bct, "get_bridge", return_value=bridge):
            return _parse(_bct.browser_press_key(args, session_id="s1"))

    def test_press_tab(self):
        r = self._call({"key": "Tab"})
        self.assertTrue(r["ok"])

    def test_enter_without_confirmed_blocked(self):
        r = self._call({"key": "Enter"})
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "confirmation_required")

    def test_return_without_confirmed_blocked(self):
        r = self._call({"key": "Return"})
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "confirmation_required")

    def test_enter_with_confirmed_true_succeeds(self):
        r = self._call({"key": "Enter", "confirmed": True})
        self.assertTrue(r["ok"])

    def test_missing_key_returns_error(self):
        r = _parse(_bct.browser_press_key({}, session_id="s1"))
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "missing_key")


class TestBrowserScroll(unittest.TestCase):

    def _call(self, args):
        bridge = _stub_bridge([{"ok": True, "result": {"status": "scrolled"}}])
        with patch.object(_bct, "get_bridge", return_value=bridge):
            return _parse(_bct.browser_scroll(args, session_id="s1"))

    def test_scroll_down(self):
        r = self._call({"direction": "down"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"]["delta_px"], 600)

    def test_scroll_small(self):
        r = self._call({"direction": "up", "amount": "small"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"]["delta_px"], 200)

    def test_invalid_direction(self):
        r = _parse(_bct.browser_scroll({"direction": "sideways"}, session_id="s1"))
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "invalid_direction")


class TestBrowserReadPage(unittest.TestCase):

    def test_returns_text_and_refs(self):
        snapshot = {
            "ok": True,
            "result": {
                "title": "YouTube",
                "url": "https://yt.com",
                "text": "Hello world",
                "nodes": [
                    {"backendDOMNodeId": 44, "role": "button", "name": "Play"},
                    {"backendDOMNodeId": 45, "role": "textbox", "name": "Search"},
                ],
            },
        }
        bridge = _stub_bridge([snapshot])
        with patch.object(_bct, "get_bridge", return_value=bridge):
            r = _parse(_bct.browser_read_page(None, session_id="s1"))
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"]["ref_count"], 2)
        self.assertEqual(r["data"]["refs"][0]["role"], "button")

    def test_snapshot_failure_propagates(self):
        bridge = _stub_bridge([{"ok": False, "error": {"code": "not_connected"}}])
        with patch.object(_bct, "get_bridge", return_value=bridge):
            r = _parse(_bct.browser_read_page(None, session_id="s1"))
        self.assertFalse(r["ok"])


class TestBrowserScreenshot(unittest.TestCase):

    def test_returns_data_url(self):
        bridge = _stub_bridge([{"ok": True, "result": {"dataUrl": "data:image/png;base64,abc"}}])
        with patch.object(_bct, "get_bridge", return_value=bridge):
            r = _parse(_bct.browser_screenshot(None, session_id="s1"))
        self.assertTrue(r["ok"])
        self.assertTrue(r["data"]["data_url"].startswith("data:image/"))

    def test_empty_data_url_returns_error(self):
        bridge = _stub_bridge([{"ok": True, "result": {"dataUrl": ""}}])
        with patch.object(_bct, "get_bridge", return_value=bridge):
            r = _parse(_bct.browser_screenshot(None, session_id="s1"))
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "screenshot_failed")


class TestBrowserMediaControl(unittest.TestCase):
    """Media control now uses keyboard shortcuts only — no browser_evaluate."""

    def _call(self, args):
        bridge = _stub_bridge([{"ok": True, "result": {"status": "pressed"}}])
        with patch.object(_bct, "get_bridge", return_value=bridge):
            return _parse(_bct.browser_media_control(args, session_id="s1"))

    def test_play_uses_keyboard_shortcut(self):
        r = self._call({"action": "play"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"]["method"], "keyboard_shortcut")
        self.assertEqual(r["data"]["key"], "k")

    def test_pause_uses_keyboard_shortcut(self):
        r = self._call({"action": "pause"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"]["key"], "k")

    def test_mute_uses_m_key(self):
        r = self._call({"action": "mute"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"]["key"], "m")

    def test_unmute_uses_m_key(self):
        r = self._call({"action": "unmute"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"]["key"], "m")

    def test_toggle_uses_k_key(self):
        r = self._call({"action": "toggle"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"]["key"], "k")

    def test_set_volume_returns_not_supported(self):
        r = _parse(_bct.browser_media_control({"action": "set_volume", "volume": 0.5}, session_id="s1"))
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "not_supported")

    def test_set_volume_invalid_number_returns_error(self):
        r = _parse(_bct.browser_media_control({"action": "set_volume", "volume": "loud"}, session_id="s1"))
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "invalid_volume")

    def test_invalid_action_returns_error(self):
        r = _parse(_bct.browser_media_control({"action": "rewind"}, session_id="s1"))
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "invalid_action")

    def test_press_failure_propagates(self):
        bridge = _stub_bridge([{"ok": False, "error": {"code": "scope_mismatch"}}])
        with patch.object(_bct, "get_bridge", return_value=bridge):
            r = _parse(_bct.browser_media_control({"action": "play"}, session_id="s1"))
        self.assertFalse(r["ok"])


class TestBrowserControllerStatus(unittest.TestCase):

    def test_returns_connected_status(self):
        mock_bridge = MagicMock()
        mock_bridge.is_connected.return_value = True
        mock_bridge.controller_id = "ctrl-abc"
        with patch.object(_bct, "get_bridge", return_value=mock_bridge):
            r = _parse(_bct.browser_controller_status(None, session_id="s1"))
        self.assertTrue(r["ok"])
        self.assertTrue(r["data"]["bridge_connected"])
        self.assertEqual(r["data"]["controller_id"], "ctrl-abc")

    def test_attempts_connect_when_not_connected(self):
        mock_bridge = MagicMock()
        mock_bridge.is_connected.return_value = False
        mock_bridge.connect.return_value = {"ok": True, "reused": False}
        mock_bridge.controller_id = "ctrl-new"
        with patch.object(_bct, "get_bridge", return_value=mock_bridge):
            r = _parse(_bct.browser_controller_status(None, session_id="s1"))
        self.assertTrue(r["ok"])
        mock_bridge.connect.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
