"""Argument parsing and dispatch.

The settings file is loaded before the parser is built, because argparse
captures its defaults at construction time: a value read from the environment
after that point is read too late and is silently ignored. So the file is applied
into the environment first, and the precedence that falls out is command line,
then real environment, then file, then built-in default.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import console, runbook, salvage, settings as cfg
from .errors import ArcError
from .settings import Settings
from .toolchain import Decryptor


def _settings_file_from_argv(argv: list[str]) -> Path | None:
    """Pull --settings out of argv by hand, before the parser exists."""
    for index, token in enumerate(argv):
        if token == "--settings" and index + 1 < len(argv):
            return Path(argv[index + 1])
        if token.startswith("--settings="):
            return Path(token.split("=", 1)[1])
    return None


def _add_stream_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--url",
        default=cfg.env(cfg.ENV_URL),
        help="m3u8 URL. A master playlist is strongly preferred.",
    )
    parser.add_argument(
        "--out",
        default=cfg.env(cfg.ENV_OUT),
        help="output directory (default: archive/run_<timestamp>)",
    )
    parser.add_argument(
        "--key",
        action="append",
        default=[cfg.env(cfg.ENV_KEYS)] if cfg.env(cfg.ENV_KEYS) else [],
        metavar="KID:KEY",
        help="content key; repeat or comma-separate. Skips the license step entirely.",
    )
    parser.add_argument(
        "--token",
        default=cfg.env(cfg.ENV_TOKEN),
        help="license session token; the endpoint URL is built from it",
    )
    parser.add_argument(
        "--license-url",
        default=cfg.env(cfg.ENV_LICENSE_URL),
        help="full license URL, used instead of --token",
    )
    parser.add_argument(
        "--headers-file",
        default=cfg.env(cfg.ENV_HEADERS),
        help="headers for --license-url, one 'Name: value' per line",
    )
    parser.add_argument(
        "--cdm",
        default=cfg.env(cfg.ENV_CDM) or str(cfg.DEFAULT_CDM),
        help="Widevine device file (default: /config/device.wvd)",
    )
    parser.add_argument(
        "--decryptor",
        type=Decryptor,
        choices=list(Decryptor),
        default=Decryptor.SHAKA,
        help="decryption engine (default: SHAKA_PACKAGER)",
    )
    parser.add_argument(
        "--allow-partial-keys",
        action="store_true",
        help="capture even when some tracks have no key; those will not decrypt",
    )
    parser.add_argument(
        "--discard-shards",
        action="store_true",
        help="do not keep decrypted shards — halves disk use, gives up rebuild",
    )
    parser.add_argument(
        "--burst-output",
        action="store_true",
        help="write the muxed file as fast as data arrives; faster, but the file "
        "cannot be played while it is being written",
    )
    parser.add_argument("--quiet-shards", action="store_true", help="do not echo each shard to the console")
    parser.add_argument("--no-shard-log", action="store_true", help="do not track shards at all")
    parser.add_argument("--verbose-downloader", action="store_true", help="let the downloader log to the console")
    parser.add_argument(
        "--guard-interval",
        type=float,
        default=240.0,
        help="seconds between key-rotation re-checks (default: 240)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stagecrowd_recorder",
        description="Archive a Widevine-protected HLS live stream.",
    )
    parser.add_argument("--settings", metavar="FILE", help="settings file (default: ./.stagecrowd)")
    subcommands = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("capture", "resolve, obtain keys, check coverage, then record"),
        ("plan", "everything capture does, but print the command instead of running it"),
        ("keys", "obtain and print keys without recording"),
    ):
        sub = subcommands.add_parser(name, help=help_text)
        _add_stream_options(sub)

    rebuild = subcommands.add_parser("rebuild", help="rebuild a playable file from kept shards")
    rebuild.add_argument("target", help="a run output directory, or a shard directory")
    rebuild.add_argument("-o", "--output", help="destination file (default: <shards>-rebuilt.mkv)")

    probe = subcommands.add_parser("probe", help="exercise the toolchain and the CDM")
    probe.add_argument("--cdm", default=cfg.env(cfg.ENV_CDM) or str(cfg.DEFAULT_CDM))
    probe.add_argument("--decryptor", type=Decryptor, choices=list(Decryptor), default=Decryptor.SHAKA)

    return parser


def _settings_from(args: argparse.Namespace) -> Settings:
    literal = tuple(k for k in (args.key or []) if k.strip())
    return Settings(
        url=(args.url or "").strip(),
        out_dir=Path(args.out) if getattr(args, "out", "") else None,
        cdm_path=Path(args.cdm),
        license_url=(args.license_url or "").strip(),
        license_token=(args.token or "").strip(),
        headers_file=Path(args.headers_file) if getattr(args, "headers_file", "") else None,
        literal_keys=literal,
        decryptor=args.decryptor,
        keep_shards=not args.discard_shards,
        strict_coverage=not args.allow_partial_keys,
        paced_output=not args.burst_output,
        quiet_downloader=not args.verbose_downloader,
        shard_log=not args.no_shard_log,
        shard_echo=not args.quiet_shards,
        guard_interval=args.guard_interval,
    )


def _run_rebuild(args: argparse.Namespace) -> int:
    destination = Path(args.output) if args.output else None
    result = salvage.rebuild(Path(args.target), destination)
    console.stage("rebuilt")
    size_mb = result.destination.stat().st_size / 1_048_576
    console.say(
        f"{result.destination}  ({size_mb:.1f} MB, {result.seconds:.1f}s container / "
        f"{result.shortest_track():.0f}s on every track)"
    )
    for note in result.notes():
        console.warn(note)
    if not result.intact:
        console.detail(
            "Rebuilt from an interrupted run. This is everything that survived; "
            "the rest was never written to disk."
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    path, applied = cfg.load_settings_file(_settings_file_from_argv(argv))
    args = build_parser().parse_args(argv)
    if path is not None and applied:
        console.detail(f"settings from {path}: {', '.join(sorted(applied))}")

    try:
        if args.command == "rebuild":
            return _run_rebuild(args)
        if args.command == "probe":
            return runbook.probe_environment(
                Settings(cdm_path=Path(args.cdm), decryptor=args.decryptor)
            )

        settings = _settings_from(args)
        if args.command == "capture":
            return runbook.execute(settings)
        if args.command == "plan":
            return runbook.describe_plan(settings)
        if args.command == "keys":
            return runbook.show_keys(settings)
        raise AssertionError(f"unhandled command {args.command!r}")
    except ArcError as error:
        return runbook.report_error(error)
    except KeyboardInterrupt:
        console.warn("interrupted")
        return 130
