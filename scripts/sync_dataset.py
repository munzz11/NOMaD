#!/usr/bin/env python3
"""Sync NOMaD dataset files from Hugging Face into a local data directory."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from huggingface_hub import snapshot_download


DEFAULT_REPO_ID = "CCOM-ASV-LAB/NOMaD"


def resolve_data_dir(explicit_dir: str | None) -> Path:
    if explicit_dir:
        return Path(explicit_dir).expanduser().resolve()

    env_dir = os.getenv("NOMAD_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()

    return (Path(__file__).resolve().parent.parent / "data").resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download or update the NOMaD dataset from Hugging Face."
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Local target directory. Defaults to NOMAD_DATA_DIR or ./data.",
    )
    parser.add_argument(
        "--revision",
        default=None,
        help="Optional dataset revision (branch, tag, or commit) for reproducibility.",
    )
    parser.add_argument(
        "--allow-pattern",
        action="append",
        default=None,
        help=(
            "Restrict downloaded files by glob pattern. "
            "Can be provided multiple times."
        ),
    )
    parser.add_argument(
        "--repo-id",
        default=DEFAULT_REPO_ID,
        help=f"Hugging Face dataset repo id (default: {DEFAULT_REPO_ID}).",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    data_dir = resolve_data_dir(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    print(f"Syncing dataset '{args.repo_id}' into: {data_dir}")
    if args.revision:
        print(f"Using revision: {args.revision}")
    if args.allow_pattern:
        print(f"Allow patterns: {args.allow_pattern}")

    snapshot_download(
        repo_id=args.repo_id,
        repo_type="dataset",
        local_dir=str(data_dir),
        revision=args.revision,
        allow_patterns=args.allow_pattern,
    )

    print("Dataset sync complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
