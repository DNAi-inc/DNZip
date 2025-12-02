"""
Copyright 2025 DNAi inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

"""
Command-line interface for DNZIP (``dnzip``).

This module implements a small but robust CLI on top of the DNZIP library.
It is intentionally simple and only depends on the Python standard library.

Supported commands (via ``python -m dnzip``):

- ``list``    : List entries in an archive
- ``info``    : Show a detailed table of entries
- ``extract`` : Extract entries to a directory
- ``create``  : Create a new archive from files and directories

Example usages:

    # List entries
    python -m dnzip list archive.zip

    # Show detailed info
    python -m dnzip info archive.zip

    # Extract everything into ./output
    python -m dnzip extract archive.zip -d output

    # Create archive.zip from all files under ./data
    python -m dnzip create archive.zip data

The CLI is intentionally conservative:
- It validates paths to avoid directory traversal when extracting.
- It uses the same safety checks as the core library (CRC, ZIP64 bounds, etc.).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Iterable, List, Optional

# Import the core API.
# We prefer relative imports when DNZIP is used as a proper package
# (e.g., `python -m dnzip`), but fall back to absolute imports when the
# module is executed as a script or from a frozen/packaged binary where
# `__package__` may not be set (PyInstaller, etc.).
try:  # pragma: no cover - environment-dependent import path
    from . import ZipReader, ZipWriter, __version__
    from .errors import ZipCrcError, ZipError, ZipFormatError
except ImportError:  # pragma: no cover
    from dnzip import ZipReader, ZipWriter, __version__
    from dnzip.errors import ZipCrcError, ZipError, ZipFormatError


def _print_error(message: str, exit_code: int = 1) -> None:
    """Print an error message to stderr and exit with the given code."""
    sys.stderr.write(f"dnzip: {message}\n")
    sys.exit(exit_code)


def _safe_extract_path(target_dir: Path, entry_name: str) -> Path:
    """
    Compute a safe extraction path for an entry.

    This function prevents directory traversal by ensuring that the final
    resolved path is still inside the target directory.
    """
    # Normalize ZIP-style separators and strip leading slashes
    normalized = entry_name.replace("\\", "/").lstrip("/")
    target_path = (target_dir / normalized).resolve()

    try:
        target_dir_resolved = target_dir.resolve()
    except FileNotFoundError:
        # If the directory does not yet exist, resolve its parent
        target_dir.mkdir(parents=True, exist_ok=True)
        target_dir_resolved = target_dir.resolve()

    # Ensure the target path is inside the extraction directory
    if os.path.commonpath([str(target_dir_resolved), str(target_path)]) != str(
        target_dir_resolved
    ):
        raise ZipFormatError(f"Refusing to extract outside target directory: {entry_name!r}")

    return target_path


def _cmd_list(archive: Path) -> None:
    """List all entries in an archive, one per line."""
    with ZipReader(archive) as z:
        for name in sorted(z.list()):
            print(name)


def _cmd_info(archive: Path) -> None:
    """Print a simple table with metadata for each entry."""
    with ZipReader(archive) as z:
        entries = sorted(z.list())

        # Print header
        print(f"Archive: {archive}")
        print("=" * 80)
        print(f"{'Name':50}  {'Size':>10}  {'Compr.':>10}  {'Method':>8}")
        print("-" * 80)

        for name in entries:
            info = z.get_info(name)
            if info is None:
                continue
            size = info.uncompressed_size
            csize = info.compressed_size
            method = info.compression_method
            display_name = name if len(name) <= 50 else name[:47] + "..."
            print(f"{display_name:50}  {size:10d}  {csize:10d}  {method:8d}")


