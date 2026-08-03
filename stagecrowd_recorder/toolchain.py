"""Locating and proving the external binaries.

The image installs these on PATH, so discovery is short. What matters more is
the proving: an existence check passes for a file that was truncated mid-download
and reports "installed" right up until capture starts, which for a live
broadcast means discovering it partway through the only chance to record. So
every binary is executed once before a run commits to it, and the version string
that probe produces is kept — the caller already paid for the process, and
knowing which build produced a recording is the question asked afterwards.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .errors import ToolError

DOWNLOADER = "N_m3u8DL-RE"
MUXER = "ffmpeg"
PROBER = "ffprobe"


class Decryptor(str, Enum):
    """Which binary performs decryption.

    SHAKA is the default: these streams are ``cbcs`` and are decrypted while
    still arriving, which is precisely the combination the downloader warns
    against handing to mp4decrypt.
    """

    SHAKA = "SHAKA_PACKAGER"
    BENTO = "MP4DECRYPT"

    def __str__(self) -> str:  # argparse renders choices with str()
        return self.value

    @property
    def binary(self) -> str:
        return "shaka-packager" if self is Decryptor.SHAKA else "mp4decrypt"

    @property
    def candidates(self) -> tuple[str, ...]:
        if self is Decryptor.SHAKA:
            # Upstream publishes the executable as packager-linux-x64; a copy
            # downloaded by hand should work without being renamed first.
            return ("shaka-packager", "packager", "packager-linux-x64")
        return ("mp4decrypt",)


_VERSION_FLAG = {MUXER: ["-version"], PROBER: ["-version"]}
_DEFAULT_VERSION_FLAG = ["--version"]


@dataclass(frozen=True, slots=True)
class Probe:
    runnable: bool
    detail: str


def probe(path: Path, *, timeout: float = 15.0) -> Probe:
    """Run a binary once and report whether the OS would execute it.

    A non-zero exit is fine and expected — mp4decrypt has no ``--version`` and
    answers with usage text and exit 1. What is being tested is whether the
    kernel refuses the image at all.
    """
    flag = _VERSION_FLAG.get(path.stem.lower(), _DEFAULT_VERSION_FLAG)
    try:
        finished = subprocess.run(
            [str(path), *flag],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except OSError as exc:
        return Probe(False, str(exc))
    except subprocess.SubprocessError as exc:
        return Probe(False, f"did not respond: {exc}")

    if finished.returncode < 0:
        # How a truncated ELF presents: the loader kills it with a signal.
        return Probe(False, f"killed by signal {-finished.returncode}")

    output = f"{finished.stdout}\n{finished.stderr}"
    line = next((ln.strip() for ln in output.splitlines() if ln.strip()), "runs")
    return Probe(True, line[:70])


def _from_env(variable: str) -> Path | None:
    value = os.environ.get(variable, "").strip()
    if value and Path(value).exists():
        return Path(value)
    return None


def find(*names: str, env: str | None = None) -> Path | None:
    if env:
        override = _from_env(env)
        if override:
            return override
    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


@dataclass(frozen=True, slots=True)
class Toolchain:
    downloader: Path | None
    muxer: Path | None
    decryptor: Path | None
    decryptor_kind: Decryptor = Decryptor.SHAKA

    @classmethod
    def discover(cls, decryptor: Decryptor = Decryptor.SHAKA) -> "Toolchain":
        env = "STC_SHAKA" if decryptor is Decryptor.SHAKA else "STC_MP4DECRYPT"
        return cls(
            downloader=find(DOWNLOADER, env="STC_DOWNLOADER"),
            muxer=find(MUXER, env="STC_FFMPEG"),
            decryptor=find(*decryptor.candidates, env=env),
            decryptor_kind=decryptor,
        )

    @property
    def prober(self) -> Path | None:
        """ffprobe, looked for beside ffmpeg before falling back to PATH."""
        if self.muxer:
            sibling = self.muxer.with_name(self.muxer.name.replace(MUXER, PROBER))
            if sibling.exists():
                return sibling
        return find(PROBER)

    def verify(self) -> dict[str, str]:
        """Prove each required binary runs. Returns name -> version detail."""
        required = (
            (DOWNLOADER, self.downloader),
            (MUXER, self.muxer),
            (self.decryptor_kind.binary, self.decryptor),
        )
        missing = [name for name, path in required if path is None]
        if missing:
            raise ToolError(
                f"missing required binaries: {', '.join(missing)}",
                remedy="Rebuild the image — it installs all three on PATH.",
            )

        versions: dict[str, str] = {}
        broken: list[str] = []
        for name, path in required:
            assert path is not None
            result = probe(path)
            if result.runnable:
                versions[name] = result.detail
            else:
                broken.append(f"{name} ({result.detail})")
        if broken:
            raise ToolError(
                f"present but not runnable: {', '.join(broken)}",
                remedy="Usually a truncated download or a wrong architecture. Rebuild the image.",
            )
        return versions
