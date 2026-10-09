"""Command-line entry point for the agora package."""

import argparse
from collections.abc import Sequence

import agora


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agora",
        description="Hermes Agora persistent social runtime (replay tooling not yet implemented).",
    )
    parser.add_argument("--version", action="version", version=f"agora {agora.__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    return 0