def _cmd_extract(archive: Path, output_dir: Path) -> None:
    """
    Extract all entries in *archive* into *output_dir*.

    Directory entries are created as directories; file entries are written
    with their relative paths preserved.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    with ZipReader(archive) as z:
        for name in z.list():
            info = z.get_info(name)
            if info is None:
                continue

            # Compute safe target path and ensure parent directories exist
            target_path = _safe_extract_path(output_dir, name)

            if info.is_dir:
                target_path.mkdir(parents=True, exist_ok=True)
                continue

            target_path.parent.mkdir(parents=True, exist_ok=True)

            with z.open(name) as src, open(target_path, "wb") as dst:
                # Stream copy in chunks to support large files
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)


def _iter_files_for_create(sources: Iterable[Path]) -> Iterable[tuple[str, Path]]:
    """
    Yield (name_in_zip, source_path) pairs for all files under *sources*.

    - Directories are walked recursively.
    - Files are added with paths relative to the common parent of all sources.
    """
    # Normalize and collect all paths
    normalized: List[Path] = [p.resolve() for p in sources]
    if not normalized:
        return []

    # Compute base directory used for relative paths
    base = normalized[0].parent
    for p in normalized[1:]:
        base = Path(os.path.commonpath([base, p.parent]))

    results: List[tuple[str, Path]] = []

    for src in normalized:
        if src.is_dir():
            for root, _, files in os.walk(src):
                root_path = Path(root)
                for filename in files:
                    file_path = root_path / filename
                    rel = file_path.relative_to(base)
                    name_in_zip = str(rel).replace(os.sep, "/")
                    results.append((name_in_zip, file_path))
        else:
            rel = src.relative_to(base)
            name_in_zip = str(rel).replace(os.sep, "/")
            results.append((name_in_zip, src))

    return results


def _cmd_create(archive: Path, sources: List[Path], compression: str) -> None:
    """
    Create *archive* from the given list of source paths.

    - *sources* may contain files and directories.
    - Directory trees are preserved inside the archive.
    - *compression* is one of: ``stored``, ``deflate``.
    """
    if archive.exists():
        _print_error(f"Refusing to overwrite existing archive: {archive}", exit_code=2)

    files = list(_iter_files_for_create(sources))
    if not files:
        _print_error("No files found to add to archive", exit_code=2)

    with ZipWriter(archive) as z:
        for name_in_zip, src_path in files:
            with open(src_path, "rb") as f:
                data = f.read()
            z.add_bytes(name_in_zip, data, compression=compression)


def _build_parser() -> argparse.ArgumentParser:
    """Create and configure the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="dnzip",
        description="DNZIP - Pure Python ZIP/ZIP64 engine (library and CLI).",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # list
    p_list = subparsers.add_parser("list", help="List entries in an archive")
    p_list.add_argument("archive", type=Path, help="Path to the ZIP/ZIP64 archive")

    # info
    p_info = subparsers.add_parser("info", help="Show detailed info about archive entries")
    p_info.add_argument("archive", type=Path, help="Path to the ZIP/ZIP64 archive")

    # extract
    p_extract = subparsers.add_parser("extract", help="Extract archive contents")
    p_extract.add_argument("archive", type=Path, help="Path to the ZIP/ZIP64 archive")
    p_extract.add_argument(
        "-d",
        "--directory",
        type=Path,
        default=Path("."),
        help="Target directory to extract into (default: current directory)",
    )

    # create
    p_create = subparsers.add_parser("create", help="Create a new archive from files/dirs")
    p_create.add_argument("archive", type=Path, help="Path of the archive to create")
    p_create.add_argument(
        "sources",
        nargs="+",
        type=Path,
        help="Files and/or directories to add to the archive",
    )
    p_create.add_argument(
        "-c",
        "--compression",
        choices=["stored", "deflate"],
        default="deflate",
        help='Compression method to use ("stored" or "deflate", default: "deflate")',
    )

    return parser


def main(argv: Optional[List[str]] = None) -> None:
    """
    Entry point for the DNZIP CLI.

    This function is invoked when running:

        python -m dnzip ...

    or, if a console script is configured, via:

        dnzip ...
    """
    # Default to command-line arguments if none were provided explicitly
    if argv is None:
        argv = sys.argv[1:]

    # If no arguments are provided, show a short banner and quick examples.
    if not argv:
        print(f"DNZIP - ZIP64 Python Library (version {__version__})")
        print("Copyright (c) 2025 DNAi inc. - Apache License 2.0")
        print()
        print("Quick examples (CLI):")
        print("  python -m dnzip list archive.zip")
        print("  python -m dnzip info archive.zip")
        print("  python -m dnzip extract archive.zip -d output_dir")
        print("  python -m dnzip create archive.zip folder")
        print()
        print('For full help, run: "python -m dnzip -help" or "python -m dnzip --help"')
        return

    # Support the user-friendly single-dash variant "-help"
    if len(argv) == 1 and argv[0] == "-help":
        parser = _build_parser()
        parser.print_help()
        return

    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "list":
            _cmd_list(args.archive)
        elif args.command == "info":
            _cmd_info(args.archive)
        elif args.command == "extract":
            _cmd_extract(args.archive, args.directory)
        elif args.command == "create":
            _cmd_create(args.archive, args.sources, args.compression)
        else:
            parser.error(f"Unknown command: {args.command!r}")
    except (ZipError, ZipCrcError, ZipFormatError) as e:
        _print_error(str(e), exit_code=1)
    except KeyboardInterrupt:
        _print_error("Interrupted by user", exit_code=130)


if __name__ == "__main__":
    main()


