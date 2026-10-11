"""Command-line entry point for the agora package."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

import agora


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agora",
        description="Hermes Agora persistent social runtime (replay slice).",
    )
    parser.add_argument("--version", action="version", version=f"agora {agora.__version__}")
    subparsers = parser.add_subparsers(dest="command")
    replay = subparsers.add_parser("replay", help="run a deterministic replay fixture")
    replay.add_argument("manifest", type=Path, help="fixture manifest JSON path")
    replay.add_argument(
        "--run-dir",
        type=Path,
        default=None,
        help="directory for the replay databases (default: a fresh temp directory)",
    )
    return parser


def _run_replay(manifest_path: Path, run_dir: Path | None) -> int:
    from agora.runner import ManifestError, ReplayRunner, RunManifest

    try:
        manifest = RunManifest.from_payload(json.loads(manifest_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ManifestError, ValueError) as exc:
        print(f"agora replay: invalid manifest: {exc}", file=sys.stderr)
        return 2
    directory = run_dir or Path(tempfile.mkdtemp(prefix="agora-replay-"))
    runner = ReplayRunner(manifest, directory)
    trace = runner.run()
    json.dump(trace, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "command", None) == "replay":
        return _run_replay(args.manifest, args.run_dir)
    return 0
