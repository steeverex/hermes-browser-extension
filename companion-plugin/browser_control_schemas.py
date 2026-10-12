"""JSON-schema definitions for the Hermes Browser Control tool suite.

Each schema follows the OpenAI function-calling format used by the rest of
the companion plugin.  All tools return ``{ok, data}`` on success or
``{ok: false, error: {code, message}}`` on failure — never raise.
"""

from __future__ import annotations

# ── Shared sub-shapes ──────────────────────────────────────────────────────────

_SELECTOR_PROPERTIES = {
    "ref": {
        "type": "string",
        "description": (
            "Opaque element ref from a prior browser_read_page or browser_inspect_element "
            "snapshot.  Preferred over CSS selector."
        ),
    },
    "selector": {
        "type": "string",
        "description": "CSS selector to locate the target element (used only when ref is absent).",
    },
}

# ── list_tabs ──────────────────────────────────────────────────────────────────

SCHEMA_LIST_TABS = {
    "name": "browser_list_tabs",
    "description": (
        "List all open browser tabs.  Read-only; never attaches the debugger.  "
        "Returns id, title, url, active, and whether the controller has already "
        "attached to each tab."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

# ── open_tab / navigate ────────────────────────────────────────────────────────

SCHEMA_OPEN_TAB = {
    "name": "browser_open_tab",
    "description": (
        "Open a new browser tab and navigate to a URL, or navigate the current "
        "leased tab to a URL.  Returns the new tab id, final URL, and page title "
        "after the navigation settles."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to open or navigate to.",
            },
            "new_tab": {
                "type": "boolean",
                "description": (
                    "When true (default), always open a fresh tab.  "
                    "When false, reuse the current leased tab."
                ),
                "default": True,
            },
            "wait_ms": {
                "type": "integer",
                "description": "Extra milliseconds to wait for the page to settle (0–10000, default 1500).",
                "minimum": 0,
                "maximum": 10000,
                "default": 1500,
            },
        },
        "required": ["url"],
        "additionalProperties": False,
    },
}

SCHEMA_NAVIGATE = {
    "name": "browser_navigate",
    "description": "Navigate the current leased tab to a URL and wait for the page to settle.",
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to navigate to.",
            },
            "wait_ms": {
                "type": "integer",
                "description": "Extra milliseconds to wait for the page to settle (0–10000, default 1500).",
                "minimum": 0,
                "maximum": 10000,
                "default": 1500,
            },
        },
        "required": ["url"],
        "additionalProperties": False,
    },
}

# ── click ──────────────────────────────────────────────────────────────────────

SCHEMA_CLICK = {
    "name": "browser_click",
    "description": (
        "Click an element on the current leased tab.  Identify the target by "
        "ref (from a prior snapshot) or CSS selector.  "
        "Returns a post-click snapshot confirming the DOM changed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            **_SELECTOR_PROPERTIES,
            "x": {
                "type": "number",
                "description": "Viewport X coordinate (used only when both ref and selector are absent).",
            },
            "y": {
                "type": "number",
                "description": "Viewport Y coordinate (used only when both ref and selector are absent).",
            },
            "verify_change": {
                "type": "boolean",
                "description": "When true (default), take a post-click snapshot to verify the action.",
                "default": True,
            },
        },
        "additionalProperties": False,
    },
}

# ── type_text ──────────────────────────────────────────────────────────────────

SCHEMA_TYPE_TEXT = {
    "name": "browser_type_text",
    "description": (
        "Type text into a focused or targeted input element.  "
        "Use ref or selector to target, or omit both to type into whatever is focused.  "
        "Does NOT submit forms — call browser_press_key with key='Enter' separately "
        "and only after explicit confirmation."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            **_SELECTOR_PROPERTIES,
            "text": {
                "type": "string",
                "description": "The text to type.",
            },
            "clear_first": {
                "type": "boolean",
                "description": "When true, clear the field before typing.",
                "default": False,
            },
        },
        "required": ["text"],
        "additionalProperties": False,
    },
}

# ── press_key ──────────────────────────────────────────────────────────────────

SCHEMA_PRESS_KEY = {
    "name": "browser_press_key",
    "description": (
        "Press a keyboard key on the current leased tab.  "
        "Consequential keys (Enter on a form, Escape, etc.) will be flagged for "
        "confirmation before execution unless the user has pre-approved."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": (
                    "Key name: Enter, Tab, Escape, Backspace, Delete, ArrowUp, ArrowDown, "
                    "ArrowLeft, ArrowRight, or a single character."
                ),
            },
        },
        "required": ["key"],
        "additionalProperties": False,
    },
}

# ── scroll ─────────────────────────────────────────────────────────────────────

SCHEMA_SCROLL = {
    "name": "browser_scroll",
    "description": "Scroll the current leased tab in a direction.",
    "parameters": {
        "type": "object",
        "properties": {
            "direction": {
                "type": "string",
                "enum": ["up", "down", "left", "right"],
                "description": "Direction to scroll.",
            },
            "amount": {
                "type": "string",
                "enum": ["small", "medium", "large"],
                "description": "Scroll distance: small≈200px, medium≈600px (default), large≈1200px.",
                "default": "medium",
            },
        },
        "required": ["direction"],
        "additionalProperties": False,
    },
}

# ── read_page ──────────────────────────────────────────────────────────────────

SCHEMA_READ_PAGE = {
    "name": "browser_read_page",
    "description": (
        "Return the visible text and interactive element refs of the current leased tab.  "
        "Use the returned refs with browser_click, browser_type_text, etc."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "include_text": {
                "type": "boolean",
                "description": "Include page body text (default true).",
                "default": True,
            },
            "include_refs": {
                "type": "boolean",
                "description": "Include interactive element ref list (default true).",
                "default": True,
            },
        },
        "additionalProperties": False,
    },
}

# ── inspect_element ────────────────────────────────────────────────────────────

SCHEMA_INSPECT_ELEMENT = {
    "name": "browser_inspect_element",
    "description": (
        "Inspect a specific element on the page.  "
        "Returns the element's role, name, attributes, and a fresh ref."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            **_SELECTOR_PROPERTIES,
        },
        "additionalProperties": False,
    },
}

# ── screenshot ─────────────────────────────────────────────────────────────────

SCHEMA_SCREENSHOT = {
    "name": "browser_screenshot",
    "description": (
        "Capture a screenshot of the current leased tab (or a specific element).  "
        "Returns a base-64 PNG data URL."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "ref": {
                "type": "string",
                "description": "Optional element ref to clip the screenshot to that element.",
            },
        },
        "additionalProperties": False,
    },
}

# ── media_control ──────────────────────────────────────────────────────────────

SCHEMA_MEDIA_CONTROL = {
    "name": "browser_media_control",
    "description": (
        "Control media playback on the current leased tab.  "
        "Supports play, pause, and volume control on HTML5 media elements "
        "(video/audio tags on the page, including YouTube's player)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["play", "pause", "toggle", "mute", "unmute", "set_volume"],
                "description": "The media action to perform.",
            },
            "volume": {
                "type": "number",
                "description": "Volume level 0.0–1.0, used only with set_volume.",
                "minimum": 0.0,
                "maximum": 1.0,
            },
            "selector": {
                "type": "string",
                "description": "Optional CSS selector for a specific media element (default: first video or audio).",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}

# ── controller_status ──────────────────────────────────────────────────────────

SCHEMA_CONTROLLER_STATUS = {
    "name": "browser_controller_status",
    "description": (
        "Return the live connection status of the browser control bridge: "
        "whether the gateway WebSocket is connected, which tabs are leased, "
        "and whether the extension controller is registered."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}
