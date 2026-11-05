"""Hermes 3DS — Nintendo 3DS platform plugin for Hermes Agent.

Registers the `3ds` gateway platform and the `hermes 3ds pair|devices|unpair` CLI.
"""

from __future__ import annotations

__all__ = ["register"]


def check_requirements() -> bool:
    try:
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False


def is_connected(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    return bool(getattr(config, "enabled", False) or extra.get("enabled"))


def register(ctx):
    from . import compat
    from .adapter import ThreeDSAdapter
    from .cli import handle_cli, setup_cli

    compat.bind(ctx)  # the jobs tab calls Hermes' cron tool through the plugin context

    ctx.register_platform(
        name="3ds",
        label="Nintendo 3DS",
        adapter_factory=ThreeDSAdapter,
        check_fn=check_requirements,
        is_connected=is_connected,
        install_hint="Needs the `cryptography` package (bundled with Hermes)",
        cron_deliver_env_var="THREEDS_HOME_CHANNEL",
        max_message_length=8000,
        emoji="🎮",
        display_tier="high",
        platform_hint=(
            "You are chatting with the user on a Nintendo 3DS handheld (400x240 top screen, "
            "320x240 touch screen, typing on a small on-screen keyboard). Keep replies short and "
            "scannable: brief paragraphs, short lists, no wide tables. Markdown (bold, italics, code, "
            "lists, headings, links) is rendered in simplified form. "
            "You can send files to this chat natively: put MEDIA:/absolute/path/to/file on its own "
            "line in your reply (Windows paths like MEDIA:C:\\Users\\name\\file.mp3 work), not inside "
            "backticks or a code block. Any file type works: images open in an image viewer, audio "
            "plays on the 3DS, text/code/PDF/documents open in a reader, everything else becomes a "
            "downloadable card. Image URLs via ![alt](url) are sent as images too. When the user asks "
            "you to send, show, attach or play a file, create or find it, then attach it this way; "
            "never tell them to open it on the PC instead. "
            "Photos from this chat come from the 3DS camera: 640x480 (0.3 MP), often soft or dim, and "
            "screens photographed with it show moire. Answer from the photo as it is, in one look: don't "
            "crop, zoom, upscale or re-analyze it with tools, and if a detail is too small to read, say so "
            "and ask for a closer photo."),
    )
    from .speech import register_provider
    register_provider(ctx)  # streaming spoken replies (speech.py); inert until tts.streaming.provider names it
    ctx.register_cli_command(
        name="3ds", help="Pair and manage Nintendo 3DS devices",
        description="Pair a Nintendo 3DS with this PC and manage paired devices.",
        setup_fn=setup_cli, handler_fn=handle_cli)
