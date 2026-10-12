"""Hermes Browser companion plugin — context cache + optional browser control.

Always registers:
  - Context-caching tools (browser_context_status, browser_get_context, …)
  - Lifecycle hooks (pre_llm_call, pre_tool_call, post_tool_call)
  - Bundled hermes-browser skill

Additionally registers browser-control tools when
``HERMES_BROWSER_CONTROL=true`` is set in the Hermes process environment:
  - browser_list_tabs, browser_open_tab, browser_navigate, browser_click,
    browser_type_text, browser_press_key, browser_scroll, browser_read_page,
    browser_inspect_element, browser_screenshot, browser_media_control,
    browser_controller_status

The plugin is fail-soft: all tools return structured {ok, data|error} JSON
and never raise into the Hermes runtime.
"""

from __future__ import annotations

from pathlib import Path

from . import hooks, tools
from .context_store import BrowserContextStore
from .policy import browser_control_allowed

# Global store — shared between tools and hooks.
STORE = BrowserContextStore()


def register(ctx) -> None:
    """Register companion plugin tools, hooks, and bundled skill."""
    durable_receipts = ctx.get_config("durable_receipts", default=True)
    receipt_limit = ctx.get_config("receipt_limit", default=8)
    STORE.configure_durable_state(
        ctx.state,
        enabled=durable_receipts is True,
        receipt_limit=receipt_limit,
    )
    # Phase 8 Task 31: metadata-only rotation journal, persisted under the
    # plugin's profile-scoped data dir (mode 0600 where supported) when one
    # exists, memory-only otherwise.
    try:
        data_dir = getattr(ctx.state, "data_dir", None)
    except Exception:
        data_dir = None
    STORE.configure_journal(data_dir)
    # Tell tools which store to use
    tools.set_store(STORE)
    hooks.set_store(STORE)

    # ── Tools ──────────────────────────────────────────────────────────
    ctx.register_tool(
        name="browser_context_status",
        toolset="hermes-browser-companion",
        schema=tools.SCHEMA_STATUS,
        handler=tools.browser_context_status,
        description="Check whether browser context is currently cached and available.",
        emoji="🌐",
    )

    ctx.register_tool(
        name="browser_get_context",
        toolset="hermes-browser-companion",
        schema=tools.SCHEMA_GET_CONTEXT,
        handler=tools.browser_get_context,
        description="Retrieve the current cached browser context envelope (scope, active tab, payload hash).",
        emoji="📄",
    )

    ctx.register_tool(
        name="browser_clear_context",
        toolset="hermes-browser-companion",
        schema=tools.SCHEMA_CLEAR_CONTEXT,
        handler=tools.browser_clear_context,
        description="Clear the cached browser context. The next extension prompt will re-populate it.",
        emoji="🗑️",
    )

    ctx.register_tool(
        name="browser_event_log",
        toolset="hermes-browser-companion",
        schema=tools.SCHEMA_EVENT_LOG,
        handler=tools.browser_event_log,
        description="Return recent browser companion events for diagnostics.",
        emoji="📋",
    )

    ctx.register_tool(
        name="browser_control_status",
        toolset="hermes-browser-companion",
        schema=tools.SCHEMA_CONTROL_STATUS,
        handler=tools.browser_control_status,
        description="Return historical owner-scoped Browser control metadata; never live authority.",
        emoji="🧭",
    )

    ctx.register_tool(
        name="browser_context_journal",
        toolset="hermes-browser-companion",
        schema=tools.SCHEMA_JOURNAL,
        handler=tools.browser_context_journal,
        description="Return the metadata-only owner-scoped journal of stored browser-context deliveries; never page data.",
        emoji="📒",
    )

    ctx.register_tool(
        name="browser_text_utility",
        toolset="hermes-browser-companion",
        schema=tools.SCHEMA_TEXT_UTILITY,
        handler=tools.browser_text_utility,
        description="Run bounded deterministic text cleanup, bullets, stats, or diff without a model call.",
        emoji="⚙️",
    )

    # ── Hooks ──────────────────────────────────────────────────────────
    ctx.register_hook("pre_llm_call", hooks.pre_llm_call)
    ctx.register_hook("pre_tool_call", hooks.pre_tool_call)
    ctx.register_hook("post_tool_call", hooks.post_tool_call)

    # ── Browser-control tools (opt-in via HERMES_BROWSER_CONTROL=true) ─
    if browser_control_allowed():
        _register_browser_control_tools(ctx)

    # ── Bundled skill ──────────────────────────────────────────────────
    skills_dir = Path(__file__).resolve().parent / "skills"
    skill_md = skills_dir / "hermes-browser" / "SKILL.md"
    if skill_md.is_file():
        ctx.register_skill("hermes-browser", skill_md)


