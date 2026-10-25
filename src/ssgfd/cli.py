"""Command-line interface for SSGFD."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .client import StellaSoraGameFilesDownloader


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ssgfd", description="Stella Sora Global/EN game files"
    )
    parser.add_argument("--data-dir", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("update", "query", "download", "clean"):
        command = commands.add_parser(name)
        command.add_argument("platform", choices=("windows", "android"))
        if name in ("query", "download"):
            command.add_argument("pattern")
            command.add_argument(
                "--filter-method",
                default="auto",
                choices=(
                    "auto",
                    "contains",
                    "glob",
                    "regex",
                    "starts_with",
                    "ends_with",
                ),
            )
        if name == "download":
            command.add_argument(
                "-o", "--output-dir", type=Path, default=Path("download")
            )
            command.add_argument("--with-path", action="store_true")
            command.add_argument("--max-files", type=int, default=50)
        if name == "update":
            command.add_argument("--force", action="store_true")
        if name == "query":
            command.add_argument("--format", choices=("name", "json"), default="name")
    args = parser.parse_args(argv)
    client = StellaSoraGameFilesDownloader(args.data_dir)
    if args.command == "update":
        status = client.update(args.platform, force=args.force)
        print(json.dumps(asdict(status)))
    elif args.command == "query":
        files = client.query(args.pattern, args.platform, args.filter_method)
        if args.format == "json":
            print(json.dumps([asdict(item) for item in files]))
        else:
            for item in files:
                print(item.name)
    elif args.command == "download":
        result = client.download(
            args.pattern,
            args.platform,
            output_dir=args.output_dir,
            with_path=args.with_path,
            filter_method=args.filter_method,
            max_files=args.max_files,
        )
        for path in result.files:
            print(path)
    elif args.command == "clean":
        client.clean(args.platform)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
