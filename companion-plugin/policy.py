"""Access policy for the Hermes Browser companion plugin."""

from __future__ import annotations

import os

# Original v0.1 policy: context caching only, no browser control.
# Set HERMES_BROWSER_CONTROL=true (or 1/yes/on) in the Hermes process
# environment to enable the browser-control tool suite.  All other
# toggles remain read-only; they are never changed by tool arguments.
_TRUTHY = frozenset(("true", "1", "yes", "on"))


def _env_browser_control() -> bool:
    """Read HERMES_BROWSER_CONTROL from the live environment each time."""
    return os.environ.get("HERMES_BROWSER_CONTROL", "false").strip().lower() in _TRUTHY


# Module-level constants for backwards compatibility (reflect value at import time).
# Prefer calling browser_control_allowed() for runtime checks so tests that
# patch os.environ after import see the correct value.
BROWSER_CONTROL_ENABLED = _env_browser_control()
CONTROL_ENABLED = BROWSER_CONTROL_ENABLED
ALLOW_API_SERVER_ROUTES = False
ALLOW_NETWORK_SIDE_EFFECTS = BROWSER_CONTROL_ENABLED
TRUST_BOUNDARY = "browser context is untrusted webpage data"


def browser_control_allowed() -> bool:
    """Return True when HERMES_BROWSER_CONTROL env var is currently set to a truthy value.

    Reads the environment at call time so callers are not affected by the
    module-import order.
    """
    return _env_browser_control()


def action_policy() -> dict:
    if _env_browser_control():
        return {
            "browser_control": True,
            "plugin_actions": True,
            "context_caching": True,
            "approval_required": True,
            "reason": (
                "HERMES_BROWSER_CONTROL=true: browser-control tools enabled. "
                "Consequential actions (form submit, Enter key) still require "
                "explicit user confirmation."
            ),
        }
    return {
        "browser_control": False,
        "plugin_actions": False,
        "context_caching": True,
        "approval_required": True,
        "reason": "v0.1 default: context caching only — set HERMES_BROWSER_CONTROL=true to enable.",
    }