def _register_browser_control_tools(ctx) -> None:  # noqa: ANN001
    """Register the live browser-control tool suite (requires HERMES_BROWSER_CONTROL=true)."""
    from . import browser_control_tools as bct
    from .browser_control_schemas import (
        SCHEMA_LIST_TABS,
        SCHEMA_OPEN_TAB,
        SCHEMA_NAVIGATE,
        SCHEMA_CLICK,
        SCHEMA_TYPE_TEXT,
        SCHEMA_PRESS_KEY,
        SCHEMA_SCROLL,
        SCHEMA_READ_PAGE,
        SCHEMA_INSPECT_ELEMENT,
        SCHEMA_SCREENSHOT,
        SCHEMA_MEDIA_CONTROL,
        SCHEMA_CONTROLLER_STATUS,
    )

    _TOOLSET = "hermes-browser-control"

    ctx.register_tool(
        name="browser_list_tabs",
        toolset=_TOOLSET,
        schema=SCHEMA_LIST_TABS,
        handler=bct.browser_list_tabs,
        description="List all open browser tabs (read-only, no debugger attach).",
        emoji="🗂️",
    )
    ctx.register_tool(
        name="browser_open_tab",
        toolset=_TOOLSET,
        schema=SCHEMA_OPEN_TAB,
        handler=bct.browser_open_tab,
        description="Open a new browser tab and navigate to a URL.",
        emoji="🔗",
    )
    ctx.register_tool(
        name="browser_navigate",
        toolset=_TOOLSET,
        schema=SCHEMA_NAVIGATE,
        handler=bct.browser_navigate,
        description="Navigate the current leased tab to a URL.",
        emoji="🧭",
    )
    ctx.register_tool(
        name="browser_click",
        toolset=_TOOLSET,
        schema=SCHEMA_CLICK,
        handler=bct.browser_click,
        description="Click an element on the current leased tab.",
        emoji="🖱️",
    )
    ctx.register_tool(
        name="browser_type_text",
        toolset=_TOOLSET,
        schema=SCHEMA_TYPE_TEXT,
        handler=bct.browser_type_text,
        description="Type text into a field (never submits).",
        emoji="⌨️",
    )
    ctx.register_tool(
        name="browser_press_key",
        toolset=_TOOLSET,
        schema=SCHEMA_PRESS_KEY,
        handler=bct.browser_press_key,
        description="Press a keyboard key. Enter/Return require confirmed=true.",
        emoji="↵",
    )
    ctx.register_tool(
        name="browser_scroll",
        toolset=_TOOLSET,
        schema=SCHEMA_SCROLL,
        handler=bct.browser_scroll,
        description="Scroll the current leased tab.",
        emoji="↕️",
    )
    ctx.register_tool(
        name="browser_read_page",
        toolset=_TOOLSET,
        schema=SCHEMA_READ_PAGE,
        handler=bct.browser_read_page,
        description="Return page text and interactive element refs via accessibility snapshot.",
        emoji="📖",
    )
    ctx.register_tool(
        name="browser_inspect_element",
        toolset=_TOOLSET,
        schema=SCHEMA_INSPECT_ELEMENT,
        handler=bct.browser_inspect_element,
        description="Inspect a specific element by ref or CSS selector.",
        emoji="🔍",
    )
    ctx.register_tool(
        name="browser_screenshot",
        toolset=_TOOLSET,
        schema=SCHEMA_SCREENSHOT,
        handler=bct.browser_screenshot,
        description="Capture a PNG screenshot of the current leased tab.",
        emoji="📸",
    )
    ctx.register_tool(
        name="browser_media_control",
        toolset=_TOOLSET,
        schema=SCHEMA_MEDIA_CONTROL,
        handler=bct.browser_media_control,
        description="Play, pause, or adjust volume on media in the current tab.",
        emoji="▶️",
    )
    ctx.register_tool(
        name="browser_controller_status",
        toolset=_TOOLSET,
        schema=SCHEMA_CONTROLLER_STATUS,
        handler=bct.browser_controller_status,
        description="Show live browser-control bridge connection status.",
        emoji="📡",
    )
