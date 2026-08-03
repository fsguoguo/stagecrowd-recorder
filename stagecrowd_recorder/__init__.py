"""stagecrowd_recorder — a container-first archiver for Widevine-protected HLS live streams.

The package splits along one line: what the stream *is* (playlist, protection,
keys, coverage) and what is *done* about it (capture, salvage). Everything
in the first group is pure and testable without a network or a subprocess.

Key acquisition here means replaying a license request against a local CDM. There
is no browser automation: attaching to a logged-in browser to capture a request
is a desktop concern a container cannot reproduce, so the token is an input to
this tool rather than something it goes and finds.
"""

from __future__ import annotations

__version__ = "1.0.0"


def main(argv: list[str] | None = None) -> int:
    from .cli import main as _main

    return _main(argv)


__all__ = ["__version__", "main"]
