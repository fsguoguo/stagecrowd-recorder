"""Console output.

Two rules. Nothing here formats domain decisions — a message is handed a
finished sentence. And colour is off unless the stream is a terminal and NO_COLOR
is unset, because the container's usual output destination is a log file.
"""

from __future__ import annotations

import os
import sys

_ENABLED = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

_DIM = "\033[2m" if _ENABLED else ""
_BOLD = "\033[1m" if _ENABLED else ""
_RED = "\033[31m" if _ENABLED else ""
_YELLOW = "\033[33m" if _ENABLED else ""
_GREEN = "\033[32m" if _ENABLED else ""
_OFF = "\033[0m" if _ENABLED else ""


def _emit(text: str, stream=sys.stdout) -> None:
    stream.write(text + "\n")
    stream.flush()


def stage(text: str) -> None:
    _emit(f"{_BOLD}==>{_OFF} {text}")


def say(text: str) -> None:
    _emit(text)


def detail(text: str) -> None:
    _emit(f"{_DIM}    {text}{_OFF}")


def good(text: str) -> None:
    _emit(f"{_GREEN}ok{_OFF}  {text}")


def warn(text: str) -> None:
    _emit(f"{_YELLOW}!{_OFF}   {text}", sys.stderr)


def fail(text: str, remedy: str | None = None) -> None:
    _emit(f"{_RED}x{_OFF}   {text}", sys.stderr)
    if remedy:
        for line in remedy.splitlines():
            _emit(f"    {_DIM}{line}{_OFF}", sys.stderr)


def table(rows: list[tuple[str, str]]) -> None:
    if not rows:
        return
    width = max(len(left) for left, _ in rows)
    for left, right in rows:
        _emit(f"    {left:<{width}}  {right}")
