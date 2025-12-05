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

from __future__ import annotations

"""
Command-line interface for DNZIP (``dnzip``).

This module implements a small but robust CLI on top of the DNZIP library.
It is intentionally simple and only depends on the Python standard library.

Supported commands (via ``python -m dnzip``):

- ``list``    : List entries in an archive
- ``info``    : Show a detailed table of entries
- ``extract`` : Extract entries to a directory
- ``create``  : Create a new archive from files and directories
- ``test``    : Test archive integrity without extracting
- ``verify``  : Verify archive integrity (alias for test)

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

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional

# Import the core API.
# We prefer relative imports when DNZIP is used as a proper package
# (e.g., `python -m dnzip`), but fall back to absolute imports when the
# module is executed as a script or from a frozen/packaged binary where
# `__package__` may not be set (PyInstaller, etc.).
try:  # pragma: no cover - environment-dependent import path
    from . import ZipReader, ZipWriter, GzipReader, GzipWriter, Bzip2Reader, Bzip2Writer, XzReader, XzWriter, TarReader, TarWriter, SevenZipReader, SevenZipWriter, RarReader, __version__
    from .errors import ZipCrcError, ZipError, ZipFormatError, SevenZipFormatError, ZipUnsupportedFeature, RarFormatError, RarUnsupportedFeature, RarError
    from .progress import ProgressCallback, create_progress_callback
    from .utils import safe_extract_path, get_archive_statistics, convert_archive, compare_archives, diff_archives, export_archive_metadata, optimize_archive, validate_and_repair_archive, recover_corrupted_archive, analyze_archive_features, analyze_rar_compatibility, batch_process_archives, batch_convert_with_smart_compression, filter_files_by_type, extract_with_filter, filter_archive, deduplicate_archive, find_duplicates_across_archives, quick_health_check, create_archive_from_file_list, sync_archive_with_directory, create_incremental_archive, create_archive_with_recent_files, create_archive_with_organization, analyze_files_for_archiving, create_archive_with_embedded_metadata, create_archive_with_filter, create_archive_with_verification, create_archive_with_compression_optimization, create_archive_with_parallel_compression, create_archive_with_redundancy, create_checksum_file, verify_checksum_file, search_archive_content, analyze_compression_options, create_archive_with_smart_compression, create_archive_with_preset_compression, create_archive_clean, create_archive_with_deduplication, create_archive_with_size_based_compression, create_timestamped_backup, create_archive_with_content_based_compression, detect_file_type_by_content, extract_with_conflict_resolution, create_archive_index, load_archive_index, search_archive_index, update_archive_index, extract_extractable_entries
    from .security_audit import create_audit_logger
    try:
        from .benchmark import BenchmarkRunner, run_multi_threaded_comparison, run_memory_mapped_comparison
    except ImportError:
        BenchmarkRunner = None
        run_multi_threaded_comparison = None
        run_memory_mapped_comparison = None
except ImportError:  # pragma: no cover
    from dnzip import ZipReader, ZipWriter, GzipReader, GzipWriter, Bzip2Reader, Bzip2Writer, XzReader, XzWriter, TarReader, TarWriter, SevenZipReader, SevenZipWriter, RarReader, __version__
    from dnzip.errors import ZipCrcError, ZipError, ZipFormatError, SevenZipFormatError, ZipUnsupportedFeature, RarFormatError, RarUnsupportedFeature, RarError
    from dnzip.progress import ProgressCallback, create_progress_callback
    from dnzip.utils import safe_extract_path, get_archive_statistics, convert_archive, diff_archives, export_archive_metadata
    try:
        from dnzip.security_audit import create_audit_logger
    except ImportError:
        create_audit_logger = None
    try:
        from dnzip.benchmark import BenchmarkRunner, run_multi_threaded_comparison, run_memory_mapped_comparison
    except ImportError:
        BenchmarkRunner = None
        run_multi_threaded_comparison = None
        run_memory_mapped_comparison = None


def _print_error(message: str, exit_code: int = 1, suggestion: Optional[str] = None) -> None:
    """Print an error message to stderr and exit with the given code.
    
    Args:
        message: Error message to display.
        exit_code: Exit code to use.
        suggestion: Optional suggestion to help the user resolve the error.
    """
    sys.stderr.write(f"dnzip: {message}\n")
    if suggestion:
        sys.stderr.write(f"dnzip: Suggestion: {suggestion}\n")
    sys.exit(exit_code)


def _detect_file_format(file_path: Path) -> Optional[str]:
    """Detect file format based on extension and magic numbers.
    
    Args:
        file_path: Path to the file to detect.
        
    Returns:
        Format name (e.g., 'zip', 'tar', 'gzip', 'bzip2', 'xz', '7z', 'rar') or None if unknown.
    """
    if not file_path.exists():
        return None
    
    # Check file extension first (fast path)
    suffix_lower = file_path.suffix.lower()
    extension_map = {
        '.zip': 'zip',
        '.tar': 'tar',
        '.gz': 'gzip',
        '.tgz': 'tar',
        '.bz2': 'bzip2',
        '.tbz2': 'tar',
        '.xz': 'xz',
        '.txz': 'tar',
        '.7z': '7z',
        '.rar': 'rar',
    }
    
    if suffix_lower in extension_map:
        return extension_map[suffix_lower]
    
    # Check magic numbers for more accurate detection
    try:
        with open(file_path, 'rb') as f:
            magic = f.read(8)
            
            # ZIP: PK\x03\x04 or PK\x05\x06 or PK\x07\x08
            if magic[:2] == b'PK':
                if magic[2:4] in (b'\x03\x04', b'\x05\x06', b'\x07\x08', b'\x01\x02'):
                    return 'zip'
            
            # GZIP: \x1f\x8b
            if magic[:2] == b'\x1f\x8b':
                return 'gzip'
            
            # BZIP2: BZ
            if magic[:2] == b'BZ':
                return 'bzip2'
            
            # XZ: \xfd7zXZ\x00
            if magic[:6] == b'\xfd7zXZ\x00':
                return 'xz'
            
            # 7Z: 7z\xbc\xaf\x27\x1c
            if magic[:6] == b'7z\xbc\xaf\x27\x1c':
                return '7z'
            
            # RAR: Rar!\x1a\x07
            if magic[:7] == b'Rar!\x1a\x07':
                return 'rar'
            
            # TAR: Check for TAR magic (ustar, etc.)
            if len(magic) >= 5:
                # USTAR: ustar at offset 257 (we read 8 bytes, so check if it's in range)
                # For simplicity, check if file ends with .tar or starts with TAR-like patterns
                if suffix_lower == '.tar':
                    return 'tar'
    except (IOError, OSError):
        pass
    
    return None


def _get_format_suggestion(file_path: Path, detected_format: Optional[str], command: str) -> Optional[str]:
    """Get a helpful suggestion based on detected format and command.
    
    Args:
        file_path: Path to the file.
        detected_format: Detected format (or None).
        command: Command that was attempted.
        
    Returns:
        Suggestion string or None.
    """
    if detected_format is None:
        return None
    
    # Map commands to format-specific commands
    format_commands = {
        'zip': {
            'list': 'list',
            'extract': 'extract',
            'create': 'create',
        },
        'tar': {
            'list': 'tar-list',
            'extract': 'tar-extract',
            'create': 'tar-create',
        },
        'gzip': {
            'compress': 'gzip-compress',
            'decompress': 'gzip-decompress',
        },
        'bzip2': {
            'compress': 'bzip2-compress',
            'decompress': 'bzip2-decompress',
        },
        'xz': {
            'compress': 'xz-compress',
            'decompress': 'xz-decompress',
        },
        '7z': {
            'list': '7z-list',
            'extract': '7z-extract',
            'create': '7z-create',
        },
        'rar': {
            'list': 'rar-list',
            'extract': 'rar-extract',
        },
    }
    
    if detected_format in format_commands:
        format_cmds = format_commands[detected_format]
        if command in format_cmds:
            suggested_cmd = format_cmds[command]
            return f"Try: python -m dnzip {suggested_cmd} {file_path}"
    
    return None


def _get_password(password: Optional[str] = None, password_file: Optional[Path] = None) -> Optional[bytes]:
    """Get password from command-line argument or password file.
    
    Args:
        password: Password string from command-line (optional).
        password_file: Path to file containing password (optional).
        
    Returns:
        Password as bytes, or None if neither password nor password_file provided.
        
    Raises:
        SystemExit: If password file cannot be read or both password and password_file are provided.
    """
    if password is not None and password_file is not None:
        _print_error("Cannot specify both --password and --password-file", exit_code=2)
    
    if password_file is not None:
        try:
            with open(password_file, "rb") as f:
                password_bytes = f.read().rstrip(b"\r\n")
                return password_bytes
        except FileNotFoundError:
            _print_error(f"Password file not found: {password_file}", exit_code=2)
        except PermissionError:
            _print_error(f"Permission denied reading password file: {password_file}", exit_code=2)
        except Exception as e:
            _print_error(f"Error reading password file: {e}", exit_code=2)
    
    if password is not None:
        return password.encode("utf-8")
    
    return None


# Use the enhanced safe_extract_path from utils module
# This function provides comprehensive security validation including:
# - Null byte detection
# - Control character validation
# - Absolute path rejection
# - Windows drive letter rejection
# - Windows reserved filename detection
# - Path length limits
# - Directory traversal protection
_safe_extract_path = safe_extract_path


def _cmd_list(archive: Path) -> None:
    """List all entries in an archive, one per line."""
    with ZipReader(archive) as z:
        for name in sorted(z.list()):
            print(name)


def _cmd_info(archive: Path, format: Optional[str] = None) -> None:
    """Print a simple table with metadata for each entry.
    
    Args:
        archive: Path to the archive file.
        format: Optional format specification (zip, tar, 7z, rar). If None, format is auto-detected.
    """
    from .utils import detect_archive_format
    
    # Detect format if not specified
    if format is None:
        detected_format = detect_archive_format(archive)
        if detected_format is None:
            _print_error(
                f"Could not detect archive format for {archive}. "
                "Please specify format using --format option.",
                exit_code=1
            )
            return
        format = detected_format
    
    # Select reader class based on format
    format_lower = format.lower()
    if format_lower == 'zip':
        reader_class = ZipReader
    elif format_lower == 'tar':
        reader_class = TarReader
    elif format_lower == '7z':
        reader_class = SevenZipReader
    elif format_lower == 'rar':
        reader_class = RarReader
    else:
        _print_error(f"Unsupported format for info command: {format}", exit_code=1)
        return
    
    try:
        with reader_class(archive) as z:
            entries = sorted(z.list())

            # Print header
            print(f"Archive: {archive}")
            print(f"Format: {format.upper()}")
            
            # Print archive comment if present (ZIP format only)
            if format_lower == 'zip' and hasattr(z, 'get_archive_comment'):
                archive_comment = z.get_archive_comment()
                if archive_comment:
                    try:
                        comment_str = archive_comment.decode("utf-8", errors="replace")
                        print(f"Archive comment: {comment_str}")
                    except Exception:
                        print(f"Archive comment: {len(archive_comment)} bytes (binary)")
            
            print("=" * 80)
            print(f"{'Name':50}  {'Size':>10}  {'Compr.':>10}  {'Method':>8}  {'Comment':>20}")
            print("-" * 80)

            for name in entries:
                info = z.get_info(name)
                if info is None:
                    continue
                
                # Handle different entry types
                size = getattr(info, 'uncompressed_size', getattr(info, 'size', 0))
                csize = getattr(info, 'compressed_size', size)
                method = getattr(info, 'compression_method', getattr(info, 'compression', 'N/A'))
                
                # Format compression method for display
                if isinstance(method, int):
                    method_display = str(method)
                elif isinstance(method, str):
                    method_display = method[:8] if len(method) <= 8 else method[:5] + "..."
                else:
                    method_display = str(method)[:8]
                
                display_name = name if len(name) <= 50 else name[:47] + "..."
                
                # Display comment if present (ZIP format only)
                comment_display = ""
                if hasattr(info, 'comment') and info.comment:
                    try:
                        if isinstance(info.comment, bytes):
                            comment_str = info.comment.decode("utf-8", errors="replace")
                        else:
                            comment_str = str(info.comment)
                        if len(comment_str) > 18:
                            comment_display = comment_str[:15] + "..."
                        else:
                            comment_display = comment_str
                    except Exception:
                        comment_display = f"{len(info.comment)}B"
                
                print(f"{display_name:50}  {size:10d}  {csize:10d}  {method_display:>8}  {comment_display:>20}")
    except Exception as e:
        _print_error(f"Failed to read archive: {e}", exit_code=1)


def _cmd_properties(archive: Path, format: Optional[str] = None) -> None:
    """Print archive properties in JSON format for programmatic use.
    
    Args:
        archive: Path to the archive file.
        format: Optional format specification (zip, tar, 7z, rar). If None, format is auto-detected.
    """
    from .utils import detect_archive_format
    
    # Detect format if not specified
    if format is None:
        detected_format = detect_archive_format(archive)
        if detected_format is None:
            _print_error(
                f"Could not detect archive format for {archive}. "
                "Please specify format using --format option.",
                exit_code=1
            )
            return
        format = detected_format
    
    # Select reader class based on format
    format_lower = format.lower()
    if format_lower == 'zip':
        reader_class = ZipReader
    elif format_lower == 'tar':
        reader_class = TarReader
    elif format_lower == '7z':
        reader_class = SevenZipReader
    elif format_lower == 'rar':
        reader_class = RarReader
    else:
        _print_error(f"Unsupported format for properties command: {format}", exit_code=1)
        return
    
    try:
        with reader_class(archive) as z:
            entries = sorted(z.list())
            
            # Build properties dictionary
            properties = {
                "archive": str(archive),
                "format": format.upper(),
                "total_entries": len(entries),
                "entries": []
            }
            
            # Add archive comment if available (ZIP format only)
            if format_lower == 'zip' and hasattr(z, 'get_archive_comment'):
                archive_comment = z.get_archive_comment()
                if archive_comment:
                    try:
                        properties["archive_comment"] = archive_comment.decode("utf-8", errors="replace")
                    except Exception:
                        properties["archive_comment"] = f"{len(archive_comment)} bytes (binary)"
            
            # Add entry information
            for name in entries:
                info = z.get_info(name)
                if info is None:
                    continue
                
                entry_props = {
                    "name": name,
                    "size": getattr(info, 'uncompressed_size', getattr(info, 'size', 0)),
                    "compressed_size": getattr(info, 'compressed_size', getattr(info, 'size', 0)),
                }
                
                # Add compression method
                method = getattr(info, 'compression_method', getattr(info, 'compression', None))
                if method is not None:
                    entry_props["compression_method"] = str(method) if isinstance(method, (int, str)) else method
                
                # Add comment if present (ZIP format only)
                if hasattr(info, 'comment') and info.comment:
                    try:
                        if isinstance(info.comment, bytes):
                            entry_props["comment"] = info.comment.decode("utf-8", errors="replace")
                        else:
                            entry_props["comment"] = str(info.comment)
                    except Exception:
                        entry_props["comment"] = f"{len(info.comment)} bytes (binary)"
                
                # Add timestamp if available
                if hasattr(info, 'mtime'):
                    mtime = info.mtime
                    if isinstance(mtime, datetime):
                        entry_props["mtime"] = mtime.isoformat()
                    elif isinstance(mtime, (int, float)):
                        entry_props["mtime"] = datetime.fromtimestamp(mtime).isoformat()
                
                # Add mode/permissions if available
                if hasattr(info, 'mode'):
                    entry_props["mode"] = oct(info.mode) if isinstance(info.mode, int) else info.mode
                
                # Add directory flag if available
                if hasattr(info, 'is_directory'):
                    entry_props["is_directory"] = info.is_directory
                elif hasattr(info, 'type'):
                    entry_props["is_directory"] = (info.type == b'5' or getattr(info, 'type', None) == 'directory')
                
                properties["entries"].append(entry_props)
            
            # Output as JSON
            print(json.dumps(properties, indent=2, ensure_ascii=False))
    except Exception as e:
        _print_error(f"Failed to read archive: {e}", exit_code=1)


def _cmd_statistics(archive: Path) -> None:
    """Print comprehensive statistics about an archive.
    
    Args:
        archive: Path to the archive to analyze.
    """
    try:
        stats = get_archive_statistics(archive)
    except Exception as e:
        _print_error(f"Failed to get archive statistics: {e}", exit_code=1)
        return
    
    # Format file sizes for display
    def format_size(size_bytes: int) -> str:
        """Format size in bytes to human-readable format."""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size_bytes < 1024.0:
                return f"{size_bytes:.2f} {unit}"
            size_bytes /= 1024.0
        return f"{size_bytes:.2f} PB"
    
    # Print statistics
    print("=" * 80)
    print(f"Archive Statistics: {archive}")
    print("=" * 80)
    print()
    
    # Basic information
    print("Basic Information:")
    print(f"  Format: {stats['format'].upper()}")
    print(f"  Total Entries: {stats['total_entries']}")
    print(f"  Files: {stats['file_count']}")
    print(f"  Directories: {stats['directory_count']}")
    print()
    
    # Size information
    print("Size Information:")
    print(f"  Total Uncompressed Size: {format_size(stats['total_uncompressed_size'])} ({stats['total_uncompressed_size']:,} bytes)")
    print(f"  Total Compressed Size: {format_size(stats['total_compressed_size'])} ({stats['total_compressed_size']:,} bytes)")
    print(f"  Space Saved: {format_size(stats['space_saved'])} ({stats['space_saved']:,} bytes)")
    print(f"  Space Saved: {stats['space_saved_percent']:.2f}%")
    print(f"  Compression Ratio: {stats['compression_ratio']:.4f} ({stats['compression_ratio']*100:.2f}%)")
    if stats['file_count'] > 0:
        print(f"  Average File Size: {format_size(int(stats['average_file_size']))} ({int(stats['average_file_size']):,} bytes)")
    print()
    
    # Compression methods
    if stats['compression_methods']:
        print("Compression Methods:")
        for method, count in sorted(stats['compression_methods'].items()):
            method_size = stats['compression_method_sizes'].get(method, 0)
            print(f"  {method}: {count} files ({format_size(method_size)})")
        print()
    
    # Largest/smallest files
    if 'largest_file' in stats:
        print("File Size Extremes:")
        print(f"  Largest File: {stats['largest_file']['name']}")
        print(f"    Size: {format_size(stats['largest_file']['size'])} ({stats['largest_file']['size']:,} bytes)")
        if 'smallest_file' in stats:
            print(f"  Smallest File: {stats['smallest_file']['name']}")
            print(f"    Size: {format_size(stats['smallest_file']['size'])} ({stats['smallest_file']['size']:,} bytes)")
        print()
    
    # Encryption information
    if stats['encrypted_count'] > 0:
        print("Encryption:")
        print(f"  Encrypted Entries: {stats['encrypted_count']}")
        print()
    
    # Archive comment
    if stats['has_comment']:
        print("Archive Comment:")
        print(f"  Comment Length: {stats['archive_comment_length']} bytes")
        print()
    
    print("=" * 80)


def _cmd_search(
    archive: Path,
    pattern: str,
    use_regex: bool = False,
    case_sensitive: bool = True,
    format: Optional[str] = None,
) -> None:
    """Search for files within an archive matching a pattern.
    
    Args:
        archive: Path to the archive to search.
        pattern: Pattern to search for (glob pattern by default, regex if --regex is used).
        use_regex: If True, treat pattern as a regular expression.
        case_sensitive: If True (default), pattern matching is case-sensitive.
        format: Optional archive format (auto-detected if not specified).
    """
    from .utils import search_archive, detect_archive_format
    
    # Determine reader class based on format
    reader_class = None
    if format:
        format_lower = format.lower()
        if format_lower == 'zip':
            from .reader import ZipReader
            reader_class = ZipReader
        elif format_lower == 'tar':
            from .tar_reader import TarReader
            reader_class = TarReader
        elif format_lower == '7z':
            from .sevenz_reader import SevenZipReader
            reader_class = SevenZipReader
        elif format_lower == 'rar':
            from .rar_reader import RarReader
            reader_class = RarReader
        else:
            _print_error(f"Unsupported format: {format}", exit_code=1)
            return
    else:
        # Auto-detect format
        detected_format = detect_archive_format(archive)
        if detected_format == 'zip':
            from .reader import ZipReader
            reader_class = ZipReader
        elif detected_format == 'tar':
            from .tar_reader import TarReader
            reader_class = TarReader
        elif detected_format == '7z':
            from .sevenz_reader import SevenZipReader
            reader_class = SevenZipReader
        elif detected_format == 'rar':
            from .rar_reader import RarReader
            reader_class = RarReader
        else:
            _print_error(
                f"Could not detect archive format for {archive}. "
                "Please specify format using --format option.",
                exit_code=1
            )
            return
    
    try:
        # Search archive
        results = search_archive(
            archive_path=archive,
            pattern=pattern,
            use_regex=use_regex,
            case_sensitive=case_sensitive,
            reader_class=reader_class,
        )
        
        # Print results
        if not results:
            print(f"No entries found matching pattern: {pattern}")
            return
        
        print(f"Found {len(results)} matching entries:")
        print()
        print(f"{'Name':<60} {'Size':>12} {'Compressed':>12} {'Type':<10}")
        print("-" * 100)
        
        for result in results:
            name = result['name']
            size = result.get('size', 0)
            compressed_size = result.get('compressed_size', 0)
            entry_type = "Directory" if result.get('is_directory', False) else "File"
            
            # Truncate long names for display
            if len(name) > 58:
                name = name[:55] + "..."
            
            print(f"{name:<60} {size:>12} {compressed_size:>12} {entry_type:<10}")
        
        print("-" * 100)
        print(f"Total: {len(results)} entries")
        
    except Exception as e:
        _print_error(f"Search failed: {e}", exit_code=1)


def _cmd_search_content(
    archive: Path,
    search_text: str,
    filename_pattern: Optional[str] = None,
    use_regex: bool = False,
    case_sensitive: bool = True,
    text_encoding: str = 'utf-8',
    binary_mode: bool = False,
    max_file_size: Optional[int] = None,
    format: Optional[str] = None,
    quiet: bool = False,
) -> None:
    """Search for text or binary patterns within archive file contents.
    
    Args:
        archive: Path to the archive to search.
        search_text: Text or bytes pattern to search for within file contents.
        filename_pattern: Optional glob pattern to filter which files to search.
        use_regex: If True, treat search_text as a regular expression.
        case_sensitive: If True (default), search is case-sensitive.
        text_encoding: Text encoding to use when reading file contents (default: 'utf-8').
        binary_mode: If True, search_text must be bytes and search is performed in binary mode.
        max_file_size: Maximum file size in bytes to search (default: None, no limit).
        format: Optional archive format (auto-detected if not specified).
        quiet: If True, suppress progress output.
    """
    from .utils import search_archive_content, detect_archive_format
    
    # Determine reader class based on format
    reader_class = None
    if format:
        format_lower = format.lower()
        if format_lower == 'zip':
            from .reader import ZipReader
            reader_class = ZipReader
        elif format_lower == 'tar':
            from .tar_reader import TarReader
            reader_class = TarReader
        elif format_lower == '7z':
            from .sevenz_reader import SevenZipReader
            reader_class = SevenZipReader
        elif format_lower == 'rar':
            from .rar_reader import RarReader
            reader_class = RarReader
        else:
            _print_error(f"Unsupported format: {format}", exit_code=1)
            return
    else:
        # Auto-detect format
        detected_format = detect_archive_format(archive)
        if detected_format == 'zip':
            from .reader import ZipReader
            reader_class = ZipReader
        elif detected_format == 'tar':
            from .tar_reader import TarReader
            reader_class = TarReader
        elif detected_format == '7z':
            from .sevenz_reader import SevenZipReader
            reader_class = SevenZipReader
        elif detected_format == 'rar':
            from .rar_reader import RarReader
            reader_class = RarReader
        else:
            _print_error(
                f"Could not detect archive format for {archive}. "
                "Please specify format using --format option.",
                exit_code=1
            )
            return
    
    # Convert search_text to bytes if binary_mode
    if binary_mode:
        try:
            # Try to interpret as hex if it starts with 0x or contains only hex chars
            if search_text.startswith('0x') or search_text.startswith('\\x'):
                # Hex string
                search_text_bytes = bytes.fromhex(search_text.replace('0x', '').replace('\\x', '').replace(' ', ''))
            else:
                # Try to encode as bytes
                search_text_bytes = search_text.encode('latin-1')
            search_text = search_text_bytes
        except Exception as e:
            _print_error(f"Invalid binary search pattern: {e}", exit_code=1)
            return
    
    # Progress callback
    def progress_callback(entry_name, current, total):
        if not quiet:
            print(f"Searching: {entry_name} ({current}/{total})", end='\r', file=sys.stderr)
            sys.stderr.flush()
    
    try:
        # Search archive contents
        results = search_archive_content(
            archive_path=archive,
            search_text=search_text,
            filename_pattern=filename_pattern,
            use_regex=use_regex,
            case_sensitive=case_sensitive,
            text_encoding=text_encoding,
            binary_mode=binary_mode,
            max_file_size=max_file_size,
            reader_class=reader_class,
            progress_callback=progress_callback if not quiet else None,
        )
        
        if not quiet:
            print(file=sys.stderr)  # New line after progress
        
        # Print results
        if not results:
            print(f"No matches found for: {search_text}")
            return
        
        print(f"Found {len(results)} file(s) with matches:")
        print()
        
        for result in results:
            print(f"File: {result['name']}")
            print(f"  Matches: {result['match_count']}")
            print(f"  Size: {result['size']} bytes")
            
            # Show first few matches with context
            matches_to_show = min(5, len(result['matches']))
            for i, match in enumerate(result['matches'][:matches_to_show]):
                if match['line_number'] is not None:
                    # Text mode match
                    print(f"  Match {i+1}: Line {match['line_number']}, Column {match['column']}")
                    if match['context']:
                        print(f"    Context: {match['context']}")
                else:
                    # Binary mode match
                    print(f"  Match {i+1}: Offset {match['offset']}")
            
            if len(result['matches']) > matches_to_show:
                print(f"  ... and {len(result['matches']) - matches_to_show} more matches")
            print()
        
        # Summary
        total_matches = sum(r['match_count'] for r in results)
        print(f"Total: {total_matches} matches in {len(results)} file(s)")
        
    except Exception as e:
        _print_error(f"Content search failed: {e}", exit_code=1)


def _cmd_convert(
    source: Path,
    target: Path,
    source_format: Optional[str] = None,
    target_format: Optional[str] = None,
    compression: Optional[str] = None,
    compression_level: Optional[int] = None,
    password: Optional[str] = None,
    password_file: Optional[Path] = None,
    preserve_metadata: bool = True,
    overwrite: bool = False,
    use_external_tool_for_rar: bool = False,
    external_tool: Optional[str] = None,
) -> None:
    """Convert an archive from one format to another.
    
    Args:
        source: Path to the source archive file.
        target: Path to the target archive file to create.
        source_format: Optional source format name (auto-detected if not specified).
        target_format: Optional target format name (auto-detected from extension if not specified).
        compression: Optional compression method for target archive (ZIP format only).
        compression_level: Optional compression level (0-9, format-dependent).
        password: Optional password for encrypted source archives (ZIP only, or RAR if use_external_tool_for_rar is True).
        password_file: Optional path to file containing password.
        preserve_metadata: If True (default), preserve file metadata (timestamps, permissions).
        overwrite: If True, overwrite target file if it already exists (default: False).
        use_external_tool_for_rar: If True, use external tools to extract compressed RAR entries (RAR source format only).
        external_tool: Optional tool name ('unrar', '7z', or 'unar') when use_external_tool_for_rar is True.
    """
    # Check if target file exists
    if target.exists() and not overwrite:
        _print_error(
            f"Target file already exists: {target}\n"
            "Use --overwrite to overwrite existing files.",
            exit_code=1
        )
        return
    
    # Get password if provided
    password_bytes = _get_password(password, password_file)
    
    # Create progress callback
    def progress_callback(entry_name: str, current: int, total: int) -> None:
        """Progress callback for conversion."""
        print(f"Converting: {entry_name} ({current}/{total})", end='\r')
    
    try:
        # Perform conversion
        stats = convert_archive(
            source_path=source,
            target_path=target,
            source_format=source_format,
            target_format=target_format,
            compression=compression,
            compression_level=compression_level,
            password=password_bytes,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback,
            use_external_tool_for_rar=use_external_tool_for_rar,
            external_tool=external_tool,
        )
        
        # Print conversion results
        print()  # New line after progress output
        print(f"Conversion completed successfully!")
        print(f"Source format: {stats['source_format']}")
        print(f"Target format: {stats['target_format']}")
        print(f"Entries converted: {stats['entries_converted']}")
        if stats['entries_skipped'] > 0:
            print(f"Entries skipped: {stats['entries_skipped']}")
        if stats['errors']:
            print(f"Errors encountered: {len(stats['errors'])}")
            for error in stats['errors']:
                print(f"  - {error}")
        
        # Format total size
        total_size = stats['total_size']
        if total_size < 1024:
            size_str = f"{total_size} B"
        elif total_size < 1024 * 1024:
            size_str = f"{total_size / 1024:.2f} KB"
        elif total_size < 1024 * 1024 * 1024:
            size_str = f"{total_size / (1024 * 1024):.2f} MB"
        else:
            size_str = f"{total_size / (1024 * 1024 * 1024):.2f} GB"
        
        print(f"Total size converted: {size_str}")
        
    except Exception as e:
        _print_error(f"Conversion failed: {e}", exit_code=1)


def _cmd_extract(
    archive: Path,
    output_dir: Path,
    quiet: bool = False,
    allow_absolute_paths: bool = False,
    max_path_length: Optional[int] = None,
    password: Optional[str] = None,
    password_file: Optional[Path] = None,
) -> None:
    """
    Extract all entries in *archive* into *output_dir*.

    Directory entries are created as directories; file entries are written
    with their relative paths preserved.
    
    Args:
        archive: Path to the archive to extract.
        output_dir: Target directory for extraction.
        quiet: If True, suppress progress output.
        allow_absolute_paths: If True, allow absolute paths in entry names (default: False).
        max_path_length: Maximum allowed path length (default: None, no limit).
        password: Optional password for encrypted entries (str).
        password_file: Optional path to file containing password.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Get password if provided
    password_bytes = _get_password(password, password_file)

    # Count files for progress reporting
    try:
        with ZipReader(archive, password=password_bytes) as z_temp:
            file_entries = list(z_temp.iter_files())
            total_files = len(file_entries)
    except ZipPasswordError as e:
        _print_error(f"Password error: {e}", exit_code=1)
    except ZipEncryptionError as e:
        _print_error(f"Encryption error: {e}", exit_code=1)
    
    # Create progress callback
    progress_callback = create_progress_callback(total_files=total_files, quiet=quiet)
    
    try:
        with ZipReader(archive, progress_callback=progress_callback, password=password_bytes) as z:
            for name in z.list():
                info = z.get_info(name)
                if info is None:
                    continue

                # Compute safe target path with security validation
                target_path = safe_extract_path(
                    output_dir,
                    name,
                    allow_absolute_paths=allow_absolute_paths,
                    max_path_length=max_path_length,
                )

                if info.is_dir:
                    target_path.mkdir(parents=True, exist_ok=True)
                    continue

                target_path.parent.mkdir(parents=True, exist_ok=True)

                try:
                    with z.open(name) as src, open(target_path, "wb") as dst:
                        # Stream copy in chunks to support large files
                        while True:
                            chunk = src.read(1024 * 1024)
                            if not chunk:
                                break
                            dst.write(chunk)
                            if progress_callback:
                                progress_callback(name, dst.tell(), info.uncompressed_size)
                except ZipPasswordError as e:
                    _print_error(f"Password error extracting '{name}': {e}", exit_code=1)
                except ZipEncryptionError as e:
                    _print_error(f"Encryption error extracting '{name}': {e}", exit_code=1)
    except ZipPasswordError as e:
        _print_error(f"Password error: {e}", exit_code=1)
    except ZipEncryptionError as e:
        _print_error(f"Encryption error: {e}", exit_code=1)
        for name in z.list():
            info = z.get_info(name)
            if info is None:
                continue

            # Compute safe target path with security validation
            target_path = safe_extract_path(
                output_dir,
                name,
                allow_absolute_paths=allow_absolute_paths,
                max_path_length=max_path_length,
            )

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


def _cmd_extract_filtered(
    archive: Path,
    output_dir: Path,
    include_patterns: Optional[List[str]] = None,
    exclude_patterns: Optional[List[str]] = None,
    include_extensions: Optional[List[str]] = None,
    exclude_extensions: Optional[List[str]] = None,
    min_size: Optional[int] = None,
    max_size: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    case_sensitive: bool = False,
    allow_absolute_paths: bool = False,
    max_path_length: Optional[int] = None,
    password: Optional[str] = None,
    password_file: Optional[Path] = None,
    quiet: bool = False,
) -> None:
    """
    Extract entries from archive matching filter criteria.
    
    Args:
        archive: Path to the archive.
        output_dir: Target directory for extraction.
        include_patterns: Glob patterns to include.
        exclude_patterns: Glob patterns to exclude.
        include_extensions: File extensions to include (with dot, e.g., '.txt').
        exclude_extensions: File extensions to exclude.
        min_size: Minimum file size in bytes.
        max_size: Maximum file size in bytes.
        start_date: Start date string (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS).
        end_date: End date string (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS).
        case_sensitive: Use case-sensitive pattern matching.
        allow_absolute_paths: Allow absolute paths in entry names.
        max_path_length: Maximum allowed path length.
        password: Password for encrypted archives.
        password_file: Path to file containing password.
        quiet: Suppress progress output.
    """
    from datetime import datetime
    from .utils import extract_with_filter
    
    # Parse dates if provided
    start_dt = None
    end_dt = None
    
    if start_date:
        try:
            # Try parsing with time first
            start_dt = datetime.strptime(start_date, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                # Try parsing date only
                start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            except ValueError:
                _print_error(f"Invalid start date format: {start_date}. Use YYYY-MM-DD or YYYY-MM-DD HH:MM:SS", exit_code=1)
    
    if end_date:
        try:
            # Try parsing with time first
            end_dt = datetime.strptime(end_date, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                # Try parsing date only
                end_dt = datetime.strptime(end_date, "%Y-%m-%d")
                # Set to end of day
                end_dt = end_dt.replace(hour=23, minute=59, second=59)
            except ValueError:
                _print_error(f"Invalid end date format: {end_date}. Use YYYY-MM-DD or YYYY-MM-DD HH:MM:SS", exit_code=1)
    
    # Get password if provided
    password_bytes = _get_password(password, password_file)
    
    # Create progress callback
    def progress_callback(entry_name: str, bytes_extracted: int, total_bytes: int) -> None:
        if not quiet:
            percent = (bytes_extracted / total_bytes * 100) if total_bytes > 0 else 0
            print(f"Extracting {entry_name}: {bytes_extracted}/{total_bytes} bytes ({percent:.1f}%)", end='\r')
    
    # Call extract_with_filter utility
    try:
        result = extract_with_filter(
            archive_path=archive,
            output_dir=output_dir,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            include_extensions=include_extensions,
            exclude_extensions=exclude_extensions,
            min_size=min_size,
            max_size=max_size,
            start_date=start_dt,
            end_date=end_dt,
            case_sensitive=case_sensitive,
            allow_absolute_paths=allow_absolute_paths,
            max_path_length=max_path_length,
            password=password_bytes,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print summary
        if not quiet:
            print()  # New line after progress output
            print(f"Extraction complete:")
            print(f"  Total entries: {result['total_entries']}")
            print(f"  Matched entries: {result['matched_entries']}")
            print(f"  Extracted entries: {result['extracted_entries']}")
            print(f"  Skipped entries: {result['skipped_entries']}")
            if result['failed_entries'] > 0:
                print(f"  Failed entries: {result['failed_entries']}")
                for failed in result['failed_files']:
                    print(f"    - {failed['entry_name']}: {failed['error']}")
    
    except Exception as e:
        _print_error(f"Extraction failed: {e}", exit_code=1)


def _cmd_extract_with_conflict_resolution(
    archive: Path,
    output_dir: Path,
    conflict_strategy: str = 'rename',
    allow_absolute_paths: bool = False,
    max_path_length: Optional[int] = None,
    password: Optional[str] = None,
    password_file: Optional[Path] = None,
    quiet: bool = False,
) -> None:
    """Extract archive entries with intelligent conflict resolution.
    
    Args:
        archive: Path to the archive file.
        output_dir: Target directory for extraction.
        conflict_strategy: Strategy for handling file conflicts (overwrite, skip, rename, timestamp, size).
        allow_absolute_paths: Allow absolute paths in entry names (default: False).
        max_path_length: Maximum allowed path length (default: None, no limit).
        password: Password for encrypted archives.
        password_file: File containing password for encrypted archives.
        quiet: Suppress progress output.
    """
    from .utils import extract_with_conflict_resolution, detect_archive_format
    
    # Detect format
    format = detect_archive_format(archive)
    if format is None:
        _print_error("Could not detect archive format. Please specify format manually.", exit_code=2)
    
    # Select reader class based on format
    reader_class_map = {
        'zip': ZipReader,
        'tar': TarReader,
        '7z': SevenZipReader,
        'rar': RarReader,
    }
    
    reader_class = reader_class_map.get(format)
    if reader_class is None:
        _print_error(f"Unsupported format for extraction: {format}", exit_code=2)
    
    # Get password if provided
    password_bytes = None
    if password_file:
        try:
            password_bytes = Path(password_file).read_bytes().strip()
        except Exception as e:
            _print_error(f"Failed to read password file: {e}", exit_code=1)
    elif password:
        password_bytes = password.encode('utf-8')
    
    # Create progress callback
    def progress_callback(entry_name: str, bytes_extracted: int, total_bytes: int, action: str) -> None:
        if not quiet:
            percent = (bytes_extracted / total_bytes * 100) if total_bytes > 0 else 0
            action_symbol = {
                'extracted': '✓',
                'skipped': '⊘',
                'renamed': '↻',
                'overwritten': '↻',
                'failed': '✗',
            }.get(action, '•')
            print(f"  [{action_symbol}] [{bytes_extracted}/{total_bytes}] ({percent:.1f}%) {entry_name}", end='\r')
    
    # Call extract_with_conflict_resolution utility
    try:
        result = extract_with_conflict_resolution(
            archive_path=archive,
            output_dir=output_dir,
            reader_class=reader_class,
            conflict_strategy=conflict_strategy,
            allow_absolute_paths=allow_absolute_paths,
            max_path_length=max_path_length,
            password=password_bytes,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print summary
        if not quiet:
            print()  # New line after progress output
            print(f"Extraction complete:")
            print(f"  Total entries: {result['total_entries']}")
            print(f"  Extracted entries: {result['extracted_entries']}")
            if result['skipped_entries'] > 0:
                print(f"  Skipped entries: {result['skipped_entries']}")
            if result['renamed_entries'] > 0:
                print(f"  Renamed entries: {result['renamed_entries']}")
            if result['overwritten_entries'] > 0:
                print(f"  Overwritten entries: {result['overwritten_entries']}")
            if result['failed_entries'] > 0:
                print(f"  Failed entries: {result['failed_entries']}")
                for failed in result['failed_files']:
                    print(f"    - {failed['entry_name']}: {failed['error']}")
            
            # Show renamed files if any
            if result['renamed_files']:
                print("\nRenamed files:")
                for rename_info in result['renamed_files'][:10]:  # Show first 10
                    print(f"  {rename_info['original']} -> {rename_info['renamed']}")
                if len(result['renamed_files']) > 10:
                    print(f"  ... and {len(result['renamed_files']) - 10} more")
    
    except Exception as e:
        _print_error(f"Extraction failed: {e}", exit_code=1)


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


def _parse_size(size_str: str) -> int:
    """
    Parse a size string (e.g., "64MB", "100KB", "1GB") into bytes.
    
    Args:
        size_str: Size string with optional suffix (KB, MB, GB).
        
    Returns:
        Size in bytes.
        
    Raises:
        ValueError: If size string is invalid.
    """
    size_str = size_str.strip().upper()
    
    # Try to parse as integer first
    try:
        return int(size_str)
    except ValueError:
        pass
    
    # Parse with suffix
    if size_str.endswith("KB"):
        return int(size_str[:-2]) * 1024
    elif size_str.endswith("MB"):
        return int(size_str[:-2]) * 1024 * 1024
    elif size_str.endswith("GB"):
        return int(size_str[:-2]) * 1024 * 1024 * 1024
    else:
        raise ValueError(f"Invalid size format: {size_str} (expected number or number with KB/MB/GB suffix)")


def _cmd_create(
    archive: Path,
    sources: List[Path],
    compression: str,
    comment: str = "",
    compression_level: int = 6,
    quiet: bool = False,
    split_size: Optional[str] = None,
    threads: int = 1,
    password: Optional[str] = None,
    password_file: Optional[Path] = None,
    aes_version: int = 1,
) -> None:
    """
    Create *archive* from the given list of source paths.

    - *sources* may contain files and directories.
    - Directory trees are preserved inside the archive.
    - *compression* is one of: ``stored``, ``deflate``, ``bzip2``, ``lzma``, ``ppmd`` (PPMd not yet implemented).
    - *comment* is the archive comment (optional).
    - *compression_level* is the compression level (0-9 for DEFLATE/LZMA, 1-9 for BZIP2, default: 6 for DEFLATE/LZMA, 9 for BZIP2).
    - *quiet* suppresses progress output if True.
    - *split_size* is optional maximum size for each split archive part (e.g., "64MB", "100KB", "1GB").
        If specified, creates a split archive with multiple part files (.z01, .z02, ..., .zip).
    - *threads* is the number of threads to use for parallel compression (default: 1).
        Multi-threading is disabled with split archives.
    - *password* is optional password for encryption (str).
    - *password_file* is optional path to file containing password.
    - *aes_version* is AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1).
    """
    if archive.exists():
        _print_error(f"Refusing to overwrite existing archive: {archive}", exit_code=2)

    files = list(_iter_files_for_create(sources))
    if not files:
        _print_error("No files found to add to archive", exit_code=2)

    # Get password if provided
    password_bytes = _get_password(password, password_file)
    if password_bytes is not None:
        # Validate AES version
        if aes_version not in (1, 2, 3):
            _print_error(f"Invalid AES version: {aes_version} (must be 1=AES-128, 2=AES-192, or 3=AES-256)", exit_code=2)

    # Parse split_size if specified
    split_size_bytes = None
    if split_size:
        try:
            split_size_bytes = _parse_size(split_size)
        except ValueError as e:
            _print_error(f"Invalid split size: {e}", exit_code=2)

    # Create progress callback
    total_files = len(files)
    progress_callback = create_progress_callback(total_files=total_files, quiet=quiet)

    try:
        with ZipWriter(archive, archive_comment=comment, progress_callback=progress_callback, split_size=split_size_bytes, threads=threads) as z:
            for name_in_zip, src_path in files:
                z.add_file(
                    name_in_zip,
                    str(src_path),
                    compression=compression,
                    compression_level=compression_level,
                    password=password_bytes,
                    aes_version=aes_version,
                )
    except ZipEncryptionError as e:
        _print_error(f"Encryption error: {e}", exit_code=1)


def _cmd_gzip_compress(input_file: Path, output_file: Path, filename: Optional[str] = None, comment: Optional[str] = None, compression_level: int = 6) -> None:
    """Compress a file using GZIP format.
    
    Args:
        input_file: Path to the file to compress.
        output_file: Path to the output GZIP file (.gz).
        filename: Optional original filename to store in GZIP header.
        comment: Optional comment to store in GZIP header.
        compression_level: Compression level (0-9, default: 6).
    """
    if not input_file.exists():
        _print_error(f"Input file not found: {input_file}", exit_code=2)
    
    if output_file.exists():
        _print_error(f"Refusing to overwrite existing file: {output_file}", exit_code=2)
    
    # Use input filename if not specified
    if filename is None:
        filename = input_file.name
    
    try:
        with open(input_file, "rb") as src, GzipWriter(output_file, filename=filename, comment=comment, compression_level=compression_level) as gz:
            # Stream copy in chunks to support large files
            while True:
                chunk = src.read(1024 * 1024)  # Read in 1MB chunks
                if not chunk:
                    break
                gz.write(chunk)
    except Exception as e:
        _print_error(f"GZIP compression failed: {e}", exit_code=1)


def _cmd_gzip_decompress(input_file: Path, output_file: Optional[Path] = None, skip_crc: bool = False) -> None:
    """Decompress a GZIP file.
    
    Args:
        input_file: Path to the GZIP file to decompress (.gz).
        output_file: Optional path to the output file. If None, uses input filename without .gz extension.
        skip_crc: If True, skip CRC32 verification.
    """
    if not input_file.exists():
        _print_error(f"Input file not found: {input_file}", exit_code=2)
    
    # Determine output file path
    if output_file is None:
        # Remove .gz extension if present
        output_name = input_file.name
        if output_name.lower().endswith('.gz'):
            output_name = output_name[:-3]
        output_file = input_file.parent / output_name
    
    if output_file.exists():
        _print_error(f"Refusing to overwrite existing file: {output_file}", exit_code=2)
    
    crc_mode = "skip" if skip_crc else "strict"
    
    try:
        with GzipReader(input_file, crc_verification=crc_mode) as gz:
            data = gz.read()
            with open(output_file, "wb") as dst:
                dst.write(data)
    except Exception as e:
        _print_error(f"GZIP decompression failed: {e}", exit_code=1)


def _cmd_bzip2_compress(input_file: Path, output_file: Path, compression_level: int = 9) -> None:
    """Compress a file using BZIP2 format.
    
    Args:
        input_file: Path to the file to compress.
        output_file: Path to the output BZIP2 file (.bz2).
        compression_level: Compression level (1-9, default: 9).
    """
    if not input_file.exists():
        _print_error(f"Input file not found: {input_file}", exit_code=2)
    
    if output_file.exists():
        _print_error(f"Refusing to overwrite existing file: {output_file}", exit_code=2)
    
    try:
        with open(input_file, "rb") as src, Bzip2Writer(output_file, compression_level=compression_level) as bz2:
            # Stream copy in chunks to support large files
            while True:
                chunk = src.read(1024 * 1024)  # Read in 1MB chunks
                if not chunk:
                    break
                bz2.write(chunk)
    except Exception as e:
        _print_error(f"BZIP2 compression failed: {e}", exit_code=1)


def _cmd_bzip2_decompress(input_file: Path, output_file: Optional[Path] = None) -> None:
    """Decompress a BZIP2 file.
    
    Args:
        input_file: Path to the BZIP2 file to decompress (.bz2).
        output_file: Optional path to the output file. If None, uses input filename without .bz2 extension.
    """
    if not input_file.exists():
        _print_error(f"Input file not found: {input_file}", exit_code=2)
    
    # Determine output file path
    if output_file is None:
        # Remove .bz2 extension if present
        output_name = input_file.name
        if output_name.lower().endswith('.bz2'):
            output_name = output_name[:-4]
        output_file = input_file.parent / output_name
    
    if output_file.exists():
        _print_error(f"Refusing to overwrite existing file: {output_file}", exit_code=2)
    
    try:
        with Bzip2Reader(input_file) as bz2:
            data = bz2.read()
            with open(output_file, "wb") as dst:
                dst.write(data)
    except Exception as e:
        _print_error(f"BZIP2 decompression failed: {e}", exit_code=1)


def _cmd_xz_compress(input_file: Path, output_file: Path, compression_level: int = 6) -> None:
    """Compress a file using XZ format.
    
    Args:
        input_file: Path to the file to compress.
        output_file: Path to the output XZ file (.xz).
        compression_level: Compression level (0-9, default: 6).
    """
    if not input_file.exists():
        _print_error(f"Input file not found: {input_file}", exit_code=2)
    
    if output_file.exists():
        _print_error(f"Refusing to overwrite existing file: {output_file}", exit_code=2)
    
    try:
        with open(input_file, "rb") as src, XzWriter(output_file, compression_level=compression_level) as xz:
            # Stream copy in chunks to support large files
            while True:
                chunk = src.read(1024 * 1024)  # Read in 1MB chunks
                if not chunk:
                    break
                xz.write(chunk)
    except Exception as e:
        _print_error(f"XZ compression failed: {e}", exit_code=1)


def _cmd_xz_decompress(input_file: Path, output_file: Optional[Path] = None) -> None:
    """Decompress an XZ file.
    
    Args:
        input_file: Path to the XZ file to decompress (.xz).
        output_file: Optional path to the output file. If None, uses input filename without .xz extension.
    """
    if not input_file.exists():
        _print_error(f"Input file not found: {input_file}", exit_code=2)
    
    # Determine output file path
    if output_file is None:
        # Remove .xz extension if present
        output_name = input_file.name
        if output_name.lower().endswith('.xz'):
            output_name = output_name[:-3]
        output_file = input_file.parent / output_name
    
    if output_file.exists():
        _print_error(f"Refusing to overwrite existing file: {output_file}", exit_code=2)
    
    try:
        with XzReader(input_file) as xz:
            data = xz.read()
            with open(output_file, "wb") as dst:
                dst.write(data)
    except Exception as e:
        _print_error(f"XZ decompression failed: {e}", exit_code=1)


def _cmd_tar_create(archive: Path, sources: List[Path]) -> None:
    """Create a TAR archive from files and directories.
    
    Args:
        archive: Path to the output TAR archive (.tar).
        sources: List of files and directories to add to the archive.
    """
    if archive.exists():
        _print_error(f"Refusing to overwrite existing file: {archive}", exit_code=2)
    
    try:
        with TarWriter(archive) as tar:
            for source in sources:
                if not source.exists():
                    _print_error(f"Source not found: {source}", exit_code=2)
                
                if source.is_file():
                    # Add file
                    tar.add_file(str(source), str(source))
                elif source.is_dir():
                    # Add directory entry
                    dir_name = str(source.name) + '/'
                    tar.add_directory(dir_name)
                    
                    # Recursively add files in directory
                    for root, dirs, files in os.walk(source):
                        # Add subdirectories
                        for d in dirs:
                            rel_path = os.path.join(root, d)
                            rel_name = os.path.relpath(rel_path, source.parent)
                            tar.add_directory(rel_name + '/')
                        
                        # Add files
                        for f in files:
                            file_path = os.path.join(root, f)
                            rel_name = os.path.relpath(file_path, source.parent)
                            tar.add_file(str(file_path), rel_name)
    except Exception as e:
        _print_error(f"TAR creation failed: {e}", exit_code=1)


def _cmd_tar_list(archive: Path) -> None:
    """List all entries in a TAR archive.
    
    Args:
        archive: Path to the TAR archive (.tar).
    """
    if not archive.exists():
        _print_error(f"Archive not found: {archive}", exit_code=2)
    
    try:
        with TarReader(archive) as tar:
            for name in sorted(tar.list()):
                print(name)
    except Exception as e:
        _print_error(f"TAR listing failed: {e}", exit_code=1)


def _cmd_tar_extract(
    archive: Path,
    output_dir: Path,
    allow_absolute_paths: bool = False,
    max_path_length: Optional[int] = None,
) -> None:
    """Extract a TAR archive.
    
    Args:
        archive: Path to the TAR archive (.tar).
        output_dir: Directory to extract files to.
        allow_absolute_paths: If True, allow absolute paths in entry names (default: False).
        max_path_length: Maximum allowed path length (default: None, no limit).
    """
    if not archive.exists():
        _print_error(f"Archive not found: {archive}", exit_code=2)
    
    output_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        with TarReader(archive) as tar:
            for entry in tar.iter_entries():
                # Use safe_extract_path for security validation
                # This prevents path traversal attacks and validates entry names
                output_path = safe_extract_path(
                    output_dir,
                    entry.name,
                    allow_absolute_paths=allow_absolute_paths,
                    max_path_length=max_path_length,
                )
                
                # Ensure parent directory exists
                output_path.parent.mkdir(parents=True, exist_ok=True)
                
                if entry.type == b'5':  # Directory
                    output_path.mkdir(parents=True, exist_ok=True)
                elif entry.type == b'2':  # Symbolic link
                    # Create symbolic link
                    try:
                        # Remove existing file/link if it exists
                        if output_path.exists() or output_path.is_symlink():
                            output_path.unlink()
                        # Create symbolic link
                        output_path.symlink_to(entry.linkname)
                    except OSError as e:
                        # On Windows or if symlink creation fails, skip it
                        # (Windows requires special privileges for symlinks)
                        pass
                else:
                    # Extract file (regular file or other types)
                    data = tar.read_entry(entry.name)
                    with open(output_path, "wb") as dst:
                        dst.write(data)
                    
                    # Set file permissions if supported
                    try:
                        os.chmod(output_path, entry.mode)
                    except (OSError, AttributeError):
                        pass  # Ignore if chmod fails or not supported
    except ZipFormatError as e:
        # Re-raise security-related errors with clear message
        _print_error(f"TAR extraction security error: {e}", exit_code=1)
    except Exception as e:
        _print_error(f"TAR extraction failed: {e}", exit_code=1)


def _cmd_7z_list(archive: Path) -> None:
    """List all entries in a 7Z archive.
    
    Args:
        archive: Path to the 7Z archive (.7z).
    """
    if not archive.exists():
        _print_error(f"Archive not found: {archive}", exit_code=2)
    
    try:
        with SevenZipReader(archive) as sz:
            entries = list(sz.iter_entries())
            if not entries:
                print("Archive is empty")
                return
            
            print(f"Archive: {archive}")
            print(f"Entries: {len(entries)}\n")
            print("Name".ljust(60) + "Size".rjust(12) + "Compressed".rjust(12) + "Type".rjust(10))
            print("-" * 94)
            
            for entry in entries:
                size_str = str(entry.size) if not entry.is_directory else "-"
                compressed_str = str(entry.compressed_size) if not entry.is_empty else "-"
                type_str = "DIR" if entry.is_directory else "FILE"
                name = entry.name[:57] + "..." if len(entry.name) > 60 else entry.name
                print(name.ljust(60) + size_str.rjust(12) + compressed_str.rjust(12) + type_str.rjust(10))
    except (SevenZipFormatError, ZipUnsupportedFeature) as e:
        _print_error(f"7Z listing failed: {e}", exit_code=1)
    except Exception as e:
        _print_error(f"7Z listing failed: {e}", exit_code=1)


def _cmd_7z_extract(
    archive: Path,
    output_dir: Path,
    allow_absolute_paths: bool = False,
    max_path_length: Optional[int] = None,
) -> None:
    """Extract a 7Z archive to a directory.
    
    Args:
        archive: Path to the 7Z archive (.7z).
        output_dir: Directory to extract files to.
        allow_absolute_paths: Allow absolute paths in entry names (default: False, for security).
        max_path_length: Maximum allowed path length (default: no limit).
    """
    if not archive.exists():
        _print_error(f"Archive not found: {archive}", exit_code=2)
    
    output_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        with SevenZipReader(archive) as sz:
            for entry in sz.iter_entries():
                # Use safe_extract_path for security validation
                # This prevents path traversal attacks and validates entry names
                output_path = safe_extract_path(
                    output_dir,
                    entry.name,
                    allow_absolute_paths=allow_absolute_paths,
                    max_path_length=max_path_length,
                )
                
                # Ensure parent directory exists
                output_path.parent.mkdir(parents=True, exist_ok=True)
                
                if entry.is_directory:
                    output_path.mkdir(parents=True, exist_ok=True)
                else:
                    # Extract file
                    data = sz.read_entry(entry.name)
                    with open(output_path, "wb") as dst:
                        dst.write(data)
    except (SevenZipFormatError, ZipUnsupportedFeature) as e:
        # Re-raise security-related errors with clear message
        _print_error(f"7Z extraction failed: {e}", exit_code=1)
    except Exception as e:
        _print_error(f"7Z extraction failed: {e}", exit_code=1)


def _cmd_7z_create(
    archive: Path,
    sources: List[Path],
    compression_method: str = "lzma2",
    compression_level: int = 6,
) -> None:
    """Create a 7Z archive from files and directories.
    
    Args:
        archive: Path to the output 7Z archive (.7z).
        sources: List of files and directories to add to the archive.
        compression_method: Compression method to use (copy, lzma, lzma2).
        compression_level: Compression level (0-9).
    """
    try:
        with SevenZipWriter(archive, compression_method=compression_method, compression_level=compression_level) as sz:
            for source in sources:
                if source.is_file():
                    with open(source, "rb") as f:
                        data = f.read()
                    sz.add_bytes(str(source), data)
                elif source.is_dir():
                    # Recursively add directory contents
                    for root, dirs, files in os.walk(source):
                        for dir_name in dirs:
                            dir_path = Path(root) / dir_name
                            rel_path = dir_path.relative_to(source)
                            sz.add_bytes(str(rel_path) + "/", b"", is_directory=True)
                        for file_name in files:
                            file_path = Path(root) / file_name
                            rel_path = file_path.relative_to(source)
                            with open(file_path, "rb") as f:
                                data = f.read()
                            sz.add_bytes(str(rel_path), data)
                else:
                    _print_error(f"Source not found: {source}", exit_code=2)
    except (SevenZipFormatError, ZipUnsupportedFeature) as e:
        _print_error(f"7Z creation failed: {e}", exit_code=1)
    except Exception as e:
        _print_error(f"7Z creation failed: {e}", exit_code=1)


def _cmd_rar_list(archive: Path) -> None:
    """List all entries in a RAR archive.
    
    Args:
        archive: Path to the RAR archive (.rar).
    """
    if not archive.exists():
        _print_error(f"Archive not found: {archive}", exit_code=2)
    
    try:
        with RarReader(archive) as rar:
            entries = list(rar.iter_entries())
            if not entries:
                print("Archive is empty")
                return
            
            # Get compression method statistics
            compression_stats = rar.get_compression_method_statistics()
            
            print(f"Archive: {archive}")
            print(f"Entries: {len(entries)}\n")
            
            # Print compression method summary
            if compression_stats:
                print("Compression Methods:")
                for method_name, stats in sorted(compression_stats.items()):
                    supported = "✅" if method_name == "STORED" else "❌"
                    print(f"  {method_name}: {stats['count']} entries "
                          f"({stats['uncompressed_size']:,} bytes uncompressed, "
                          f"{stats['compressed_size']:,} bytes compressed) {supported}")
                print()
            
            print("Name".ljust(55) + "Size".rjust(12) + "Compressed".rjust(12) + "Method".rjust(12) + "Type".rjust(10))
            print("-" * 101)
            
            for entry in entries:
                size_str = str(entry.size) if not entry.is_directory else "-"
                compressed_str = str(entry.compressed_size) if entry.compressed_size > 0 else "-"
                type_str = "DIR" if entry.is_directory else "FILE"
                
                # Get compression method name
                method_name = rar._get_compression_method_name(entry.compression_method)
                # Truncate method name if too long
                method_str = method_name[:10] if len(method_name) <= 10 else method_name[:7] + "..."
                
                name = entry.name[:52] + "..." if len(entry.name) > 55 else entry.name
                print(name.ljust(55) + size_str.rjust(12) + compressed_str.rjust(12) + 
                      method_str.rjust(12) + type_str.rjust(10))
            
            # Print summary statistics
            total_uncompressed = sum(e.size for e in entries if not e.is_directory)
            total_compressed = sum(e.compressed_size for e in entries if not e.is_directory)
            file_count = sum(1 for e in entries if not e.is_directory)
            dir_count = sum(1 for e in entries if e.is_directory)
            
            if file_count > 0:
                print("-" * 101)
                print(f"Total: {file_count} files, {dir_count} directories")
                print(f"Uncompressed: {total_uncompressed:,} bytes")
                print(f"Compressed: {total_compressed:,} bytes")
                if total_uncompressed > 0:
                    ratio = total_compressed / total_uncompressed
                    print(f"Compression ratio: {ratio:.2%}")
    except (RarFormatError, RarUnsupportedFeature) as e:
        _print_error(f"RAR listing failed: {e}", exit_code=1)
    except Exception as e:
        _print_error(f"RAR listing failed: {e}", exit_code=1)


def _cmd_rar_extract(
    archive: Path,
    output_dir: Path,
    allow_absolute_paths: bool = False,
    max_path_length: Optional[int] = None,
    use_external_tool: bool = False,
    external_tool: Optional[str] = None,
    password: Optional[str] = None,
) -> None:
    """Extract a RAR archive to a directory.
    
    Args:
        archive: Path to the RAR archive (.rar).
        output_dir: Directory to extract files to.
        allow_absolute_paths: Allow absolute paths in entry names (default: False, for security).
        max_path_length: Maximum allowed path length (default: no limit).
        use_external_tool: If True, automatically use external tools when encountering compressed or encrypted entries.
        external_tool: Optional specific tool to use ('unrar', '7z', or 'unar'). Only used if use_external_tool is True.
        password: Optional password for encrypted archives. Only used if use_external_tool is True.
    """
    if not archive.exists():
        _print_error(f"Archive not found: {archive}", exit_code=2)
    
    output_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        with RarReader(archive) as rar:
            # Use extract_all() method which supports external tool fallback
            rar.extract_all(
                output_dir,
                allow_absolute_paths=allow_absolute_paths,
                max_path_length=max_path_length,
                use_external_tool_on_error=use_external_tool,
                external_tool=external_tool,
                password=password,
            )
    except (RarFormatError, RarUnsupportedFeature) as e:
        # Re-raise security-related errors with clear message
        _print_error(f"RAR extraction failed: {e}", exit_code=1)
    except Exception as e:
        _print_error(f"RAR extraction failed: {e}", exit_code=1)


def _cmd_rar_extractable(
    archive: Path,
    include_directories: bool = True,
    detailed: bool = False,
) -> None:
    """List extractable entries in a RAR archive.
    
    This command identifies which entries in a RAR archive can be extracted
    with DNZIP. Only entries using STORED compression (no compression) can
    be extracted. Compressed entries and encrypted entries cannot be extracted.
    
    Args:
        archive: Path to the RAR archive (.rar).
        include_directories: If True (default), include directory entries in the output.
        detailed: If True, show detailed information for each entry.
    """
    if not archive.exists():
        _print_error(f"Archive not found: {archive}", exit_code=2)
    
    try:
        from .utils import get_rar_extractable_entries
        
        result = get_rar_extractable_entries(archive, include_directories=include_directories)
        
        print(f"Archive: {archive}")
        print(f"Total entries: {result['total_entries']}")
        print(f"Extractable entries: {result['extractable_count']}")
        print(f"Non-extractable entries: {result['non_extractable_count']}")
        print(f"Extractability ratio: {result['extractability_ratio']:.1%}\n")
        
        # Print compression method statistics
        stats = result['statistics']
        if stats['compression_methods']:
            print("Compression Methods:")
            for method_name, count in sorted(stats['compression_methods'].items()):
                supported = "✅" if method_name == "STORED" else "❌"
                print(f"  {method_name}: {count} entries {supported}")
            print()
        
        # Print extractable entries
        if result['extractable_entries']:
            print(f"Extractable entries ({len(result['extractable_entries'])}):")
            for entry_name in result['extractable_entries']:
                if detailed:
                    # Find entry detail
                    entry_detail = next(
                        (e for e in result['entry_details'] if e['name'] == entry_name),
                        None
                    )
                    if entry_detail:
                        print(f"  ✅ {entry_name}")
                        print(f"     Type: {'Directory' if entry_detail['is_directory'] else 'File'}")
                        print(f"     Compression: {entry_detail['compression_method_name']}")
                        print(f"     Reason: {entry_detail['reason']}")
                    else:
                        print(f"  ✅ {entry_name}")
                else:
                    print(f"  ✅ {entry_name}")
            print()
        
        # Print non-extractable entries
        if result['non_extractable_entries']:
            print(f"Non-extractable entries ({len(result['non_extractable_entries'])}):")
            for entry_name in result['non_extractable_entries']:
                if detailed:
                    # Find entry detail
                    entry_detail = next(
                        (e for e in result['entry_details'] if e['name'] == entry_name),
                        None
                    )
                    if entry_detail:
                        print(f"  ❌ {entry_name}")
                        print(f"     Type: {'Directory' if entry_detail['is_directory'] else 'File'}")
                        print(f"     Compression: {entry_detail['compression_method_name']}")
                        if entry_detail['is_encrypted']:
                            print(f"     Encrypted: Yes")
                        print(f"     Reason: {entry_detail['reason']}")
                    else:
                        print(f"  ❌ {entry_name}")
                else:
                    print(f"  ❌ {entry_name}")
            print()
        
        # Print warnings if any
        if result['non_extractable_count'] > 0:
            print("Note: Non-extractable entries use unsupported compression methods or encryption.")
            print("To extract these entries, use a tool that supports RAR decompression")
            print("(such as unrar or 7-Zip), then re-compress using a supported format.")
        
    except Exception as e:
        _print_error(f"Failed to analyze RAR archive: {e}", exit_code=1)


def _cmd_rar_extract_external(
    archive: Path,
    output_dir: Optional[Path] = None,
    tool: Optional[str] = None,
    password: Optional[str] = None,
) -> None:
    """Extract RAR archive using external tool (unrar, 7z, or unar).
    
    This command attempts to extract a RAR archive using external tools when
    DNZIP cannot handle compressed or encrypted entries. It automatically
    detects available tools and uses the first one found.
    
    Args:
        archive: Path to the RAR archive (.rar).
        output_dir: Optional destination directory for extraction. If None, extracts
            to archive directory.
        tool: Optional specific tool to use ('unrar', '7z', or 'unar'). If None,
            automatically detects and uses the first available tool.
        password: Optional password for encrypted archives.
    """
    if not archive.exists():
        _print_error(f"Archive not found: {archive}", exit_code=2)
    
    try:
        from .utils import extract_rar_with_external_tool
        
        result = extract_rar_with_external_tool(
            archive,
            extract_path=output_dir,
            tool=tool,
            password=password,
        )
        
        if result['success']:
            print(f"Successfully extracted RAR archive using {result['tool_used']}")
            print(f"Extraction path: {result['extract_path']}")
            if result['output']:
                # Print tool output if verbose (could add --verbose flag later)
                pass
        else:
            print(f"Failed to extract RAR archive: {result['error']}")
            print(f"\nAvailable tools: {', '.join(result['available_tools']) if result['available_tools'] else 'None'}")
            if result['command']:
                print(f"Command attempted: {result['command']}")
            if result['output']:
                print(f"\nTool output:\n{result['output']}")
            
            # Provide helpful recommendations
            if not result['available_tools']:
                print("\nTo extract RAR archives, please install one of the following tools:")
                print("  - unrar: sudo apt-get install unrar (Linux) or download from https://www.rarlab.com/")
                print("  - 7z: sudo apt-get install p7zip-full (Linux) or download from https://www.7-zip.org/")
                print("  - unar: brew install unar (macOS)")
            
            sys.exit(1)
    
    except Exception as e:
        _print_error(f"Failed to extract RAR archive: {e}", exit_code=1)


def _cmd_rar_check_tools() -> None:
    """Check which external RAR extraction tools are available on the system.
    
    This command checks for common RAR extraction tools (unrar, 7z, unar) and
    displays information about their availability and versions.
    """
    try:
        from .utils import check_rar_external_tools
        
        result = check_rar_external_tools()
        
        print("External RAR Extraction Tools Status:")
        print("=" * 50)
        
        if result['available_tools']:
            print(f"\n✅ Available tools: {', '.join(result['available_tools'])}")
        else:
            print("\n❌ No external RAR extraction tools found")
        
        print("\nTool Details:")
        for tool_name, details in result['tool_details'].items():
            status = "✅ Available" if details['available'] else "❌ Not available"
            print(f"\n  {tool_name}: {status}")
            if details['available']:
                if details['path']:
                    print(f"    Path: {details['path']}")
                if details['version']:
                    print(f"    Version: {details['version']}")
            else:
                print(f"    Command: {details['command']}")
        
        if result['recommendations']:
            print("\nRecommendations:")
            for rec in result['recommendations']:
                print(f"  - {rec}")
    
    except Exception as e:
        _print_error(f"Failed to check external tools: {e}", exit_code=1)


def _cmd_rar_compat(archive: Path, no_tool_check: bool = False) -> None:
    """Analyze RAR archive compatibility and extractability.
    
    This command provides comprehensive analysis of a RAR archive, including
    which entries can be extracted directly, which require external tools,
    compression method breakdown, encryption status, and actionable recommendations.
    
    Args:
        archive: Path to the RAR archive to analyze.
        no_tool_check: If True, skip checking for external RAR extraction tools.
    """
    if not archive.exists():
        _print_error(f"Archive not found: {archive}", exit_code=2)
    
    try:
        from .utils import analyze_rar_compatibility
        
        print(f"Analyzing RAR archive: {archive}")
        print("=" * 70)
        
        result = analyze_rar_compatibility(archive, include_external_tool_check=not no_tool_check)
        
        # Archive info
        print("\n📦 Archive Information:")
        print(f"  Format: {result['archive_info']['format']}")
        if result['archive_info']['version']:
            print(f"  Version: {result['archive_info']['version']}")
        print(f"  Total entries: {result['archive_info']['total_entries']}")
        print(f"  File entries: {result['archive_info']['file_entries']}")
        print(f"  Directory entries: {result['archive_info']['directory_entries']}")
        
        # Extractability
        print("\n✅ Extractability Analysis:")
        extractability_ratio = result['extractability']['extractability_ratio']
        print(f"  Directly extractable: {result['extractability']['directly_extractable']} entries")
        print(f"  Requires external tool: {result['extractability']['requires_external_tool']} entries")
        print(f"  Extractability ratio: {extractability_ratio:.1%}")
        
        if result['extractability']['extractable_entries']:
            print(f"\n  ✅ Extractable entries ({len(result['extractability']['extractable_entries'])}):")
            for entry_name in result['extractability']['extractable_entries'][:10]:
                print(f"    - {entry_name}")
            if len(result['extractability']['extractable_entries']) > 10:
                print(f"    ... and {len(result['extractability']['extractable_entries']) - 10} more")
        
        if result['extractability']['non_extractable_entries']:
            print(f"\n  ❌ Non-extractable entries ({len(result['extractability']['non_extractable_entries'])}):")
            for entry_name in result['extractability']['non_extractable_entries'][:10]:
                print(f"    - {entry_name}")
            if len(result['extractability']['non_extractable_entries']) > 10:
                print(f"    ... and {len(result['extractability']['non_extractable_entries']) - 10} more")
        
        # Compression analysis
        if result['compression_analysis']['methods']:
            print("\n📊 Compression Method Analysis:")
            for method_name, method_stats in result['compression_analysis']['methods'].items():
                status = "✅ Supported" if method_stats['is_supported'] else "❌ Not supported"
                print(f"  {method_name}: {status}")
                print(f"    Count: {method_stats['count']} entries")
                if method_stats['total_size'] > 0:
                    print(f"    Total size: {method_stats['total_size']:,} bytes")
                    print(f"    Compressed size: {method_stats['total_compressed_size']:,} bytes")
                    if method_stats['total_size'] > 0:
                        ratio = method_stats['total_compressed_size'] / method_stats['total_size']
                        print(f"    Compression ratio: {ratio:.2%}")
        
        # Encryption analysis
        print("\n🔒 Encryption Analysis:")
        if result['encryption_analysis']['has_encryption']:
            print(f"  ⚠️  Archive contains encrypted entries")
            print(f"  Encrypted: {result['encryption_analysis']['encrypted_count']} entries")
            print(f"  Unencrypted: {result['encryption_analysis']['unencrypted_count']} entries")
            if result['encryption_analysis']['encrypted_entries']:
                print(f"\n  Encrypted entries:")
                for entry_name in result['encryption_analysis']['encrypted_entries'][:5]:
                    print(f"    - {entry_name}")
                if len(result['encryption_analysis']['encrypted_entries']) > 5:
                    print(f"    ... and {len(result['encryption_analysis']['encrypted_entries']) - 5} more")
        else:
            print("  ✅ No encrypted entries")
        
        # External tools
        if result['external_tools']:
            print("\n🔧 External Tools Status:")
            if result['external_tools']['available_tools']:
                print(f"  ✅ Available tools: {', '.join(result['external_tools']['available_tools'])}")
                for tool_name, details in result['external_tools']['tool_details'].items():
                    if details['available']:
                        print(f"    {tool_name}: {details['version'] or 'version unknown'} at {details['path']}")
            else:
                print("  ❌ No external RAR extraction tools found")
                if result['external_tools']['recommendations']:
                    print("\n  Installation recommendations:")
                    for rec in result['external_tools']['recommendations']:
                        print(f"    - {rec}")
        
        # Recommendations
        print("\n💡 Recommendations:")
        if result['recommendations']['extraction_strategy']:
            print("\n  Extraction Strategy:")
            for rec in result['recommendations']['extraction_strategy']:
                print(f"    - {rec}")
        
        if result['recommendations']['conversion_options']:
            print("\n  Conversion Options:")
            for rec in result['recommendations']['conversion_options']:
                print(f"    - {rec}")
        
        if result['recommendations']['tool_usage']:
            print("\n  Tool Usage:")
            for rec in result['recommendations']['tool_usage']:
                print(f"    - {rec}")
        
        print("\n" + "=" * 70)
        print("Analysis complete!")
    
    except Exception as e:
        _print_error(f"Failed to analyze RAR compatibility: {e}", exit_code=1)


def _cmd_update(archive: Path, entry: str, source: Path, compression: str = "deflate", compression_level: int = 6) -> None:
    """Update an existing entry in an archive.
    
    Args:
        archive: Path to the ZIP archive to update.
        entry: Entry name to update.
        source: Source file to replace the entry with.
        compression: Compression method to use.
        compression_level: Compression level.
    """
    if not archive.exists():
        _print_error(f"Archive not found: {archive}", exit_code=2)
    
    if not source.exists():
        _print_error(f"Source file not found: {source}", exit_code=2)
    
    if not source.is_file():
        _print_error(f"Source must be a file: {source}", exit_code=2)
    
    try:
        # Open archive in update mode
        with ZipWriter(archive, mode="a") as z:
            # Read source file
            with open(source, "rb") as f:
                data = f.read()
            
            # Add/update entry (add_bytes will replace if exists)
            z.add_bytes(entry, data, compression=compression, compression_level=compression_level)
    except Exception as e:
        _print_error(f"Archive update failed: {e}", exit_code=1)


def _cmd_delete(archive: Path, entry: str) -> None:
    """Delete an entry from an archive.
    
    Args:
        archive: Path to the ZIP archive.
        entry: Entry name to delete.
    """
    if not archive.exists():
        _print_error(f"Archive not found: {archive}", exit_code=2)
    
    try:
        # Open archive in update mode
        with ZipWriter(archive, mode="a") as z:
            z.delete_entry(entry)
    except Exception as e:
        _print_error(f"Archive delete failed: {e}", exit_code=1)


def _cmd_rename(archive: Path, old_name: str, new_name: str) -> None:
    """Rename an entry in an archive.
    
    Args:
        archive: Path to the ZIP archive.
        old_name: Current entry name.
        new_name: New entry name.
    """
    if not archive.exists():
        _print_error(f"Archive not found: {archive}", exit_code=2)
    
    try:
        # Open archive in update mode
        with ZipWriter(archive, mode="a") as z:
            z.rename_entry(old_name, new_name)
    except Exception as e:
        _print_error(f"Archive rename failed: {e}", exit_code=1)


def _cmd_merge(
    output: Path,
    archives: List[Path],
    overwrite: bool = False,
    conflict_resolution: str = "skip",
) -> None:
    """Merge multiple archives into a single archive.
    
    Args:
        output: Path to the output archive file.
        archives: List of paths to source archives to merge.
        overwrite: If True, overwrite existing output file.
        conflict_resolution: Strategy for handling entry name conflicts.
    """
    from .utils import merge_archives
    
    # Validate that all source archives exist
    for archive in archives:
        if not archive.exists():
            _print_error(f"Source archive not found: {archive}", exit_code=2)
    
    try:
        # Merge archives
        result = merge_archives(
            output,
            archives,
            overwrite=overwrite,
            conflict_resolution=conflict_resolution,
        )
        
        # Print summary
        print(f"Merged {len(archives)} archive(s) into: {output}")
        print(f"Total entries processed: {result['total_entries']}")
        print(f"Entries added: {result['entries_added']}")
        
        if result['entries_skipped'] > 0:
            print(f"Entries skipped (conflicts): {result['entries_skipped']}")
        if result['entries_overwritten'] > 0:
            print(f"Entries overwritten: {result['entries_overwritten']}")
        if result['entries_renamed'] > 0:
            print(f"Entries renamed: {result['entries_renamed']}")
        
        if result['conflicts']:
            print(f"\nConflicts detected: {len(result['conflicts'])} entry name(s)")
            if len(result['conflicts']) <= 10:
                for conflict in result['conflicts']:
                    print(f"  - {conflict}")
            else:
                for conflict in result['conflicts'][:10]:
                    print(f"  - {conflict}")
                print(f"  ... and {len(result['conflicts']) - 10} more")
        
    except Exception as e:
        _print_error(f"Archive merge failed: {e}", exit_code=1)


def _cmd_split(
    archive: Path,
    output: Path,
    max_size: Optional[str] = None,
    max_entries: Optional[int] = None,
    overwrite: bool = False,
) -> None:
    """Split an archive into multiple smaller archives.
    
    Args:
        archive: Path to the source archive to split.
        output: Base path for output archives.
        max_size: Maximum uncompressed size per output archive (e.g., '100MB').
        max_entries: Maximum number of entries per output archive.
        overwrite: If True, overwrite existing output files.
    """
    from .utils import split_archive
    
    # Validate source archive exists
    if not archive.exists():
        _print_error(f"Source archive not found: {archive}", exit_code=2)
    
    # Parse max_size if provided
    max_size_bytes = None
    if max_size is not None:
        max_size_bytes = _parse_size(max_size)
        if max_size_bytes is None:
            _print_error(
                f"Invalid size format: {max_size}. "
                "Use format like '100MB', '1GB', '500KB', etc.",
                exit_code=2
            )
    
    try:
        # Split archive
        result = split_archive(
            archive,
            output,
            max_size=max_size_bytes,
            max_entries=max_entries,
            overwrite=overwrite,
        )
        
        # Print summary
        print(f"Split archive into {len(result['output_archives'])} archive(s)")
        print(f"Total entries processed: {result['total_entries']}")
        print(f"\nOutput archives:")
        for output_archive in result['output_archives']:
            entry_count = result['entries_per_archive'].get(output_archive, 0)
            size = result['sizes_per_archive'].get(output_archive, 0)
            size_mb = size / (1024 * 1024)
            print(f"  {output_archive}: {entry_count} entries, {size_mb:.2f} MB")
        
    except Exception as e:
        _print_error(f"Archive split failed: {e}", exit_code=1)


def _parse_size(size_str: str) -> Optional[int]:
    """Parse size string like '100MB', '1GB', '500KB' into bytes.
    
    Args:
        size_str: Size string with unit suffix.
    
    Returns:
        Size in bytes, or None if format is invalid.
    """
    import re
    
    # Match pattern: number followed by optional unit
    match = re.match(r'^(\d+(?:\.\d+)?)\s*([KMGT]?B?)$', size_str.upper())
    if not match:
        return None
    
    value = float(match.group(1))
    unit = match.group(2) or 'B'
    
    # Convert to bytes
    multipliers = {
        'B': 1,
        'KB': 1024,
        'MB': 1024 * 1024,
        'GB': 1024 * 1024 * 1024,
        'TB': 1024 * 1024 * 1024 * 1024,
    }
    
    multiplier = multipliers.get(unit, 1)
    return int(value * multiplier)


def _cmd_compare(
    archive1: Path,
    archive2: Path,
    format: Optional[str] = None,
) -> int:
    """Compare two archives and show basic differences.
    
    This is a simpler alternative to the 'diff' command, providing a quick
    comparison without detailed statistics.
    
    Args:
        archive1: Path to first archive.
        archive2: Path to second archive.
        format: Archive format (auto-detected if not specified).
    
    Returns:
        Exit code: 0 if archives are identical, 1 if different.
    """
    from .utils import detect_archive_format
    
    # Detect format if not specified
    if format is None:
        format1 = detect_archive_format(archive1)
        format2 = detect_archive_format(archive2)
        if format1 != format2:
            _print_error(f"Archive formats differ: {format1} vs {format2}. Use --format to specify.", exit_code=2)
        format = format1
    
    if format is None:
        _print_error("Could not detect archive format. Please specify --format.", exit_code=2)
    
    # Select reader class based on format
    reader_class_map = {
        'zip': ZipReader,
        'tar': TarReader,
        '7z': SevenZipReader,
        'rar': RarReader,
    }
    
    reader_class = reader_class_map.get(format)
    if reader_class is None:
        _print_error(f"Unsupported format for compare: {format}", exit_code=2)
    
    # Validate archives exist
    if not archive1.exists():
        _print_error(f"Archive not found: {archive1}", exit_code=2)
    if not archive2.exists():
        _print_error(f"Archive not found: {archive2}", exit_code=2)
    
    try:
        # Perform comparison
        result = compare_archives(archive1, archive2, reader_class=reader_class)
        
        # Print results
        print(f"Comparing: {archive1} vs {archive2}")
        print("=" * 80)
        
        if result['identical']:
            print("\n✅ Archives are identical")
            return 0
        
        # Print differences
        print("\n❌ Archives differ")
        print()
        
        if result['only_in_first']:
            print(f"📁 Only in first archive ({len(result['only_in_first'])}):")
            for entry_name in result['only_in_first'][:20]:  # Limit to first 20
                print(f"  - {entry_name}")
            if len(result['only_in_first']) > 20:
                print(f"  ... and {len(result['only_in_first']) - 20} more")
            print()
        
        if result['only_in_second']:
            print(f"📁 Only in second archive ({len(result['only_in_second'])}):")
            for entry_name in result['only_in_second'][:20]:  # Limit to first 20
                print(f"  - {entry_name}")
            if len(result['only_in_second']) > 20:
                print(f"  ... and {len(result['only_in_second']) - 20} more")
            print()
        
        if result['different']:
            print(f"🔀 Different entries ({len(result['different'])}):")
            for entry_name in result['different'][:20]:  # Limit to first 20
                print(f"  - {entry_name}")
            if len(result['different']) > 20:
                print(f"  ... and {len(result['different']) - 20} more")
            print()
        
        if result['same']:
            print(f"✅ Identical entries ({len(result['same'])}):")
            if len(result['same']) <= 10:
                for entry_name in result['same']:
                    print(f"  - {entry_name}")
            else:
                for entry_name in result['same'][:5]:
                    print(f"  - {entry_name}")
                print(f"  ... and {len(result['same']) - 5} more")
        
        print()
        print("💡 Tip: Use 'diff' command for detailed comparison with statistics")
        return 1
    
    except Exception as e:
        _print_error(f"Comparison failed: {e}", exit_code=1)
        return 1


def _cmd_compare_formats(
    archive1: Path,
    archive2: Path,
    format1: Optional[str] = None,
    format2: Optional[str] = None,
    timeout: int = 300,
) -> None:
    """Compare two archives in potentially different formats.
    
    This command compares archives regardless of their format, extracting entries
    and comparing their contents. Useful for verifying format conversion correctness
    or comparing archives created by different tools.
    
    Args:
        archive1: Path to first archive file.
        archive2: Path to second archive file.
        format1: Optional format name for first archive (auto-detected if not specified).
        format2: Optional format name for second archive (auto-detected if not specified).
        timeout: Maximum time to wait for comparison in seconds.
    """
    from .utils import compare_formats
    
    # Validate archives exist
    if not archive1.exists():
        _print_error(f"Archive 1 not found: {archive1}", exit_code=2)
    if not archive2.exists():
        _print_error(f"Archive 2 not found: {archive2}", exit_code=2)
    
    try:
        # Perform format comparison
        result = compare_formats(
            archive1_path=archive1,
            archive2_path=archive2,
            format1=format1,
            format2=format2,
            timeout_seconds=timeout,
        )
        
        # Check for errors
        if result.get('error'):
            _print_error(f"Comparison failed: {result['error']}", exit_code=1)
            return
        
        # Print results
        print("=" * 80)
        print("Format Comparison Results")
        print("=" * 80)
        print()
        print(f"Archive 1: {result['archive1_path']} ({result['format1']})")
        print(f"Archive 2: {result['archive2_path']} ({result['format2']})")
        print()
        
        if result['identical']:
            print("✅ Archives are identical")
            print(f"   Same entries: {len(result['same'])}")
        else:
            print("❌ Archives differ")
            print()
            
            if result['only_in_first']:
                print(f"📁 Only in first archive ({len(result['only_in_first'])}):")
                for entry_name in result['only_in_first'][:20]:  # Limit to first 20
                    print(f"  - {entry_name}")
                if len(result['only_in_first']) > 20:
                    print(f"  ... and {len(result['only_in_first']) - 20} more")
                print()
            
            if result['only_in_second']:
                print(f"📁 Only in second archive ({len(result['only_in_second'])}):")
                for entry_name in result['only_in_second'][:20]:  # Limit to first 20
                    print(f"  - {entry_name}")
                if len(result['only_in_second']) > 20:
                    print(f"  ... and {len(result['only_in_second']) - 20} more")
                print()
            
            if result['different']:
                print(f"🔀 Different entries ({len(result['different'])}):")
                for entry_name in result['different'][:20]:  # Limit to first 20
                    print(f"  - {entry_name}")
                if len(result['different']) > 20:
                    print(f"  ... and {len(result['different']) - 20} more")
                print()
            
            if result['same']:
                print(f"✅ Identical entries ({len(result['same'])}):")
                if len(result['same']) <= 10:
                    for entry_name in result['same']:
                        print(f"  - {entry_name}")
                else:
                    for entry_name in result['same'][:5]:
                        print(f"  - {entry_name}")
                    print(f"  ... and {len(result['same']) - 5} more")
                print()
        
        print(f"⏱️  Comparison time: {result['comparison_time']:.2f} seconds")
        
    except Exception as e:
        _print_error(f"Format comparison failed: {e}", exit_code=1)


def _cmd_format_statistics(
    archive: Path,
    format_name: Optional[str] = None,
    timeout: int = 300,
) -> None:
    """Get comprehensive statistics for an archive in any format.
    
    This command provides format-agnostic statistics extraction, automatically
    detecting the format and using the appropriate reader.
    
    Args:
        archive: Path to the archive file.
        format_name: Optional format name (auto-detected if not specified).
        timeout: Maximum time to wait in seconds.
    """
    from .utils import get_format_statistics
    
    # Validate archive exists
    if not archive.exists():
        _print_error(f"Archive not found: {archive}", exit_code=2)
    
    try:
        # Get format statistics
        result = get_format_statistics(
            archive_path=archive,
            format_name=format_name,
            timeout_seconds=timeout,
        )
        
        # Check for errors
        if result.get('error'):
            _print_error(f"Statistics extraction failed: {result['error']}", exit_code=1)
            return
        
        # Print results
        print("=" * 80)
        print("Format Statistics")
        print("=" * 80)
        print()
        print(f"Archive: {result['archive_path']}")
        print(f"Format: {result['format']}")
        print()
        
        print("📊 Archive Statistics:")
        print(f"  Total entries: {result['total_entries']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        
        # Print additional statistics if available
        if 'compression_methods' in result:
            print()
            print("🔧 Compression Methods:")
            for method, count in sorted(result['compression_methods'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} entries")
        
        if 'encrypted_entries' in result:
            print()
            print(f"🔒 Encrypted entries: {result['encrypted_entries']:,}")
        
        if 'directories' in result:
            print()
            print(f"📁 Directories: {result['directories']:,}")
        
        print()
        print(f"⏱️  Statistics extraction time: {result['statistics_time']:.2f} seconds")
        
    except Exception as e:
        _print_error(f"Format statistics extraction failed: {e}", exit_code=1)


def _cmd_diff(
    archive1: Path,
    archive2: Path,
    format: Optional[str] = None,
    summary_only: bool = False,
) -> None:
    """Compare two archives and show detailed differences.
    
    Args:
        archive1: Path to first archive.
        archive2: Path to second archive.
        format: Archive format (auto-detected if not specified).
        summary_only: If True, only show summary statistics.
    """
    from .utils import detect_archive_format
    
    # Detect format if not specified
    if format is None:
        format1 = detect_archive_format(archive1)
        format2 = detect_archive_format(archive2)
        if format1 != format2:
            _print_error(f"Archive formats differ: {format1} vs {format2}. Use --format to specify.", exit_code=2)
        format = format1
    
    if format is None:
        _print_error("Could not detect archive format. Please specify --format.", exit_code=2)
    
    # Select reader class based on format
    reader_class_map = {
        'zip': ZipReader,
        'tar': TarReader,
        '7z': SevenZipReader,
        'rar': RarReader,
    }
    
    reader_class = reader_class_map.get(format)
    if reader_class is None:
        _print_error(f"Unsupported format for diff: {format}", exit_code=2)
    
    # Validate archives exist
    if not archive1.exists():
        _print_error(f"Archive not found: {archive1}", exit_code=2)
    if not archive2.exists():
        _print_error(f"Archive not found: {archive2}", exit_code=2)
    
    try:
        # Perform diff
        result = diff_archives(archive1, archive2, reader_class=reader_class, detailed=not summary_only)
        
        # Print results
        print(f"Comparing: {archive1} vs {archive2}")
        print("=" * 80)
        
        if result['identical']:
            print("\n✅ Archives are identical")
            return
        
        # Print summary
        summary = result['summary']
        print(f"\n📊 Summary:")
        print(f"  Total entries in first:  {summary['total_entries_first']}")
        print(f"  Total entries in second: {summary['total_entries_second']}")
        print(f"  Common entries:          {summary['common_entries']}")
        print(f"  Only in first:           {summary['only_in_first_count']}")
        print(f"  Only in second:          {summary['only_in_second_count']}")
        print(f"  Different:               {summary['different_count']}")
        print(f"  Identical:               {summary['same_count']}")
        print(f"  Total size (first):      {summary['total_size_first']:,} bytes")
        print(f"  Total size (second):     {summary['total_size_second']:,} bytes")
        print(f"  Compressed size (first): {summary['total_compressed_size_first']:,} bytes")
        print(f"  Compressed size (second): {summary['total_compressed_size_second']:,} bytes")
        
        if summary_only:
            return
        
        # Print entries only in first
        if result['only_in_first']:
            print(f"\n📁 Only in first archive ({len(result['only_in_first'])}):")
            for item in result['only_in_first'][:20]:  # Limit to first 20
                if isinstance(item, dict):
                    print(f"  - {item['name']} ({item.get('size', 0):,} bytes)")
                else:
                    print(f"  - {item}")
            if len(result['only_in_first']) > 20:
                print(f"  ... and {len(result['only_in_first']) - 20} more")
        
        # Print entries only in second
        if result['only_in_second']:
            print(f"\n📁 Only in second archive ({len(result['only_in_second'])}):")
            for item in result['only_in_second'][:20]:  # Limit to first 20
                if isinstance(item, dict):
                    print(f"  - {item['name']} ({item.get('size', 0):,} bytes)")
                else:
                    print(f"  - {item}")
            if len(result['only_in_second']) > 20:
                print(f"  ... and {len(result['only_in_second']) - 20} more")
        
        # Print different entries
        if result['different']:
            print(f"\n🔀 Different entries ({len(result['different'])}):")
            for diff in result['different'][:20]:  # Limit to first 20
                if isinstance(diff, dict):
                    print(f"  - {diff['name']}:")
                    for difference in diff.get('differences', []):
                        print(f"      {difference}")
                else:
                    print(f"  - {diff}")
            if len(result['different']) > 20:
                print(f"  ... and {len(result['different']) - 20} more")
        
    except Exception as e:
        _print_error(f"Error comparing archives: {e}", exit_code=1)


def _cmd_batch_process(
    archives: List[Path],
    operation: str,
    output_dir: Optional[Path] = None,
    target_format: Optional[str] = None,
    compression: Optional[str] = None,
    compression_level: Optional[int] = None,
    stop_on_error: bool = False,
) -> None:
    """Process multiple archives in batch with a specified operation.
    
    Args:
        archives: List of paths to archives to process.
        operation: Operation to perform ('extract', 'validate', 'list', 'statistics', 'convert', 'optimize').
        output_dir: Output directory for extract/convert/optimize operations.
        target_format: Target format for convert operation.
        compression: Compression method for convert/optimize operations.
        compression_level: Compression level for convert/optimize operations.
        stop_on_error: If True, stop processing on first error.
    """
    # Build operation parameters
    operation_params = {}
    if operation == 'convert':
        if target_format is None:
            _print_error("--target-format is required for convert operation", exit_code=2)
        operation_params['target_format'] = target_format
        if compression:
            operation_params['compression'] = compression
        if compression_level is not None:
            operation_params['compression_level'] = compression_level
    elif operation == 'optimize':
        if compression:
            operation_params['compression'] = compression
        if compression_level is not None:
            operation_params['compression_level'] = compression_level
    
    # Progress callback
    def progress_callback(archive_path: str, current: int, total: int, status: str) -> None:
        status_symbol = {
            'processing': '⏳',
            'success': '✅',
            'error': '❌',
        }.get(status, '•')
        print(f"{status_symbol} [{current}/{total}] {archive_path} ({status})")
    
    try:
        results = batch_process_archives(
            archives,
            operation=operation,
            output_dir=output_dir,
            operation_params=operation_params,
            progress_callback=progress_callback,
            stop_on_error=stop_on_error,
        )
        
        # Print summary
        print("\n" + "=" * 80)
        print(f"Batch Processing Summary:")
        print(f"  Total archives:    {results['total']}")
        print(f"  Successful:        {results['successful']}")
        print(f"  Failed:            {results['failed']}")
        
        # Print failed archives
        if results['failed'] > 0:
            print("\nFailed archives:")
            for result in results['results']:
                if not result['success']:
                    print(f"  ❌ {result['archive_path']}: {result['error']}")
        
        # Exit with error code if any failed
        if results['failed'] > 0:
            sys.exit(1)
    
    except Exception as e:
        _print_error(f"Error during batch processing: {e}", exit_code=1)


def _cmd_export(
    archive: Path,
    output: Path,
    format: str = 'json',
    archive_format: Optional[str] = None,
) -> None:
    """Export archive metadata to JSON or CSV format.
    
    Args:
        archive: Path to the archive file.
        output: Path to the output file (JSON or CSV).
        format: Output format ('json' or 'csv'). Defaults to 'json'.
        archive_format: Archive format (auto-detected if not specified).
    """
    from .utils import detect_archive_format
    
    # Detect format if not specified
    if archive_format is None:
        archive_format = detect_archive_format(archive)
    
    if archive_format is None:
        _print_error("Could not detect archive format. Please specify --archive-format.", exit_code=2)
    
    # Select reader class based on format
    reader_class_map = {
        'zip': ZipReader,
        'tar': TarReader,
        '7z': SevenZipReader,
        'rar': RarReader,
    }
    
    reader_class = reader_class_map.get(archive_format)
    if reader_class is None:
        _print_error(f"Unsupported format for export: {archive_format}", exit_code=2)
    
    # Validate archive exists
    if not archive.exists():
        _print_error(f"Archive not found: {archive}", exit_code=2)
    
    # Validate output format
    if format not in ('json', 'csv'):
        _print_error(f"Unsupported output format: {format}. Must be 'json' or 'csv'.", exit_code=2)
    
    try:
        # Export metadata
        export_archive_metadata(archive, output, format=format, reader_class=reader_class)
        print(f"✅ Exported metadata to: {output} ({format.upper()})")
    except Exception as e:
        _print_error(f"Error exporting metadata: {e}", exit_code=1)


def _format_size(size_bytes: int) -> str:
    """Format size in bytes to human-readable format.
    
    Args:
        size_bytes: Size in bytes.
        
    Returns:
        Formatted size string (e.g., "1.5 MB").
    """
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} PB"


def _cmd_optimize(
    archive: Path,
    output: Path,
    compression: Optional[str] = None,
    compression_level: Optional[int] = None,
    password: Optional[str] = None,
    password_file: Optional[Path] = None,
    no_preserve_metadata: bool = False,
) -> None:
    """Optimize an archive by recompressing entries with different compression settings.
    
    Args:
        archive: Path to the source archive to optimize.
        output: Path to the output optimized archive.
        compression: Compression method ('deflate', 'bzip2', 'lzma', 'stored').
                    If not specified, uses original compression method for each entry.
        compression_level: Compression level (0-9). If not specified, uses default (6).
        password: Password for encrypted ZIP archives (source only).
        password_file: Path to file containing password.
        no_preserve_metadata: If True, does not preserve file timestamps and metadata.
    """
    from .utils import detect_archive_format
    
    # Detect format
    archive_format = detect_archive_format(archive)
    
    if archive_format is None:
        _print_error("Could not detect archive format. Optimization currently only supports ZIP format.", exit_code=2)
    
    if archive_format != 'zip':
        _print_error(
            f"Archive optimization currently only supports ZIP format. "
            f"Detected format: {archive_format}. Use 'convert' command for format conversion.",
            exit_code=2
        )
    
    # Validate archive exists
    if not archive.exists():
        _print_error(f"Archive not found: {archive}", exit_code=2)
    
    # Get password if needed
    password_bytes = _get_password(password, password_file)
    
    # Validate compression method
    if compression is not None:
        valid_methods = {'deflate', 'bzip2', 'lzma', 'stored'}
        if compression.lower() not in valid_methods:
            _print_error(
                f"Invalid compression method: {compression}. "
                f"Must be one of: {', '.join(valid_methods)}",
                exit_code=2
            )
        compression = compression.lower()
    
    # Validate compression level
    if compression_level is not None:
        if compression_level < 0 or compression_level > 9:
            _print_error("Compression level must be between 0 and 9.", exit_code=2)
    
    try:
        # Create progress callback
        def progress_callback(entry_name: str, current: int, total: int) -> None:
            percent = (current / total * 100) if total > 0 else 0
            print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name}", end='\r')
        
        print(f"Optimizing archive: {archive}")
        print(f"Output: {output}")
        if compression:
            print(f"Compression: {compression}")
        if compression_level is not None:
            print(f"Compression level: {compression_level}")
        print("-" * 80)
        
        # Optimize archive
        result = optimize_archive(
            archive,
            output,
            compression=compression,
            compression_level=compression_level,
            preserve_metadata=not no_preserve_metadata,
            password=password_bytes,
            progress_callback=progress_callback,
        )
        
        print()  # New line after progress
        print("-" * 80)
        print("Optimization complete!")
        print(f"  Original size: {_format_size(result['original_size'])}")
        print(f"  Optimized size: {_format_size(result['optimized_size'])}")
        
        if result['size_reduction'] > 0:
            print(f"  Size reduction: {_format_size(result['size_reduction'])} ({result['size_reduction_percent']:.1f}%)")
        elif result['size_reduction'] < 0:
            print(f"  Size increase: {_format_size(-result['size_reduction'])} ({-result['size_reduction_percent']:.1f}%)")
        else:
            print(f"  Size unchanged")
        
        print(f"  Entries optimized: {result['entries_optimized']}")
        if result['entries_skipped'] > 0:
            print(f"  Entries skipped: {result['entries_skipped']}")
        
        if result['errors']:
            print(f"\n  Errors encountered:")
            for error in result['errors']:
                print(f"    - {error}")
        
        print(f"\n✅ Optimized archive saved to: {output}")
        
    except Exception as e:
        _print_error(f"Error optimizing archive: {e}", exit_code=1)


def _cmd_repair(
    archive: Path,
    output: Optional[Path] = None,
    repair: bool = False,
    crc_mode: str = "strict",
    format: Optional[str] = None,
) -> None:
    """Validate and optionally repair an archive by extracting valid entries.
    
    Args:
        archive: Path to the archive to validate/repair.
        output: Path to the repaired archive (required if repair=True).
        repair: If True, create a repaired archive with only valid entries.
        crc_mode: CRC verification mode ("strict", "warn", or "skip").
        format: Archive format (auto-detected if not specified).
    """
    from .utils import detect_archive_format, deduplicate_archive
    
    # Detect format if not specified
    if format is None:
        format = detect_archive_format(archive)
    
    if format is None:
        _print_error("Could not detect archive format. Please specify --format.", exit_code=2)
    
    # Select reader and writer classes based on format
    reader_class_map = {
        'zip': ZipReader,
        'tar': TarReader,
        '7z': SevenZipReader,
        'rar': RarReader,
    }
    
    writer_class_map = {
        'zip': ZipWriter,
        'tar': TarWriter,
        '7z': SevenZipWriter,
        'rar': None,  # RAR writing not supported
    }
    
    reader_class = reader_class_map.get(format)
    if reader_class is None:
        _print_error(f"Unsupported format for repair: {format}", exit_code=2)
    
    writer_class = writer_class_map.get(format)
    if repair and writer_class is None:
        _print_error(f"Repair not supported for format: {format} (writing not available)", exit_code=2)
    
    # Validate archive exists
    if not archive.exists():
        _print_error(f"Archive not found: {archive}", exit_code=2)
    
    # Validate output path if repair requested
    if repair:
        if output is None:
            _print_error("Output path is required when --repair flag is used.", exit_code=2)
        if output.exists():
            _print_error(f"Output file already exists: {output}. Remove it first or choose a different path.", exit_code=2)
    else:
        # If not repairing, output is optional (just validation)
        output = None
    
    try:
        # Create progress callback
        def progress_callback(current: int, total: int, entry_name: str) -> None:
            percent = (current / total * 100) if total > 0 else 0
            print(f"  [{current}/{total}] ({percent:.1f}%) Validating: {entry_name}", end='\r')
        
        print(f"Validating archive: {archive}")
        if repair:
            print(f"Repair mode: ON")
            print(f"Output: {output}")
        print(f"CRC mode: {crc_mode}")
        print("-" * 80)
        
        # Validate and optionally repair
        result = validate_and_repair_archive(
            archive,
            output_path=output,
            reader_class=reader_class,
            writer_class=writer_class,
            repair=repair,
            crc_verification=crc_mode,
            progress_callback=progress_callback,
        )
        
        print()  # New line after progress
        print("-" * 80)
        
        # Print results
        if result['valid']:
            print("✅ Archive is valid!")
            print(f"  Total entries: {result['total_entries']}")
            print(f"  Valid entries: {result['valid_entries']}")
        else:
            print("❌ Archive validation failed!")
            print(f"  Total entries: {result['total_entries']}")
            print(f"  Valid entries: {result['valid_entries']}")
            print(f"  Corrupted entries: {result['corrupted_entries']}")
            
            if result['errors']:
                print("\n  Errors found:")
                for error in result['errors']:
                    print(f"    - {error['entry_name']}: {error['error_type']} - {error['error_message']}")
        
        if repair:
            if result['repaired']:
                print(f"\n✅ Repaired archive saved to: {result['repaired_archive_path']}")
                print(f"  Entries repaired: {result['entries_repaired']}")
            else:
                print("\n⚠️  Repair was requested but no repaired archive was created.")
        
    except Exception as e:
        _print_error(f"Error validating/repairing archive: {e}", exit_code=1)


def _cmd_deduplicate(
    archive: Path,
    output: Path,
    hash_algorithm: str = "crc32",
    keep_last: bool = False,
    preserve_metadata: bool = True,
    compression: Optional[str] = None,
    compression_level: Optional[int] = None,
    password: Optional[str] = None,
    password_file: Optional[Path] = None,
    format: Optional[str] = None,
) -> None:
    """Remove duplicate files from an archive based on content hash.
    
    Args:
        archive: Path to the source archive file.
        output: Path to the output archive file.
        hash_algorithm: Hash algorithm for duplicate detection ('crc32' or 'sha256').
        keep_last: If True, keep the last occurrence of duplicates instead of first.
        preserve_metadata: If True, preserve file metadata from original archive.
        compression: Compression method for output archive (None preserves original).
        compression_level: Compression level (0-9, None preserves original).
        password: Password for encrypted source archives (ZIP only).
        password_file: File containing password for encrypted source archives.
        format: Archive format (auto-detected if not specified).
    """
    from .utils import detect_archive_format, deduplicate_archive
    
    # Detect format if not specified
    if format is None:
        format = detect_archive_format(archive)
    
    if format is None:
        _print_error("Could not detect archive format. Please specify --format.", exit_code=2)
    
    # Select reader and writer classes based on format
    reader_class_map = {
        'zip': ZipReader,
        'tar': TarReader,
        '7z': SevenZipReader,
    }
    
    writer_class_map = {
        'zip': ZipWriter,
        'tar': TarWriter,
        '7z': SevenZipWriter,
    }
    
    reader_class = reader_class_map.get(format)
    if reader_class is None:
        _print_error(f"Unsupported format for deduplication: {format}", exit_code=2)
    
    writer_class = writer_class_map.get(format)
    if writer_class is None:
        _print_error(f"Deduplication not supported for format: {format} (writing not available)", exit_code=2)
    
    # Validate archive exists
    if not archive.exists():
        _print_error(f"Archive not found: {archive}", exit_code=2)
    
    # Validate output path
    if output.exists():
        _print_error(f"Output file already exists: {output}. Remove it first or choose a different path.", exit_code=2)
    
    # Handle password
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    elif password_file:
        if not password_file.exists():
            _print_error(f"Password file not found: {password_file}", exit_code=2)
        try:
            password_bytes = password_file.read_bytes().strip()
        except Exception as e:
            _print_error(f"Error reading password file: {e}", exit_code=2)
    
    # Normalize compression method name
    compression_normalized = None
    if compression:
        compression_map = {
            'stored': None,
            'deflate': 'deflate',
            'bzip2': 'bzip2',
            'lzma': 'lzma',
        }
        compression_normalized = compression_map.get(compression)
    
    try:
        # Create progress callback
        def progress_callback(entry_name: str, current: int, total: int) -> None:
            percent = (current / total * 100) if total > 0 else 0
            print(f"  [{current}/{total}] ({percent:.1f}%) Processing: {entry_name}", end='\r')
        
        print(f"Deduplicating archive: {archive}")
        print(f"Output: {output}")
        print(f"Hash algorithm: {hash_algorithm}")
        print(f"Keep: {'last' if keep_last else 'first'} occurrence")
        print("-" * 80)
        
        # Deduplicate archive
        result = deduplicate_archive(
            archive,
            output,
            reader_class=reader_class,
            writer_class=writer_class,
            hash_algorithm=hash_algorithm,
            keep_first=not keep_last,
            preserve_metadata=preserve_metadata,
            compression=compression_normalized,
            compression_level=compression_level,
            password=password_bytes,
            progress_callback=progress_callback,
        )
        
        print()  # New line after progress
        print("-" * 80)
        
        # Print results
        print("✅ Deduplication complete!")
        print(f"  Total entries: {result['total_entries']}")
        print(f"  Unique entries: {result['unique_entries']}")
        print(f"  Duplicate entries removed: {result['duplicate_entries']}")
        print(f"  Duplicate groups found: {result['duplicate_groups']}")
        print(f"  Space saved: {result['space_saved']:,} bytes ({result['space_saved'] / 1024 / 1024:.2f} MB)")
        
        if result['duplicates']:
            print("\n📋 Duplicate Groups:")
            for group in result['duplicates']:
                print(f"  • {group['original']}")
                print(f"    Removed duplicates ({len(group['duplicates'])}):")
                for dup in group['duplicates']:
                    print(f"      - {dup}")
                print(f"    Total size: {group['size']:,} bytes")
        
    except Exception as e:
        _print_error(f"Error deduplicating archive: {e}", exit_code=1)


def _cmd_find_duplicates(
    archives: List[Path],
    hash_algorithm: str = "crc32",
    password: Optional[str] = None,
    password_file: Optional[Path] = None,
    quiet: bool = False,
) -> None:
    """Find duplicate files across multiple archives based on content hash.
    
    This command analyzes multiple archives to identify files with identical content
    across different archives. It computes content hashes for all files in all archives
    and groups files with the same hash together, indicating which files are duplicates
    across archives.
    
    Args:
        archives: List of paths to archive files to analyze.
        hash_algorithm: Hash algorithm for duplicate detection ('crc32' or 'sha256').
        password: Password for encrypted archives (applied to all archives).
        password_file: File containing password for encrypted archives.
        quiet: If True, suppress progress output.
    """
    from .utils import find_duplicates_across_archives
    
    # Validate archives exist
    for archive in archives:
        if not archive.exists():
            _print_error(f"Archive not found: {archive}", exit_code=2)
    
    # Handle password
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    elif password_file:
        if not password_file.exists():
            _print_error(f"Password file not found: {password_file}", exit_code=2)
        try:
            password_bytes = password_file.read_bytes().strip()
        except Exception as e:
            _print_error(f"Error reading password file: {e}", exit_code=2)
    
    # Create passwords dictionary (apply same password to all archives)
    passwords = {}
    if password_bytes:
        for archive in archives:
            passwords[str(archive)] = password_bytes
    
    try:
        # Create progress callback
        def progress_callback(archive_path: str, current: int, total: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) Processing: {Path(archive_path).name}", end='\r')
        
        if not quiet:
            print(f"Finding duplicates across {len(archives)} archives...")
            print(f"Hash algorithm: {hash_algorithm}")
            print("-" * 80)
        
        # Find duplicates
        result = find_duplicates_across_archives(
            archives,
            hash_algorithm=hash_algorithm,
            passwords=passwords if passwords else None,
            progress_callback=progress_callback if not quiet else None,
        )
        
        if not quiet:
            print()  # New line after progress
            print("-" * 80)
        
        # Print results
        print("✅ Duplicate analysis complete!")
        print(f"  Total archives analyzed: {result['total_archives']}")
        print(f"  Total files analyzed: {result['total_files']}")
        print(f"  Unique files: {result['unique_files']}")
        print(f"  Duplicate groups found: {result['duplicate_groups']}")
        print(f"  Potential space savings: {result['potential_space_savings']:,} bytes ({result['potential_space_savings'] / 1024 / 1024:.2f} MB)")
        
        if result['duplicates']:
            print("\n📋 Duplicate Groups:")
            for idx, group in enumerate(result['duplicates'][:20], 1):  # Show first 20 groups
                print(f"\n  Group {idx} ({group['count']} files, {group['size']:,} bytes):")
                for file_info in group['files']:
                    archive_name = Path(file_info['archive']).name
                    print(f"    • {archive_name}:{file_info['name']}")
                    if file_info.get('compression_method'):
                        print(f"      Compression: {file_info['compression_method']}, "
                              f"Size: {file_info['size']:,} bytes, "
                              f"Compressed: {file_info['compressed_size']:,} bytes")
            
            if len(result['duplicates']) > 20:
                print(f"\n  ... and {len(result['duplicates']) - 20} more duplicate groups")
        
        # Print archive statistics
        print("\n📊 Archive Statistics:")
        for archive_path, stats in result['archive_statistics'].items():
            archive_name = Path(archive_path).name
            print(f"  {archive_name}:")
            print(f"    Total files: {stats['total_files']}")
            print(f"    Unique files: {stats['unique_files']}")
            print(f"    Duplicate files: {stats['duplicate_files']}")
            print(f"    Total size: {stats['total_size']:,} bytes ({stats['total_size'] / 1024 / 1024:.2f} MB)")
            if stats['duplicate_size'] > 0:
                print(f"    Duplicate size: {stats['duplicate_size']:,} bytes ({stats['duplicate_size'] / 1024 / 1024:.2f} MB)")
        
    except Exception as e:
        _print_error(f"Error finding duplicates: {e}", exit_code=1)


def _cmd_create_smart(
    archive: Path,
    files: List[Path],
    strategy: str = "best_compression",
    sample_size: Optional[int] = None,
    test_methods: Optional[List[str]] = None,
    test_levels: Optional[List[int]] = None,
    password: Optional[str] = None,
    password_file: Optional[Path] = None,
    no_preserve_metadata: bool = False,
    quiet: bool = False,
) -> None:
    """Create an archive with automatic optimal compression selection for each file.
    
    This command analyzes each file to determine the best compression method and level,
    then creates an archive using those optimal settings per file. This provides better
    compression ratios than using a single compression method/level for all files.
    
    Args:
        archive: Path where the archive will be created.
        files: List of file/directory paths to add to archive.
        strategy: Compression strategy ('best_compression', 'balanced', 'fastest', 'fastest_decompression').
        sample_size: Maximum bytes to sample from each file for analysis (None = analyze entire file).
        test_methods: Compression methods to test (default: all methods).
        test_levels: Compression levels to test (default: [1, 3, 6, 9]).
        password: Password for encryption (ZIP only).
        password_file: File containing password for encryption.
        no_preserve_metadata: If True, do not preserve file metadata.
        quiet: If True, suppress progress output.
    """
    from .utils import create_archive_with_smart_compression
    from .writer import ZipWriter
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}. Remove it first or choose a different path.", exit_code=2)
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Handle password
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    elif password_file:
        if not password_file.exists():
            _print_error(f"Password file not found: {password_file}", exit_code=2)
        try:
            password_bytes = password_file.read_bytes().strip()
        except Exception as e:
            _print_error(f"Error reading password file: {e}", exit_code=2)
    
    try:
        # Create progress callback
        def progress_callback(file_path: str, current: int, total: int, method: str, level: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {Path(file_path).name} - {method} level {level}", end='\r')
        
        if not quiet:
            print(f"Creating archive with smart compression: {archive}")
            print(f"Strategy: {strategy}")
            print(f"Files/directories: {len(files)}")
            print("-" * 80)
        
        # Create archive with smart compression
        result = create_archive_with_smart_compression(
            archive_path=archive,
            file_paths=files,
            writer_class=ZipWriter,
            compression_strategy=strategy,
            sample_size=sample_size,
            test_methods=test_methods,
            test_levels=test_levels,
            password=password_bytes,
            preserve_metadata=not no_preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        if not quiet:
            print()  # New line after progress
            print("-" * 80)
        
        # Print results
        print("✅ Archive created successfully!")
        print(f"  Archive: {result['archive_path']}")
        print(f"  Files added: {result['total_files']}")
        print(f"  Directories added: {result['total_directories']}")
        print(f"  Original size: {result['total_size']:,} bytes ({result['total_size'] / 1024 / 1024:.2f} MB)")
        print(f"  Compressed size: {result['compressed_size']:,} bytes ({result['compressed_size'] / 1024 / 1024:.2f} MB)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print(f"  Space saved: {result['statistics']['total_space_saved']:,} bytes ({result['statistics']['space_saved_percent']:.1f}%)")
        
        # Print compression method usage
        print("\n📊 Compression Method Usage:")
        for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
            print(f"  {method}: {count} files")
        
        # Print compression settings for first few files
        if not quiet and result['compression_settings']:
            print("\n📋 Compression Settings (sample):")
            for idx, (file_path, settings) in enumerate(list(result['compression_settings'].items())[:10], 1):
                print(f"  {idx}. {Path(file_path).name}:")
                print(f"     Method: {settings['method']}, Level: {settings['level']}")
                print(f"     Ratio: {settings['compression_ratio']:.2%}")
            
            if len(result['compression_settings']) > 10:
                print(f"  ... and {len(result['compression_settings']) - 10} more files")
        
    except Exception as e:
        _print_error(f"Error creating archive: {e}", exit_code=1)


def _cmd_create_preset(
    archive: Path,
    files: List[Path],
    preset: str = "balanced",
    password: Optional[str] = None,
    password_file: Optional[Path] = None,
    no_preserve_metadata: bool = False,
    quiet: bool = False,
) -> None:
    """Create an archive with file-type-based compression presets.
    
    This command creates archives using compression presets based on file types
    (determined by file extensions). Faster than smart compression but still
    provides good compression ratios.
    
    Args:
        archive: Path where the archive will be created.
        files: List of file/directory paths to add to archive.
        preset: Compression preset ('balanced', 'maximum', 'fast', default: balanced).
        password: Password for encryption (ZIP only).
        password_file: File containing password for encryption.
        no_preserve_metadata: Do not preserve file metadata.
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_preset_compression
    from .writer import ZipWriter
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}. Remove it first or choose a different path.", exit_code=2)
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Handle password
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    elif password_file:
        if not password_file.exists():
            _print_error(f"Password file not found: {password_file}", exit_code=2)
        try:
            password_bytes = password_file.read_bytes().strip()
        except Exception as e:
            _print_error(f"Error reading password file: {e}", exit_code=2)
    
    try:
        # Create progress callback
        def progress_callback(file_path: str, current: int, total: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {Path(file_path).name}", end='\r')
        
        if not quiet:
            print(f"Creating archive with preset compression: {archive}")
            print(f"Preset: {preset}")
            print(f"Files/directories: {len(files)}")
            print("-" * 80)
        
        # Create archive with preset compression
        result = create_archive_with_preset_compression(
            archive_path=archive,
            file_paths=files,
            writer_class=ZipWriter,
            preset=preset,
            password=password_bytes,
            preserve_metadata=not no_preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        if not quiet:
            print()  # New line after progress
            print("-" * 80)
        
        # Print results
        print("✅ Archive created successfully!")
        print(f"  Archive: {result['archive_path']}")
        print(f"  Files added: {result['total_files']}")
        print(f"  Directories added: {result['total_directories']}")
        print(f"  Original size: {result['total_size']:,} bytes ({result['total_size'] / 1024 / 1024:.2f} MB)")
        print(f"  Compressed size: {result['compressed_size']:,} bytes ({result['compressed_size'] / 1024 / 1024:.2f} MB)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print(f"  Space saved: {result['statistics']['total_space_saved']:,} bytes ({result['statistics']['space_saved_percent']:.1f}%)")
        
        # Print compression method usage
        print("\n📊 Compression Method Usage:")
        for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
            print(f"  {method}: {count} files")
        
        # Print file type usage
        print("\n📋 File Type Usage:")
        for file_type, count in sorted(result['statistics']['preset_usage'].items(), key=lambda x: x[1], reverse=True):
            print(f"  {file_type}: {count} files")
        
    except Exception as e:
        _print_error(f"Error creating archive: {e}", exit_code=1)


def _cmd_create_clean(
    archive: Path,
    files: List[Path],
    preset: str = "balanced",
    no_exclude_temp: bool = False,
    exclude_patterns: Optional[List[str]] = None,
    exclude_extensions: Optional[List[str]] = None,
    password: Optional[str] = None,
    password_file: Optional[Path] = None,
    no_preserve_metadata: bool = False,
    quiet: bool = False,
) -> None:
    """Create an archive with automatic exclusion of common temporary/cache files and preset compression.
    
    This command creates archives using file-type-based compression presets while
    automatically excluding common temporary files, cache files, and build artifacts.
    Useful for archiving project directories without manual filtering.
    
    Args:
        archive: Path where the archive will be created.
        files: List of file/directory paths to add to archive.
        preset: Compression preset ('balanced', 'maximum', 'fast', default: balanced).
        no_exclude_temp: If True, do not exclude common temporary files.
        exclude_patterns: Additional glob patterns to exclude.
        exclude_extensions: Additional file extensions to exclude.
        password: Password for encryption (ZIP only).
        password_file: File containing password for encryption.
        no_preserve_metadata: Do not preserve file metadata.
        quiet: Suppress progress output.
    """
    from .utils import create_archive_clean
    from .writer import ZipWriter
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}. Remove it first or choose a different path.", exit_code=2)
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Handle password
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    elif password_file:
        if not password_file.exists():
            _print_error(f"Password file not found: {password_file}", exit_code=2)
        try:
            password_bytes = password_file.read_bytes().strip()
        except Exception as e:
            _print_error(f"Error reading password file: {e}", exit_code=2)
    
    try:
        # Create progress callback
        def progress_callback(file_path: str, current: int, total: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {Path(file_path).name}", end='\r')
        
        if not quiet:
            print(f"Creating clean archive: {archive}")
            print(f"Preset: {preset}")
            print(f"Exclude common temp files: {not no_exclude_temp}")
            print(f"Files/directories: {len(files)}")
            print("-" * 80)
        
        # Create archive with clean filtering
        result = create_archive_clean(
            archive_path=archive,
            file_paths=files,
            writer_class=ZipWriter,
            preset=preset,
            exclude_common_temp=not no_exclude_temp,
            custom_exclude_patterns=exclude_patterns,
            custom_exclude_extensions=exclude_extensions,
            password=password_bytes,
            preserve_metadata=not no_preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        if not quiet:
            print()  # New line after progress
            print("-" * 80)
        
        # Print results
        print("✅ Archive created successfully!")
        print(f"  Archive: {result['archive_path']}")
        print(f"  Files added: {result['total_files']}")
        print(f"  Directories added: {result['total_directories']}")
        print(f"  Files excluded: {result['excluded_files']}")
        print(f"  Directories excluded: {result['excluded_directories']}")
        print(f"  Original size: {result['total_size']:,} bytes ({result['total_size'] / 1024 / 1024:.2f} MB)")
        print(f"  Compressed size: {result['compressed_size']:,} bytes ({result['compressed_size'] / 1024 / 1024:.2f} MB)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print(f"  Space saved: {result['statistics']['total_space_saved']:,} bytes ({result['statistics']['space_saved_percent']:.1f}%)")
        
        # Print compression method usage
        print("\n📊 Compression Method Usage:")
        for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
            print(f"  {method}: {count} files")
        
        # Print file type usage
        print("\n📋 File Type Usage:")
        for file_type, count in sorted(result['statistics']['preset_usage'].items(), key=lambda x: x[1], reverse=True):
            print(f"  {file_type}: {count} files")
        
        # Show sample of excluded items
        if result['excluded_items'] and not quiet:
            print(f"\n🚫 Excluded Items (sample, {len(result['excluded_items'])} total):")
            for item in result['excluded_items'][:20]:
                print(f"  - {item}")
            if len(result['excluded_items']) > 20:
                print(f"  ... and {len(result['excluded_items']) - 20} more")
        
    except Exception as e:
        _print_error(f"Error creating archive: {e}", exit_code=1)


def _cmd_create_dedup(
    archive: Path,
    files: List[Path],
    hash_algorithm: str = "crc32",
    keep_first: bool = True,
    compression: Optional[str] = None,
    compression_level: Optional[int] = None,
    preset: Optional[str] = None,
    password: Optional[str] = None,
    password_file: Optional[Path] = None,
    no_preserve_metadata: bool = False,
    quiet: bool = False,
) -> None:
    """Create an archive with automatic deduplication during creation.
    
    This command creates archives from files/directories while automatically
    detecting and removing duplicate files based on content hash. Only one
    copy of each unique file is stored in the archive.
    
    Args:
        archive: Path where the archive will be created.
        files: List of file/directory paths to add to archive.
        hash_algorithm: Hash algorithm for duplicate detection ('crc32' or 'sha256').
        keep_first: If True, keep first occurrence of duplicates (default: True).
        compression: Uniform compression method for all files (if preset not specified).
        compression_level: Compression level (0-9).
        preset: Compression preset ('balanced', 'maximum', 'fast') - overrides compression.
        password: Password for encryption (ZIP only).
        password_file: File containing password for encryption.
        no_preserve_metadata: Do not preserve file metadata.
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_deduplication
    from .writer import ZipWriter
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}. Remove it first or choose a different path.", exit_code=2)
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Handle password
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    elif password_file:
        if not password_file.exists():
            _print_error(f"Password file not found: {password_file}", exit_code=2)
        try:
            password_bytes = password_file.read_bytes().strip()
        except Exception as e:
            _print_error(f"Error reading password file: {e}", exit_code=2)
    
    # Validate compression and preset
    if compression and preset:
        _print_error("Cannot specify both --compression and --preset. Use one or the other.", exit_code=2)
    
    try:
        # Create progress callback
        def progress_callback(file_path: str, current: int, total: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {Path(file_path).name}", end='\r')
        
        if not quiet:
            print(f"Creating archive with deduplication: {archive}")
            print(f"Hash algorithm: {hash_algorithm}")
            print(f"Keep: {'first' if keep_first else 'last'} occurrence")
            if preset:
                print(f"Compression preset: {preset}")
            elif compression:
                print(f"Compression: {compression} level {compression_level or 6}")
            print(f"Files/directories: {len(files)}")
            print("-" * 80)
        
        # Create archive with deduplication
        result = create_archive_with_deduplication(
            archive_path=archive,
            file_paths=files,
            writer_class=ZipWriter,
            hash_algorithm=hash_algorithm,
            keep_first=keep_first,
            compression=compression,
            compression_level=compression_level,
            preset=preset,
            password=password_bytes,
            preserve_metadata=not no_preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        if not quiet:
            print()  # New line after progress
            print("-" * 80)
        
        # Print results
        print("✅ Archive created successfully!")
        print(f"  Archive: {result['archive_path']}")
        print(f"  Files scanned: {result['total_files_scanned']}")
        print(f"  Unique files: {result['unique_files']}")
        print(f"  Duplicate files skipped: {result['duplicate_files']}")
        print(f"  Duplicate groups found: {result['duplicate_groups']}")
        print(f"  Directories added: {result['total_directories']}")
        print(f"  Original size: {result['total_size']:,} bytes ({result['total_size'] / 1024 / 1024:.2f} MB)")
        print(f"  Compressed size: {result['compressed_size']:,} bytes ({result['compressed_size'] / 1024 / 1024:.2f} MB)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print(f"  Space saved from deduplication: {result['statistics']['space_saved_from_deduplication']:,} bytes ({result['statistics']['space_saved_from_deduplication'] / 1024 / 1024:.2f} MB)")
        print(f"  Space saved from compression: {result['statistics']['space_saved_from_compression']:,} bytes ({result['statistics']['space_saved_from_compression'] / 1024 / 1024:.2f} MB)")
        print(f"  Total space saved: {result['statistics']['total_space_saved']:,} bytes ({result['statistics']['space_saved_percent']:.1f}%)")
        
        # Print compression method usage
        if 'method_usage' in result['statistics']:
            print("\n📊 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count} files")
        
        # Show sample of duplicate groups
        if result['duplicates'] and not quiet:
            print(f"\n🔄 Duplicate Groups (sample, {len(result['duplicates'])} total):")
            for idx, group in enumerate(result['duplicates'][:10], 1):
                print(f"  Group {idx}:")
                print(f"    Kept: {Path(group['kept']).name}")
                print(f"    Duplicates ({len(group['duplicates'])}):")
                for dup in group['duplicates'][:5]:
                    print(f"      - {Path(dup).name}")
                if len(group['duplicates']) > 5:
                    print(f"      ... and {len(group['duplicates']) - 5} more")
                print(f"    Size: {group['size']:,} bytes")
            
            if len(result['duplicates']) > 10:
                print(f"  ... and {len(result['duplicates']) - 10} more duplicate groups")
        
    except Exception as e:
        _print_error(f"Error creating archive: {e}", exit_code=1)


def _cmd_create_size_based(
    archive: Path,
    files: List[Path],
    compression: str = "deflate",
    small_threshold: int = 1024 * 1024,
    medium_threshold: int = 10 * 1024 * 1024,
    small_level: int = 3,
    medium_level: int = 6,
    large_level: int = 9,
    password: Optional[str] = None,
    password_file: Optional[Path] = None,
    no_preserve_metadata: bool = False,
    quiet: bool = False,
) -> None:
    """Create an archive with automatic compression level selection based on file size.
    
    This command creates archives using adaptive compression levels based on file size
    thresholds. Smaller files use lower compression levels (faster), while larger files
    use higher compression levels (better compression ratio).
    
    Args:
        archive: Path where the archive will be created.
        files: List of file/directory paths to add to archive.
        compression: Compression method ('stored', 'deflate', 'bzip2', 'lzma').
        small_threshold: Size threshold in bytes for small files (default: 1 MB).
        medium_threshold: Size threshold in bytes for medium files (default: 10 MB).
        small_level: Compression level for small files (0-9, default: 3).
        medium_level: Compression level for medium files (0-9, default: 6).
        large_level: Compression level for large files (0-9, default: 9).
        password: Password for encryption (ZIP only).
        password_file: File containing password for encryption.
        no_preserve_metadata: Do not preserve file metadata.
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_size_based_compression
    from .writer import ZipWriter
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}. Remove it first or choose a different path.", exit_code=2)
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Handle password
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    elif password_file:
        if not password_file.exists():
            _print_error(f"Password file not found: {password_file}", exit_code=2)
        try:
            password_bytes = password_file.read_bytes().strip()
        except Exception as e:
            _print_error(f"Error reading password file: {e}", exit_code=2)
    
    try:
        # Create progress callback
        def progress_callback(file_path: str, current: int, total: int, compression_level: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) Level {compression_level} - {Path(file_path).name}", end='\r')
        
        if not quiet:
            print(f"Creating archive with size-based compression: {archive}")
            print(f"Compression method: {compression}")
            print(f"Small files (< {_format_size(small_threshold)}): Level {small_level}")
            print(f"Medium files ({_format_size(small_threshold)} - {_format_size(medium_threshold)}): Level {medium_level}")
            print(f"Large files (> {_format_size(medium_threshold)}): Level {large_level}")
            print(f"Files/directories: {len(files)}")
            print("-" * 80)
        
        # Create archive with size-based compression
        result = create_archive_with_size_based_compression(
            archive_path=archive,
            file_paths=files,
            writer_class=ZipWriter,
            compression=compression,
            small_file_threshold=small_threshold,
            medium_file_threshold=medium_threshold,
            small_file_level=small_level,
            medium_file_level=medium_level,
            large_file_level=large_level,
            password=password_bytes,
            preserve_metadata=not no_preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        if not quiet:
            print()  # New line after progress
        
        # Print results
        print("✅ Archive created successfully!")
        print(f"  Archive: {result['archive_path']}")
        print(f"  Files added: {result['total_files']}")
        print(f"  Total size: {_format_size(result['total_size'])}")
        print(f"  Compressed size: {_format_size(result['compressed_size'])}")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print(f"  Space saved: {result['statistics']['space_saved_percent']:.1f}% ({_format_size(result['statistics']['total_space_saved'])})")
        
        print(f"\n📊 Size-Based Statistics:")
        print(f"  Small files (< {_format_size(small_threshold)}): {result['statistics']['small_files']} files ({_format_size(result['statistics']['small_files_size'])})")
        print(f"  Medium files ({_format_size(small_threshold)} - {_format_size(medium_threshold)}): {result['statistics']['medium_files']} files ({_format_size(result['statistics']['medium_files_size'])})")
        print(f"  Large files (> {_format_size(medium_threshold)}): {result['statistics']['large_files']} files ({_format_size(result['statistics']['large_files_size'])})")
        
        print(f"\n🔧 Compression Level Usage:")
        for level, count in sorted(result['statistics']['level_usage'].items()):
            print(f"  Level {level}: {count} files")
        
    except Exception as e:
        _print_error(f"Error creating archive: {e}", exit_code=1)


def _cmd_backup(
    base_archive: Path,
    files: List[Path],
    timestamp_format: str = "%Y-%m-%d_%H-%M-%S",
    include_timezone: bool = False,
    max_backups: Optional[int] = None,
    compression: str = "deflate",
    compression_level: int = 6,
    password: Optional[str] = None,
    password_file: Optional[Path] = None,
    no_preserve_metadata: bool = False,
    quiet: bool = False,
) -> None:
    """Create a timestamped backup archive with automatic versioning.
    
    This command creates archives with timestamps automatically appended to the filename,
    making it easy to create versioned backups. Optionally manages old backups by removing
    the oldest ones when a maximum count is reached.
    
    Args:
        base_archive: Base path for the archive (timestamp will be inserted before extension).
        files: List of file/directory paths to add to archive.
        timestamp_format: strftime format string for timestamp.
        include_timezone: Include timezone info in timestamp.
        max_backups: Maximum number of backups to keep (removes oldest if exceeded).
        compression: Compression method ('stored', 'deflate', 'bzip2', 'lzma').
        compression_level: Compression level (0-9).
        password: Password for encryption (ZIP only).
        password_file: File containing password for encryption.
        no_preserve_metadata: Do not preserve file metadata.
        quiet: Suppress progress output.
    """
    from .utils import create_timestamped_backup
    from .writer import ZipWriter
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Handle password
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    elif password_file:
        if not password_file.exists():
            _print_error(f"Password file not found: {password_file}", exit_code=2)
        try:
            password_bytes = password_file.read_bytes().strip()
        except Exception as e:
            _print_error(f"Error reading password file: {e}", exit_code=2)
    
    try:
        # Create progress callback
        def progress_callback(file_path: str, current: int, total: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {Path(file_path).name}", end='\r')
        
        if not quiet:
            print(f"Creating timestamped backup: {base_archive}")
            print(f"Timestamp format: {timestamp_format}")
            if max_backups:
                print(f"Max backups to keep: {max_backups}")
            print(f"Compression: {compression} level {compression_level}")
            print(f"Files/directories: {len(files)}")
            print("-" * 80)
        
        # Create timestamped backup
        result = create_timestamped_backup(
            base_archive_path=base_archive,
            file_paths=files,
            writer_class=ZipWriter,
            timestamp_format=timestamp_format,
            include_timezone=include_timezone,
            max_backups=max_backups,
            compression=compression,
            compression_level=compression_level,
            password=password_bytes,
            preserve_metadata=not no_preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        if not quiet:
            print()  # New line after progress
        
        # Print results
        print("✅ Backup created successfully!")
        print(f"  Backup archive: {result['archive_path']}")
        print(f"  Timestamp: {result['timestamp']}")
        print(f"  Files added: {result['total_files']}")
        print(f"  Total size: {_format_size(result['total_size'])}")
        print(f"  Compressed size: {_format_size(result['compressed_size'])}")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        
        if max_backups:
            print(f"\n📦 Backup Management:")
            print(f"  Backups kept: {result['backups_kept']}")
            if result['backups_removed'] > 0:
                print(f"  Backups removed: {result['backups_removed']}")
                if not quiet and result['removed_backups']:
                    print(f"  Removed backups:")
                    for removed in result['removed_backups'][:5]:
                        print(f"    - {Path(removed).name}")
                    if len(result['removed_backups']) > 5:
                        print(f"    ... and {len(result['removed_backups']) - 5} more")
        
    except Exception as e:
        _print_error(f"Error creating backup: {e}", exit_code=1)


def _cmd_create_content_based(
    archive: Path,
    files: List[Path],
    preset: str = "balanced",
    password: Optional[str] = None,
    password_file: Optional[Path] = None,
    no_preserve_metadata: bool = False,
    quiet: bool = False,
) -> None:
    """Create an archive with content-based file type detection and preset compression.
    
    This command creates archives using compression presets based on actual file content
    analysis (magic numbers/file signatures) rather than file extensions. This provides
    more accurate file type detection, especially for files without extensions or with
    incorrect extensions.
    
    Args:
        archive: Path where the archive will be created.
        files: List of file/directory paths to add to archive.
        preset: Compression preset ('balanced', 'maximum', 'fast').
        password: Password for encryption (ZIP only).
        password_file: File containing password for encryption.
        no_preserve_metadata: Do not preserve file metadata.
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_content_based_compression
    from .writer import ZipWriter
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}. Remove it first or choose a different path.", exit_code=2)
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Handle password
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    elif password_file:
        if not password_file.exists():
            _print_error(f"Password file not found: {password_file}", exit_code=2)
        try:
            password_bytes = password_file.read_bytes().strip()
        except Exception as e:
            _print_error(f"Error reading password file: {e}", exit_code=2)
    
    try:
        # Create progress callback
        def progress_callback(file_path: str, current: int, total: int, file_type: str) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {file_type} - {Path(file_path).name}", end='\r')
        
        if not quiet:
            print(f"Creating archive with content-based compression: {archive}")
            print(f"Compression preset: {preset}")
            print(f"Detection method: Content analysis (magic numbers/file signatures)")
            print(f"Files/directories: {len(files)}")
            print("-" * 80)
        
        # Create archive with content-based compression
        result = create_archive_with_content_based_compression(
            archive_path=archive,
            file_paths=files,
            writer_class=ZipWriter,
            preset=preset,
            password=password_bytes,
            preserve_metadata=not no_preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        if not quiet:
            print()  # New line after progress
        
        # Print results
        print("✅ Archive created successfully!")
        print(f"  Archive: {result['archive_path']}")
        print(f"  Files added: {result['total_files']}")
        print(f"  Total size: {_format_size(result['total_size'])}")
        print(f"  Compressed size: {_format_size(result['compressed_size'])}")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print(f"  Space saved: {result['statistics']['space_saved_percent']:.1f}% ({_format_size(result['statistics']['total_space_saved'])})")
        
        print(f"\n🔍 File Type Detection:")
        for file_type, count in sorted(result['statistics']['type_detection'].items()):
            print(f"  {file_type}: {count} files")
        
        print(f"\n🔧 Compression Method Usage:")
        for method, count in sorted(result['statistics']['method_usage'].items()):
            print(f"  {method}: {count} files")
        
    except Exception as e:
        _print_error(f"Error creating archive: {e}", exit_code=1)


def _cmd_create_incremental(
    archive: Path,
    files: List[Path],
    reference: Path,
    compare_by: str = "mtime",
    compression: str = "deflate",
    compression_level: int = 6,
    password: Optional[str] = None,
    password_file: Optional[Path] = None,
    reference_password: Optional[str] = None,
    reference_password_file: Optional[Path] = None,
    no_preserve_metadata: bool = False,
    quiet: bool = False,
) -> None:
    """Create an incremental archive containing only files changed since a reference archive.
    
    This command creates a new archive containing only files that have changed since
    a reference archive. Useful for creating incremental backups where you only
    store changed files, reducing archive size and backup time.
    
    Args:
        archive: Path where the incremental archive will be created.
        files: List of file/directory paths to check for changes.
        reference: Path to the reference archive to compare against.
        compare_by: How to detect changes ('mtime', 'size', 'both', 'hash').
        compression: Compression method ('stored', 'deflate', 'bzip2', 'lzma').
        compression_level: Compression level (0-9).
        password: Password for encryption (ZIP only).
        password_file: File containing password for encryption.
        reference_password: Password for reference archive (ZIP only).
        reference_password_file: File containing password for reference archive.
        no_preserve_metadata: Do not preserve file metadata.
        quiet: Suppress progress output.
    """
    from .utils import create_incremental_archive
    from .writer import ZipWriter
    from .reader import ZipReader
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}. Remove it first or choose a different path.", exit_code=2)
    
    # Validate reference archive exists
    if not reference.exists():
        _print_error(f"Reference archive not found: {reference}", exit_code=2)
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Handle password
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    elif password_file:
        if not password_file.exists():
            _print_error(f"Password file not found: {password_file}", exit_code=2)
        try:
            password_bytes = password_file.read_bytes().strip()
        except Exception as e:
            _print_error(f"Error reading password file: {e}", exit_code=2)
    
    # Handle reference password
    reference_password_bytes = None
    if reference_password:
        reference_password_bytes = reference_password.encode('utf-8')
    elif reference_password_file:
        if not reference_password_file.exists():
            _print_error(f"Reference password file not found: {reference_password_file}", exit_code=2)
        try:
            reference_password_bytes = reference_password_file.read_bytes().strip()
        except Exception as e:
            _print_error(f"Error reading reference password file: {e}", exit_code=2)
    
    try:
        # Create progress callback
        def progress_callback(file_path: str, current: int, total: int, status: str) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                status_symbol = {
                    'added': '✅',
                    'unchanged': '⏭️',
                    'error': '❌',
                }.get(status, '•')
                print(f"  [{current}/{total}] ({percent:.1f}%) {status_symbol} {Path(file_path).name}", end='\r')
        
        if not quiet:
            print(f"Creating incremental archive: {archive}")
            print(f"Reference archive: {reference}")
            print(f"Compare by: {compare_by}")
            print(f"Compression: {compression} level {compression_level}")
            print(f"Files/directories: {len(files)}")
            print("-" * 80)
        
        # Create incremental archive
        result = create_incremental_archive(
            archive_path=archive,
            file_paths=files,
            reference_archive_path=reference,
            writer_class=ZipWriter,
            reader_class=ZipReader,
            compare_by=compare_by,
            compression=compression,
            compression_level=compression_level,
            password=password_bytes,
            reference_password=reference_password_bytes,
            preserve_metadata=not no_preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        if not quiet:
            print()  # New line after progress
        
        # Print results
        print("✅ Incremental archive created successfully!")
        print(f"  Archive: {result['archive_path']}")
        print(f"  Reference: {result['reference_archive']}")
        print(f"  Files scanned: {result['total_files_scanned']}")
        print(f"  Files added (changed): {result['files_added']}")
        print(f"    - New files: {result['files_new']}")
        print(f"    - Modified files: {result['files_modified']}")
        print(f"  Files unchanged: {result['files_unchanged']}")
        print(f"  Total size: {_format_size(result['total_size'])}")
        print(f"  Compressed size: {_format_size(result['compressed_size'])}")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print(f"  Space saved: {result['statistics']['space_saved_percent']:.1f}% ({_format_size(result['statistics']['space_saved'])})")
        
    except Exception as e:
        _print_error(f"Error creating incremental archive: {e}", exit_code=1)


def _cmd_create_recent(
    archive: Path,
    files: List[Path],
    hours: Optional[int] = None,
    days: Optional[int] = None,
    compression: str = "deflate",
    compression_level: int = 6,
    password: Optional[str] = None,
    password_file: Optional[Path] = None,
    no_preserve_metadata: bool = False,
    quiet: bool = False,
) -> None:
    """Create an archive containing only files modified within a specified time period.
    
    This command creates archives containing only files that have been modified
    within a specified time period (e.g., last 24 hours, last 7 days). Useful for
    creating archives of recent changes or recent backups.
    
    Args:
        archive: Path where the archive will be created.
        files: List of file/directory paths to check.
        hours: Number of hours to look back (cannot be used with --days).
        days: Number of days to look back (cannot be used with --hours).
        compression: Compression method ('stored', 'deflate', 'bzip2', 'lzma').
        compression_level: Compression level (0-9).
        password: Password for encryption (ZIP only).
        password_file: File containing password for encryption.
        no_preserve_metadata: Do not preserve file metadata.
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_recent_files
    from .writer import ZipWriter
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}. Remove it first or choose a different path.", exit_code=2)
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate time period
    if hours is None and days is None:
        _print_error("Must specify either --hours or --days parameter", exit_code=2)
    
    if hours is not None and days is not None:
        _print_error("Cannot specify both --hours and --days parameters", exit_code=2)
    
    # Handle password
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    elif password_file:
        if not password_file.exists():
            _print_error(f"Password file not found: {password_file}", exit_code=2)
        try:
            password_bytes = password_file.read_bytes().strip()
        except Exception as e:
            _print_error(f"Error reading password file: {e}", exit_code=2)
    
    try:
        # Create progress callback
        def progress_callback(file_path: str, current: int, total: int, included: bool) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                status_symbol = '✅' if included else '⏭️'
                print(f"  [{current}/{total}] ({percent:.1f}%) {status_symbol} {Path(file_path).name}", end='\r')
        
        time_period_str = f"{hours} hours" if hours else f"{days} days"
        
        if not quiet:
            print(f"Creating archive with recent files: {archive}")
            print(f"Time period: {time_period_str}")
            print(f"Compression: {compression} level {compression_level}")
            print(f"Files/directories: {len(files)}")
            print("-" * 80)
        
        # Create archive with recent files
        result = create_archive_with_recent_files(
            archive_path=archive,
            file_paths=files,
            writer_class=ZipWriter,
            hours=hours,
            days=days,
            compression=compression,
            compression_level=compression_level,
            password=password_bytes,
            preserve_metadata=not no_preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        if not quiet:
            print()  # New line after progress
        
        # Print results
        print("✅ Archive created successfully!")
        print(f"  Archive: {result['archive_path']}")
        print(f"  Time period: {result['time_period']}")
        print(f"  Cutoff time: {result['cutoff_time']}")
        print(f"  Files scanned: {result['total_files_scanned']}")
        print(f"  Files included: {result['files_included']}")
        print(f"  Files excluded (too old): {result['files_excluded']}")
        print(f"  Total size: {_format_size(result['total_size'])}")
        print(f"  Compressed size: {_format_size(result['compressed_size'])}")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print(f"  Space saved: {result['statistics']['space_saved_percent']:.1f}% ({_format_size(result['statistics']['space_saved'])})")
        
    except Exception as e:
        _print_error(f"Error creating archive: {e}", exit_code=1)


def _cmd_create_organize(
    archive: Path,
    files: List[Path],
    organize_by: str = "type",
    preserve_original_structure: bool = False,
    compression: str = "deflate",
    compression_level: int = 6,
    password: Optional[str] = None,
    password_file: Optional[Path] = None,
    no_preserve_metadata: bool = False,
    quiet: bool = False,
) -> None:
    """Create an archive with automatic file organization into subdirectories.
    
    This command creates archives with files automatically organized into subdirectories
    based on specified criteria (type, date, size, or combinations). Useful for organizing
    messy directories or creating well-structured archives.
    
    Args:
        archive: Path where the archive will be created.
        files: List of file/directory paths to add to archive.
        organize_by: Organization method ('type', 'date', 'size', 'type_date', 'type_size').
        preserve_original_structure: Preserve original directory structure in addition to organization.
        compression: Compression method ('stored', 'deflate', 'bzip2', 'lzma').
        compression_level: Compression level (0-9).
        password: Password for encryption (ZIP only).
        password_file: File containing password for encryption.
        no_preserve_metadata: Do not preserve file metadata.
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_organization
    from .writer import ZipWriter
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}. Remove it first or choose a different path.", exit_code=2)
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Handle password
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    elif password_file:
        if not password_file.exists():
            _print_error(f"Password file not found: {password_file}", exit_code=2)
        try:
            password_bytes = password_file.read_bytes().strip()
        except Exception as e:
            _print_error(f"Error reading password file: {e}", exit_code=2)
    
    try:
        # Create progress callback
        def progress_callback(file_path: str, current: int, total: int, category: str) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) [{category}] {Path(file_path).name}", end='\r')
        
        if not quiet:
            print(f"Creating archive with organization: {archive}")
            print(f"Organization method: {organize_by}")
            if preserve_original_structure:
                print(f"Preserving original directory structure")
            print(f"Compression: {compression} level {compression_level}")
            print(f"Files/directories: {len(files)}")
            print("-" * 80)
        
        # Create archive with organization
        result = create_archive_with_organization(
            archive_path=archive,
            file_paths=files,
            writer_class=ZipWriter,
            organize_by=organize_by,
            compression=compression,
            compression_level=compression_level,
            preserve_original_structure=preserve_original_structure,
            password=password_bytes,
            preserve_metadata=not no_preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        if not quiet:
            print()  # New line after progress
        
        # Print results
        print("✅ Archive created successfully!")
        print(f"  Archive: {result['archive_path']}")
        print(f"  Organization method: {result['organization_method']}")
        print(f"  Files added: {result['total_files']}")
        print(f"  Total size: {_format_size(result['total_size'])}")
        print(f"  Compressed size: {_format_size(result['compressed_size'])}")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print(f"  Space saved: {result['statistics']['space_saved_percent']:.1f}% ({_format_size(result['statistics']['total_space_saved'])})")
        
        print(f"\n📁 Organization Categories:")
        for category, count in sorted(result['categories'].items()):
            print(f"  {category}: {count} files")
        
    except Exception as e:
        _print_error(f"Error creating archive: {e}", exit_code=1)


def _cmd_analyze_files(
    files: List[Path],
    sample_size: Optional[int] = None,
    no_content_analysis: bool = False,
    quiet: bool = False,
) -> None:
    """Analyze files before archiving and provide a detailed report.
    
    This command analyzes files and directories before archiving to provide comprehensive
    information about what will be archived. Helps users understand contents, estimate
    archive size, and make informed decisions about compression settings.
    
    Args:
        files: List of file/directory paths to analyze.
        sample_size: Maximum bytes to sample from each file for content analysis.
        no_content_analysis: Skip content-based file type detection.
        quiet: Suppress detailed output (show summary only).
    """
    from .utils import analyze_files_for_archiving
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    try:
        if not quiet:
            print(f"Analyzing files for archiving...")
            print(f"Files/directories: {len(files)}")
            if sample_size:
                print(f"Sample size: {sample_size:,} bytes per file")
            print("-" * 80)
        
        # Analyze files
        result = analyze_files_for_archiving(
            file_paths=files,
            sample_size=sample_size,
            include_content_analysis=not no_content_analysis,
        )
        
        # Print summary
        print("=" * 80)
        print("File Analysis Report")
        print("=" * 80)
        print()
        
        print("📊 Summary:")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total directories: {result['total_directories']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        if result['total_files'] > 0:
            avg_size = result['total_size'] / result['total_files']
            print(f"  Average file size: {_format_size(int(avg_size))}")
        print()
        
        if not quiet:
            print("📁 File Type Distribution:")
            for file_type, count in sorted(result['statistics']['by_type'].items(), key=lambda x: x[1], reverse=True):
                percent = (count / result['total_files'] * 100) if result['total_files'] > 0 else 0
                print(f"  {file_type}: {count:,} files ({percent:.1f}%)")
            print()
            
            print("📏 Size Distribution:")
            for size_cat, count in sorted(result['statistics']['by_size_category'].items()):
                percent = (count / result['total_files'] * 100) if result['total_files'] > 0 else 0
                print(f"  {size_cat}: {count:,} files ({percent:.1f}%)")
            print()
            
            if result['statistics']['largest_files']:
                print("📈 Largest Files (top 10):")
                for file_info in result['statistics']['largest_files']:
                    print(f"  {_format_size(file_info['size'])} - {Path(file_info['path']).name}")
                print()
            
            if result['statistics']['duplicate_groups']:
                print(f"🔄 Duplicate Files ({len(result['statistics']['duplicate_groups'])} groups):")
                total_duplicate_size = sum(group['size'] * (group['count'] - 1) for group in result['statistics']['duplicate_groups'])
                print(f"  Potential space savings: {_format_size(total_duplicate_size)}")
                if not quiet:
                    for idx, group in enumerate(result['statistics']['duplicate_groups'][:5], 1):
                        print(f"  Group {idx}: {group['count']} files ({_format_size(group['size'])})")
                        for file_path in group['files'][:3]:
                            print(f"    - {Path(file_path).name}")
                        if len(group['files']) > 3:
                            print(f"    ... and {len(group['files']) - 3} more")
                print()
        
        if result['statistics']['recommendations']:
            print("💡 Recommendations:")
            for rec in result['statistics']['recommendations']:
                print(f"  [{rec['type'].upper()}] {rec['message']}")
                if not quiet:
                    print(f"    → {rec['action']}")
            print()
        
        if not quiet:
            print("📅 Date Distribution (sample):")
            sorted_dates = sorted(result['statistics']['by_date'].items(), key=lambda x: x[0], reverse=True)[:10]
            for date_str, count in sorted_dates:
                print(f"  {date_str}: {count:,} files")
            print()
        
    except Exception as e:
        _print_error(f"Error analyzing files: {e}", exit_code=1)


def _cmd_create_embedded_metadata(
    archive: Path,
    files: List[Path],
    metadata_format: str = 'json',
    include_manifest: bool = True,
    include_checksums: bool = True,
    include_creation_info: bool = True,
    metadata_prefix: str = '.dnzip',
    compression: Optional[str] = None,
    compression_level: Optional[int] = None,
    preset: Optional[str] = None,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with embedded metadata files (manifest, checksums, creation info).
    
    This command creates archives and automatically embeds metadata files inside the archive
    itself. This makes archives self-documenting and verifiable without needing external files.
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        metadata_format: Format for metadata files ('json', 'csv', 'text').
        include_manifest: Include manifest file with file list and metadata.
        include_checksums: Include checksums file with hash values.
        include_creation_info: Include creation info file with archive metadata.
        metadata_prefix: Prefix directory for metadata files.
        compression: Uniform compression method for all files.
        compression_level: Compression level (0-9).
        preset: Compression preset ('balanced', 'maximum', 'fast').
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_embedded_metadata
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with embedded metadata: {archive}")
            print(f"Files/directories: {len(files)}")
            print(f"Metadata format: {metadata_format}")
            print(f"Metadata prefix: {metadata_prefix}")
            if include_manifest:
                print(f"  ✓ Manifest file")
            if include_checksums:
                print(f"  ✓ Checksums file")
            if include_creation_info:
                print(f"  ✓ Creation info file")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name}")
        
        # Create archive with embedded metadata
        result = create_archive_with_embedded_metadata(
            archive_path=archive,
            file_paths=files,
            metadata_format=metadata_format,
            include_manifest=include_manifest,
            include_checksums=include_checksums,
            include_creation_info=include_creation_info,
            metadata_prefix=metadata_prefix,
            compression=compression,
            compression_level=compression_level,
            preset=preset,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Metadata files: {result['metadata_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Metadata size: {_format_size(result['metadata_size'])} ({result['metadata_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        if result['metadata_files_list']:
            print("📄 Embedded Metadata Files:")
            for metadata_file in result['metadata_files_list']:
                print(f"  • {metadata_file}")
            print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_filter(
    archive: Path,
    files: List[Path],
    include_patterns: Optional[List[str]] = None,
    exclude_patterns: Optional[List[str]] = None,
    include_regex: Optional[List[str]] = None,
    exclude_regex: Optional[List[str]] = None,
    include_extensions: Optional[List[str]] = None,
    exclude_extensions: Optional[List[str]] = None,
    min_size: Optional[int] = None,
    max_size: Optional[int] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    case_sensitive: bool = False,
    compression: Optional[str] = None,
    compression_level: Optional[int] = None,
    preset: Optional[str] = None,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with advanced filtering criteria applied during creation.
    
    This command creates archives from files/directories while applying advanced
    filtering criteria. Files are filtered before being added to the archive.
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        include_patterns: List of glob patterns to include.
        exclude_patterns: List of glob patterns to exclude.
        include_regex: List of regular expression patterns to include.
        exclude_regex: List of regular expression patterns to exclude.
        include_extensions: List of file extensions to include.
        exclude_extensions: List of file extensions to exclude.
        min_size: Minimum file size in bytes.
        max_size: Maximum file size in bytes.
        start_date: Start date for modification time filter.
        end_date: End date for modification time filter.
        case_sensitive: Case-sensitive pattern matching.
        compression: Uniform compression method for all files.
        compression_level: Compression level (0-9).
        preset: Compression preset ('balanced', 'maximum', 'fast').
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_filter
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with filtering: {archive}")
            print(f"Files/directories: {len(files)}")
            if include_patterns:
                print(f"Include patterns: {include_patterns}")
            if exclude_patterns:
                print(f"Exclude patterns: {exclude_patterns}")
            if include_regex:
                print(f"Include regex: {include_regex}")
            if exclude_regex:
                print(f"Exclude regex: {exclude_regex}")
            if include_extensions:
                print(f"Include extensions: {include_extensions}")
            if exclude_extensions:
                print(f"Exclude extensions: {exclude_extensions}")
            if min_size is not None:
                print(f"Min size: {min_size:,} bytes")
            if max_size is not None:
                print(f"Max size: {max_size:,} bytes")
            if start_date:
                print(f"Start date: {start_date}")
            if end_date:
                print(f"End date: {end_date}")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name}")
        
        # Create archive with filtering
        result = create_archive_with_filter(
            archive_path=archive,
            file_paths=files,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            include_regex=include_regex,
            exclude_regex=exclude_regex,
            include_extensions=include_extensions,
            exclude_extensions=exclude_extensions,
            min_size=min_size,
            max_size=max_size,
            start_date=start_date,
            end_date=end_date,
            case_sensitive=case_sensitive,
            compression=compression,
            compression_level=compression_level,
            preset=preset,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Files scanned: {result['total_files_scanned']:,}")
        print(f"  Files added: {result['total_files_added']:,}")
        print(f"  Files filtered: {result['total_files_filtered']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        if result['statistics']['filter_reasons']:
            print("🔍 Filter Reasons:")
            for reason, count in sorted(result['statistics']['filter_reasons'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {reason}: {count:,} files")
            print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_verify(
    archive: Path,
    files: List[Path],
    verify_crc: bool = True,
    verify_size: bool = True,
    verify_decompression: bool = True,
    fail_fast: bool = False,
    compression: Optional[str] = None,
    compression_level: Optional[int] = None,
    preset: Optional[str] = None,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with automatic integrity verification during creation.
    
    This command creates archives and automatically verifies each file as it's
    added to ensure data integrity. After adding each file, it reads it back
    from the archive and verifies CRC32, size, and optionally decompresses
    the data to ensure it was added correctly.
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        verify_crc: Verify CRC32 checksum after adding each file.
        verify_size: Verify file size after adding each file.
        verify_decompression: Decompress and compare data to verify integrity.
        fail_fast: Stop on first verification failure.
        compression: Uniform compression method for all files.
        compression_level: Compression level (0-9).
        preset: Compression preset ('balanced', 'maximum', 'fast').
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_verification
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with verification: {archive}")
            print(f"Files/directories: {len(files)}")
            print(f"Verification checks:")
            if verify_crc:
                print(f"  ✓ CRC32 checksum")
            if verify_size:
                print(f"  ✓ File size")
            if verify_decompression:
                print(f"  ✓ Decompression (data comparison)")
            if fail_fast:
                print(f"  ⚠ Fail-fast mode (stop on first error)")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name}")
        
        # Create archive with verification
        result = create_archive_with_verification(
            archive_path=archive,
            file_paths=files,
            verify_crc=verify_crc,
            verify_size=verify_size,
            verify_decompression=verify_decompression,
            fail_fast=fail_fast,
            compression=compression,
            compression_level=compression_level,
            preset=preset,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        print("✅ Verification Results:")
        print(f"  Verified files: {result['verified_files']:,}")
        print(f"  Verification failures: {result['verification_failures']:,}")
        if result['statistics']['verification_passed']:
            print(f"  Status: ✓ All files verified successfully")
        else:
            print(f"  Status: ✗ Verification failed for {result['verification_failures']} files")
        print()
        
        if result['verification_errors']:
            print("❌ Verification Errors:")
            for error in result['verification_errors'][:10]:  # Show first 10 errors
                print(f"  [{error['error_type']}] {error['entry_name']}: {error['error_message']}")
            if len(result['verification_errors']) > 10:
                print(f"  ... and {len(result['verification_errors']) - 10} more errors")
            print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
        # Exit with error code if verification failed
        if not result['statistics']['verification_passed']:
            sys.exit(1)
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_optimize(
    archive: Path,
    files: List[Path],
    optimization_mode: str = 'best_compression',
    target_ratio: Optional[float] = None,
    max_iterations: int = 3,
    test_methods: Optional[List[str]] = None,
    test_levels: Optional[List[int]] = None,
    compression: Optional[str] = None,
    compression_level: Optional[int] = None,
    preset: Optional[str] = None,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with automatic compression optimization through iterative refinement.
    
    This command creates archives with automatic compression optimization by iteratively
    testing different compression settings and selecting the best ones.
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        optimization_mode: Optimization mode ('best_compression', 'target_ratio', 'balanced', 'size_reduction').
        target_ratio: Target compression ratio (0.0-1.0, required for target_ratio mode).
        max_iterations: Maximum number of optimization iterations.
        test_methods: List of compression methods to test during optimization.
        test_levels: List of compression levels to test.
        compression: Initial compression method for all files.
        compression_level: Initial compression level (0-9).
        preset: Initial compression preset ('balanced', 'maximum', 'fast').
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_compression_optimization
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with compression optimization: {archive}")
            print(f"Files/directories: {len(files)}")
            print(f"Optimization mode: {optimization_mode}")
            if target_ratio is not None:
                print(f"Target compression ratio: {target_ratio:.2%}")
            print(f"Max iterations: {max_iterations}")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, status: str) -> None:
            if not quiet:
                if status == 'optimizing':
                    print(f"  {status}: {entry_name}")
                else:
                    percent = (current / total * 100) if total > 0 else 0
                    print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} ({status})")
        
        # Create archive with compression optimization
        result = create_archive_with_compression_optimization(
            archive_path=archive,
            file_paths=files,
            optimization_mode=optimization_mode,
            target_ratio=target_ratio,
            max_iterations=max_iterations,
            test_methods=test_methods,
            test_levels=test_levels,
            compression=compression,
            compression_level=compression_level,
            preset=preset,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        print("🔧 Optimization Results:")
        print(f"  Optimization iterations: {result['optimization_iterations']}")
        if target_ratio is not None:
            print(f"  Target achieved: {'✓ Yes' if result['target_achieved'] else '✗ No'}")
        if result['optimization_history']:
            print(f"  Optimization history:")
            for hist in result['optimization_history']:
                print(f"    Iteration {hist['iteration']}: {_format_size(hist['compressed_size'])} ({hist['compression_ratio']:.2%}), {hist['improvements']} improvements")
        print()
        
        if result['statistics']['optimization_improvement'] > 0:
            print(f"  Optimization improvement: {_format_size(result['statistics']['optimization_improvement'])} ({result['statistics']['optimization_improvement_percent']:.1f}%)")
        print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_parallel(
    archive: Path,
    files: List[Path],
    auto_threads: bool = True,
    max_threads: Optional[int] = None,
    min_files_for_parallel: int = 4,
    compression: Optional[str] = None,
    compression_level: Optional[int] = None,
    preset: Optional[str] = None,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with automatic parallel compression optimization.
    
    This command creates archives using parallel compression with automatic
    thread count optimization based on file count, file sizes, and system resources.
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        auto_threads: Enable automatic thread count optimization.
        max_threads: Maximum number of threads to use.
        min_files_for_parallel: Minimum number of files for parallel compression.
        compression: Compression method for all files.
        compression_level: Compression level (0-9).
        preset: Compression preset ('balanced', 'maximum', 'fast').
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_parallel_compression
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with parallel compression: {archive}")
            print(f"Files/directories: {len(files)}")
            print(f"Auto threads: {auto_threads}")
            if max_threads is not None:
                print(f"Max threads: {max_threads}")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, threads: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [threads: {threads}]")
        
        # Create archive with parallel compression
        result = create_archive_with_parallel_compression(
            archive_path=archive,
            file_paths=files,
            auto_threads=auto_threads,
            max_threads=max_threads,
            min_files_for_parallel=min_files_for_parallel,
            compression=compression,
            compression_level=compression_level,
            preset=preset,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        print("⚡ Parallel Compression Results:")
        print(f"  Threads used: {result['threads_used']}")
        opt = result['thread_optimization']
        print(f"  Auto-determined: {'✓ Yes' if opt['auto_determined'] else '✗ No'}")
        print(f"  CPU cores: {opt['cpu_cores']}")
        print(f"  Optimization reason: {opt['optimization_reason']}")
        print(f"  Average file size: {_format_size(opt['average_file_size'])}")
        print(f"  Parallel compression: {'✓ Enabled' if result['statistics']['parallel_compression_enabled'] else '✗ Disabled'}")
        print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_redundant(
    archive: Path,
    files: List[Path],
    redundancy_mode: str = 'copies',
    num_copies: int = 2,
    redundancy_location: Optional[Path] = None,
    include_checksums: bool = True,
    checksum_algorithm: str = 'sha256',
    compression: Optional[str] = None,
    compression_level: Optional[int] = None,
    preset: Optional[str] = None,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with automatic redundancy for data protection.
    
    This command creates archives with redundancy features to protect against
    data loss. Supports multiple copies and/or embedded checksums.
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        redundancy_mode: Redundancy mode ('copies', 'checksums', 'both').
        num_copies: Number of copies to create.
        redundancy_location: Directory for storing redundant copies.
        include_checksums: Include embedded checksums.
        checksum_algorithm: Checksum algorithm ('md5', 'sha1', 'sha256', 'sha512').
        compression: Compression method for all files.
        compression_level: Compression level (0-9).
        preset: Compression preset ('balanced', 'maximum', 'fast').
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_redundancy
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with redundancy: {archive}")
            print(f"Files/directories: {len(files)}")
            print(f"Redundancy mode: {redundancy_mode}")
            if redundancy_mode in ('copies', 'both'):
                print(f"Number of copies: {num_copies}")
            if include_checksums and redundancy_mode in ('checksums', 'both'):
                print(f"Checksum algorithm: {checksum_algorithm}")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, status: str) -> None:
            if not quiet:
                if status == 'copying':
                    print(f"  {status}: {entry_name}")
                else:
                    percent = (current / total * 100) if total > 0 else 0
                    print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} ({status})")
        
        # Create archive with redundancy
        result = create_archive_with_redundancy(
            archive_path=archive,
            file_paths=files,
            redundancy_mode=redundancy_mode,
            num_copies=num_copies,
            redundancy_location=redundancy_location,
            include_checksums=include_checksums,
            checksum_algorithm=checksum_algorithm,
            compression=compression,
            compression_level=compression_level,
            preset=preset,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with Redundancy")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        print("🛡️ Redundancy Results:")
        print(f"  Redundancy mode: {result['redundancy_mode']}")
        print(f"  Copies created: {result['copies_created']}")
        if result['copy_paths']:
            print(f"  Copy paths:")
            for copy_path in result['copy_paths']:
                print(f"    - {copy_path}")
        print(f"  Checksums embedded: {'✓ Yes' if result['checksums_embedded'] else '✗ No'}")
        if result['checksums_embedded']:
            print(f"  Checksum algorithm: {result['checksum_algorithm']}")
        print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully with redundancy!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        print(f"   Total redundant size: {_format_size(result['statistics']['total_redundant_size'])}")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_retry(
    archive: Path,
    files: List[Path],
    max_retries: int = 3,
    retry_delay: float = 1.0,
    resume_on_interrupt: bool = True,
    compression: Optional[str] = None,
    compression_level: Optional[int] = None,
    preset: Optional[str] = None,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with automatic retry and resume capabilities.
    
    This command creates archives with automatic retry mechanisms and resume
    capabilities for interrupted operations.
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        max_retries: Maximum number of retry attempts.
        retry_delay: Delay in seconds between retry attempts.
        resume_on_interrupt: Enable resume capability.
        compression: Compression method for all files.
        compression_level: Compression level (0-9).
        preset: Compression preset ('balanced', 'maximum', 'fast').
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_retry
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with retry/resume: {archive}")
            print(f"Files/directories: {len(files)}")
            print(f"Max retries: {max_retries}")
            print(f"Resume on interrupt: {resume_on_interrupt}")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, status: str) -> None:
            if not quiet:
                if status in ('retrying', 'resuming'):
                    print(f"  {status}: {entry_name}")
                else:
                    percent = (current / total * 100) if total > 0 else 0
                    print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} ({status})")
        
        # Create archive with retry
        result = create_archive_with_retry(
            archive_path=archive,
            file_paths=files,
            max_retries=max_retries,
            retry_delay=retry_delay,
            resume_on_interrupt=resume_on_interrupt,
            compression=compression,
            compression_level=compression_level,
            preset=preset,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Files added: {result['files_added']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        print("🔄 Retry and Resume Results:")
        print(f"  Retry attempts: {result['retry_attempts']}")
        print(f"  Resumed: {'✓ Yes' if result['resumed'] else '✗ No'}")
        print(f"  Files skipped: {result['files_skipped']}")
        if result['statistics']['retry_info']['retry_reasons']:
            print(f"  Retry reasons:")
            for reason in result['statistics']['retry_info']['retry_reasons']:
                print(f"    - {reason}")
        print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_auto_format(
    archive: Path,
    files: List[Path],
    format_selection_strategy: str = 'best_compression',
    prefer_zip: bool = True,
    prefer_tar: bool = False,
    prefer_7z: bool = False,
    compression: Optional[str] = None,
    compression_level: Optional[int] = None,
    preset: Optional[str] = None,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with automatic format selection based on file characteristics.
    
    This command automatically selects the best archive format (ZIP, TAR, 7Z) based on
    file characteristics and selection strategy.
    
    Args:
        archive: Path to the archive file to create (extension may be changed).
        files: List of file/directory paths to add to archive.
        format_selection_strategy: Strategy for format selection.
        prefer_zip: Prefer ZIP format when suitable.
        prefer_tar: Prefer TAR format when suitable.
        prefer_7z: Prefer 7Z format when suitable.
        compression: Compression method for all files.
        compression_level: Compression level (0-9).
        preset: Compression preset ('balanced', 'maximum', 'fast', ZIP only).
        archive_comment: Archive comment.
        password: Password for encryption (ZIP only).
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, ZIP only).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_auto_format
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with automatic format selection: {archive}")
            print(f"Files/directories: {len(files)}")
            print(f"Format selection strategy: {format_selection_strategy}")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, format_selected: str) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [format: {format_selected}]")
        
        # Create archive with auto format
        result = create_archive_with_auto_format(
            archive_path=archive,
            file_paths=files,
            format_selection_strategy=format_selection_strategy,
            prefer_zip=prefer_zip,
            prefer_tar=prefer_tar,
            prefer_7z=prefer_7z,
            compression=compression,
            compression_level=compression_level,
            preset=preset,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with Auto Format Selection")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Format selected: {result['format_selected'].upper()}")
        print(f"  Selection reason: {result['format_selection_reason']}")
        if result['statistics']['format_alternatives']:
            print(f"  Alternative formats considered: {', '.join(result['statistics']['format_alternatives'])}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully in {result['format_selected'].upper()} format!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_entropy(
    archive: Path,
    files: List[Path],
    entropy_threshold: float = 7.5,
    sample_size: int = 8192,
    compression: str = 'deflate',
    compression_level: int = 6,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with automatic compression selection based on file entropy analysis.
    
    This command analyzes file entropy to automatically determine whether files should
    be compressed or stored uncompressed.
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        entropy_threshold: Entropy threshold for compression decision (0.0-8.0).
        sample_size: Maximum bytes to sample for entropy analysis.
        compression: Compression method for compressible files.
        compression_level: Compression level (0-9).
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_entropy_based_compression
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with entropy-based compression: {archive}")
            print(f"Files/directories: {len(files)}")
            print(f"Entropy threshold: {entropy_threshold}")
            print(f"Sample size: {sample_size if sample_size > 0 else 'entire file'}")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, comp_method: str, entropy: float) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [{comp_method}, entropy: {entropy:.2f}]")
        
        # Create archive with entropy-based compression
        result = create_archive_with_entropy_based_compression(
            archive_path=archive,
            file_paths=files,
            entropy_threshold=entropy_threshold,
            sample_size=sample_size if sample_size > 0 else None,
            compression=compression,
            compression_level=compression_level,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with Entropy-Based Compression")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        print("📊 Entropy Analysis Results:")
        entropy_stats = result['statistics']['entropy_analysis']
        print(f"  High entropy files (stored): {entropy_stats['high_entropy_files']:,}")
        print(f"  Low entropy files (compressed): {entropy_stats['low_entropy_files']:,}")
        print(f"  Average entropy: {entropy_stats['average_entropy']:.2f}")
        print(f"  Min entropy: {entropy_stats['min_entropy']:.2f}")
        print(f"  Max entropy: {entropy_stats['max_entropy']:.2f}")
        print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_pattern(
    archive: Path,
    files: List[Path],
    pattern_analysis_size: int = 16384,
    compression: str = 'deflate',
    compression_level: int = 6,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with automatic compression selection based on file data patterns.
    
    This command analyzes file data patterns to automatically select the best compression
    method based on detected patterns (repetitive, structured text, text, binary, compressed).
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        pattern_analysis_size: Maximum bytes to analyze for pattern detection.
        compression: Default compression method for files without strong pattern match.
        compression_level: Compression level (0-9).
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_pattern_based_compression
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with pattern-based compression: {archive}")
            print(f"Files/directories: {len(files)}")
            print(f"Pattern analysis size: {pattern_analysis_size if pattern_analysis_size > 0 else 'entire file'}")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, comp_method: str) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [{comp_method}]")
        
        # Create archive with pattern-based compression
        result = create_archive_with_pattern_based_compression(
            archive_path=archive,
            file_paths=files,
            pattern_analysis_size=pattern_analysis_size if pattern_analysis_size > 0 else None,
            compression=compression,
            compression_level=compression_level,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with Pattern-Based Compression")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        print("🔍 Pattern Detection Results:")
        for pattern_type, count in sorted(result['statistics']['pattern_detection'].items(), key=lambda x: x[1], reverse=True):
            print(f"  {pattern_type}: {count:,} files")
        print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_time_based(
    archive: Path,
    files: List[Path],
    recent_threshold_days: int = 7,
    old_threshold_days: int = 90,
    recent_compression: str = 'deflate',
    recent_level: int = 6,
    old_compression: str = 'lzma',
    old_level: int = 9,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with automatic compression selection based on file modification time.
    
    This command analyzes file modification times to automatically select compression
    methods and levels. Recent files use faster compression, while old files use
    better compression for archival purposes.
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        recent_threshold_days: Number of days to consider a file 'recent' (default: 7).
        old_threshold_days: Number of days to consider a file 'old' (default: 90).
        recent_compression: Compression method for recent files (default: deflate).
        recent_level: Compression level for recent files (0-9, default: 6).
        old_compression: Compression method for old files (default: lzma).
        old_level: Compression level for old files (0-9, default: 9).
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_time_based_compression
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with time-based compression: {archive}")
            print(f"Files/directories: {len(files)}")
            print(f"Recent threshold: {recent_threshold_days} days")
            print(f"Old threshold: {old_threshold_days} days")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, comp_method: str, comp_level: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [{comp_method} level {comp_level}]")
        
        # Create archive with time-based compression
        result = create_archive_with_time_based_compression(
            archive_path=archive,
            file_paths=files,
            recent_threshold_days=recent_threshold_days,
            old_threshold_days=old_threshold_days,
            recent_compression=recent_compression,
            recent_level=recent_level,
            old_compression=old_compression,
            old_level=old_level,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with Time-Based Compression")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        print("⏰ File Age Categories:")
        print(f"  Recent files (< {recent_threshold_days} days): {result['statistics']['recent_files']:,} files ({_format_size(result['statistics']['recent_files_size'])})")
        print(f"  Medium files ({recent_threshold_days}-{old_threshold_days} days): {result['statistics']['medium_files']:,} files ({_format_size(result['statistics']['medium_files_size'])})")
        print(f"  Old files (> {old_threshold_days} days): {result['statistics']['old_files']:,} files ({_format_size(result['statistics']['old_files_size'])})")
        print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_creation_based(
    archive: Path,
    files: List[Path],
    recent_threshold_days: int = 7,
    old_threshold_days: int = 90,
    recent_compression: str = 'deflate',
    recent_level: int = 6,
    old_compression: str = 'lzma',
    old_level: int = 9,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with automatic compression selection based on file creation time.
    
    This command analyzes file creation times (ctime) to automatically select compression
    methods and levels. Recently created files use faster compression, while old files use
    better compression for archival purposes.
    
    Note: Creation time (ctime) behavior varies by system:
    - On Unix: ctime represents metadata change time, not just creation time
    - On Windows: ctime represents actual creation time
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        recent_threshold_days: Number of days to consider a file 'recently created' (default: 7).
        old_threshold_days: Number of days to consider a file 'old' (default: 90).
        recent_compression: Compression method for recently created files (default: deflate).
        recent_level: Compression level for recently created files (0-9, default: 6).
        old_compression: Compression method for old files (default: lzma).
        old_level: Compression level for old files (0-9, default: 9).
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_creation_based_compression
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with creation-based compression: {archive}")
            print(f"Files/directories: {len(files)}")
            print(f"Recent threshold: {recent_threshold_days} days")
            print(f"Old threshold: {old_threshold_days} days")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, comp_method: str, comp_level: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [{comp_method} level {comp_level}]")
        
        # Create archive with creation-based compression
        result = create_archive_with_creation_based_compression(
            archive_path=archive,
            file_paths=files,
            recent_threshold_days=recent_threshold_days,
            old_threshold_days=old_threshold_days,
            recent_compression=recent_compression,
            recent_level=recent_level,
            old_compression=old_compression,
            old_level=old_level,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with Creation-Based Compression")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        print("📅 File Creation Age Categories:")
        print(f"  Recent files (< {recent_threshold_days} days): {result['statistics']['recent_files']:,} files ({_format_size(result['statistics']['recent_files_size'])})")
        print(f"  Medium files ({recent_threshold_days}-{old_threshold_days} days): {result['statistics']['medium_files']:,} files ({_format_size(result['statistics']['medium_files_size'])})")
        print(f"  Old files (> {old_threshold_days} days): {result['statistics']['old_files']:,} files ({_format_size(result['statistics']['old_files_size'])})")
        print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_permission_based(
    archive: Path,
    files: List[Path],
    permission_rules: Optional[str] = None,
    default_compression: str = 'deflate',
    default_level: int = 6,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with automatic compression selection based on file permissions.
    
    This command analyzes file permissions (mode bits) to automatically select compression
    methods and levels. Files with different permission patterns can use different
    compression strategies.
    
    Permission patterns supported:
    - 'executable': Files with execute permission
    - 'readonly': Files without write permission
    - 'writable': Files with write permission
    - 'owner_executable': Files with owner execute permission
    - 'group_executable': Files with group execute permission
    - 'other_executable': Files with other execute permission
    - Octal mode strings (e.g., '0755', '0644')
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        permission_rules: JSON string mapping permission patterns to compression settings.
        default_compression: Default compression method for files not matching any rule.
        default_level: Default compression level for files not matching any rule (0-9).
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    import json
    from .utils import create_archive_with_permission_based_compression
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Parse permission rules from JSON string
    permission_rules_dict = None
    if permission_rules:
        try:
            permission_rules_dict = json.loads(permission_rules)
        except json.JSONDecodeError as e:
            _print_error(f"Invalid JSON in permission rules: {e}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with permission-based compression: {archive}")
            print(f"Files/directories: {len(files)}")
            if permission_rules_dict:
                print(f"Permission rules: {len(permission_rules_dict)} patterns")
            print(f"Default compression: {default_compression} level {default_level}")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, comp_method: str, comp_level: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [{comp_method} level {comp_level}]")
        
        # Create archive with permission-based compression
        result = create_archive_with_permission_based_compression(
            archive_path=archive,
            file_paths=files,
            permission_rules=permission_rules_dict,
            default_compression=default_compression,
            default_level=default_level,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with Permission-Based Compression")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        if result['statistics']['pattern_matches']:
            print("🔐 Permission Pattern Matches:")
            for pattern, count in sorted(result['statistics']['pattern_matches'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {pattern}: {count:,} files")
            print()
        
        print(f"📊 Default matches: {result['statistics']['default_matches']:,} files")
        print()
        
        if result['statistics']['permission_distribution']:
            print("📋 Permission Distribution:")
            for perm_type, count in sorted(result['statistics']['permission_distribution'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {perm_type}: {count:,} files")
            print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_owner_based(
    archive: Path,
    files: List[Path],
    owner_rules: Optional[str] = None,
    group_rules: Optional[str] = None,
    default_compression: str = 'deflate',
    default_level: int = 6,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with automatic compression selection based on file owner and group.
    
    This command analyzes file ownership (UID/GID or username/groupname) to automatically
    select compression methods and levels. Files owned by different users or groups can
    use different compression strategies.
    
    Owner rules take precedence over group rules when both match. Supports both
    username/groupname and UID/GID matching.
    
    Note: On Unix systems, owner/group names are resolved from UID/GID using pwd/grp.
    On Windows, owner/group information may be limited.
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        owner_rules: JSON string mapping owner identifiers (username or UID) to compression settings.
        group_rules: JSON string mapping group identifiers (groupname or GID) to compression settings.
        default_compression: Default compression method for files not matching any rule.
        default_level: Default compression level for files not matching any rule (0-9).
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    import json
    from .utils import create_archive_with_owner_based_compression
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Parse owner and group rules from JSON strings
    owner_rules_dict = None
    if owner_rules:
        try:
            owner_rules_dict = json.loads(owner_rules)
            # Convert numeric string keys to integers for UID matching
            normalized_owner_rules = {}
            for key, value in owner_rules_dict.items():
                try:
                    # Try to convert to int if it's a numeric string
                    int_key = int(key)
                    normalized_owner_rules[int_key] = value
                    # Also keep string key for username matching
                    if key != str(int_key):
                        normalized_owner_rules[key] = value
                except (ValueError, TypeError):
                    # Keep as string for username matching
                    normalized_owner_rules[key] = value
            owner_rules_dict = normalized_owner_rules
        except json.JSONDecodeError as e:
            _print_error(f"Invalid JSON in owner rules: {e}", exit_code=2)
    
    group_rules_dict = None
    if group_rules:
        try:
            group_rules_dict = json.loads(group_rules)
            # Convert numeric string keys to integers for GID matching
            normalized_group_rules = {}
            for key, value in group_rules_dict.items():
                try:
                    # Try to convert to int if it's a numeric string
                    int_key = int(key)
                    normalized_group_rules[int_key] = value
                    # Also keep string key for groupname matching
                    if key != str(int_key):
                        normalized_group_rules[key] = value
                except (ValueError, TypeError):
                    # Keep as string for groupname matching
                    normalized_group_rules[key] = value
            group_rules_dict = normalized_group_rules
        except json.JSONDecodeError as e:
            _print_error(f"Invalid JSON in group rules: {e}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with owner-based compression: {archive}")
            print(f"Files/directories: {len(files)}")
            if owner_rules_dict:
                print(f"Owner rules: {len(owner_rules_dict)} patterns")
            if group_rules_dict:
                print(f"Group rules: {len(group_rules_dict)} patterns")
            print(f"Default compression: {default_compression} level {default_level}")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, comp_method: str, comp_level: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [{comp_method} level {comp_level}]")
        
        # Create archive with owner-based compression
        result = create_archive_with_owner_based_compression(
            archive_path=archive,
            file_paths=files,
            owner_rules=owner_rules_dict,
            group_rules=group_rules_dict,
            default_compression=default_compression,
            default_level=default_level,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with Owner-Based Compression")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        if result['statistics']['owner_matches']:
            print("👤 Owner Pattern Matches:")
            for owner, count in sorted(result['statistics']['owner_matches'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {owner}: {count:,} files")
            print()
        
        if result['statistics']['group_matches']:
            print("👥 Group Pattern Matches:")
            for group, count in sorted(result['statistics']['group_matches'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {group}: {count:,} files")
            print()
        
        print(f"📊 Default matches: {result['statistics']['default_matches']:,} files")
        print()
        
        if result['statistics']['owner_distribution']:
            print("📋 Owner Distribution:")
            for owner, count in sorted(result['statistics']['owner_distribution'].items(), key=lambda x: x[1], reverse=True)[:10]:
                print(f"  {owner}: {count:,} files")
            if len(result['statistics']['owner_distribution']) > 10:
                print(f"  ... and {len(result['statistics']['owner_distribution']) - 10} more owners")
            print()
        
        if result['statistics']['group_distribution']:
            print("📋 Group Distribution:")
            for group, count in sorted(result['statistics']['group_distribution'].items(), key=lambda x: x[1], reverse=True)[:10]:
                print(f"  {group}: {count:,} files")
            if len(result['statistics']['group_distribution']) > 10:
                print(f"  ... and {len(result['statistics']['group_distribution']) - 10} more groups")
            print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        if result['statistics']['name_resolution_available']:
            print("ℹ️  Owner/group name resolution: Available")
        else:
            print("ℹ️  Owner/group name resolution: Not available (using UID/GID only)")
        print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_path_based(
    archive: Path,
    files: List[Path],
    path_patterns: Optional[str] = None,
    default_compression: str = 'deflate',
    default_level: int = 6,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with automatic compression selection based on file path patterns.
    
    This command analyzes file paths to automatically select compression methods and
    levels based on directory structure, path patterns, or naming conventions.
    
    Path patterns support glob-style matching (e.g., '*.log', '**/temp/**', 'data/**').
    Patterns are matched in order, first match wins.
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        path_patterns: JSON string mapping path patterns to compression settings.
        default_compression: Default compression method for files not matching any pattern.
        default_level: Default compression level for files not matching any pattern (0-9).
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    import json
    from .utils import create_archive_with_path_based_compression
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Parse path patterns from JSON string
    path_patterns_dict = None
    if path_patterns:
        try:
            path_patterns_dict = json.loads(path_patterns)
        except json.JSONDecodeError as e:
            _print_error(f"Invalid JSON in path patterns: {e}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with path-based compression: {archive}")
            print(f"Files/directories: {len(files)}")
            if path_patterns_dict:
                print(f"Path patterns: {len(path_patterns_dict)} patterns")
            print(f"Default compression: {default_compression} level {default_level}")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, comp_method: str, comp_level: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [{comp_method} level {comp_level}]")
        
        # Create archive with path-based compression
        result = create_archive_with_path_based_compression(
            archive_path=archive,
            file_paths=files,
            path_patterns=path_patterns_dict,
            default_compression=default_compression,
            default_level=default_level,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with Path-Based Compression")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        if result['statistics']['pattern_matches']:
            print("📁 Path Pattern Matches:")
            for pattern, count in sorted(result['statistics']['pattern_matches'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {pattern}: {count:,} files")
            print()
        
        print(f"📊 Default matches: {result['statistics']['default_matches']:,} files")
        print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_extension_based(
    archive: Path,
    files: List[Path],
    extension_rules: Optional[str] = None,
    default_compression: str = 'deflate',
    default_level: int = 6,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with automatic compression selection based on file extensions.
    
    This command analyzes file extensions to automatically select compression methods
    and levels. Files with different extensions can use different compression strategies.
    
    Extension matching is case-insensitive. Extensions should include the leading dot
    (e.g., '.txt', '.jpg', '.zip').
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        extension_rules: JSON string mapping file extensions to compression settings.
        default_compression: Default compression method for files not matching any rule.
        default_level: Default compression level for files not matching any rule (0-9).
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    import json
    from .utils import create_archive_with_extension_based_compression
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Parse extension rules from JSON string
    extension_rules_dict = None
    if extension_rules:
        try:
            extension_rules_dict = json.loads(extension_rules)
        except json.JSONDecodeError as e:
            _print_error(f"Invalid JSON in extension rules: {e}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with extension-based compression: {archive}")
            print(f"Files/directories: {len(files)}")
            if extension_rules_dict:
                print(f"Extension rules: {len(extension_rules_dict)} patterns")
            print(f"Default compression: {default_compression} level {default_level}")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, comp_method: str, comp_level: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [{comp_method} level {comp_level}]")
        
        # Create archive with extension-based compression
        result = create_archive_with_extension_based_compression(
            archive_path=archive,
            file_paths=files,
            extension_rules=extension_rules_dict,
            default_compression=default_compression,
            default_level=default_level,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with Extension-Based Compression")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        if result['statistics']['extension_matches']:
            print("📄 Extension Pattern Matches:")
            for ext, count in sorted(result['statistics']['extension_matches'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {ext}: {count:,} files")
            print()
        
        print(f"📊 Default matches: {result['statistics']['default_matches']:,} files")
        print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_mime_based(
    archive: Path,
    files: List[Path],
    mime_rules: Optional[str] = None,
    default_compression: str = 'deflate',
    default_level: int = 6,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with automatic compression selection based on MIME type detection.
    
    This command analyzes file MIME types (detected from content and extension) to
    automatically select compression methods and levels. Files with different MIME types
    can use different compression strategies.
    
    MIME rules support exact MIME types (e.g., 'text/plain', 'image/jpeg') and wildcard
    patterns (e.g., 'text/*', 'image/*', '*/json').
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        mime_rules: JSON string mapping MIME types/patterns to compression settings.
        default_compression: Default compression method for files not matching any rule.
        default_level: Default compression level for files not matching any rule (0-9).
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    import json
    from .utils import create_archive_with_mime_based_compression
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Parse MIME rules from JSON string
    mime_rules_dict = None
    if mime_rules:
        try:
            mime_rules_dict = json.loads(mime_rules)
        except json.JSONDecodeError as e:
            _print_error(f"Invalid JSON in MIME rules: {e}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with MIME-based compression: {archive}")
            print(f"Files/directories: {len(files)}")
            if mime_rules_dict:
                print(f"MIME rules: {len(mime_rules_dict)} patterns")
            print(f"Default compression: {default_compression} level {default_level}")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, comp_method: str, comp_level: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [{comp_method} level {comp_level}]")
        
        # Create archive with MIME-based compression
        result = create_archive_with_mime_based_compression(
            archive_path=archive,
            file_paths=files,
            mime_rules=mime_rules_dict,
            default_compression=default_compression,
            default_level=default_level,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with MIME-Based Compression")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        print("🔍 MIME Type Distribution:")
        if result['statistics']['mime_matches']:
            print(f"  MIME pattern matches:")
            for mime_pattern, count in sorted(result['statistics']['mime_matches'].items(), key=lambda x: x[1], reverse=True):
                print(f"    {mime_pattern}: {count:,} files")
        print(f"  Default compression: {result['statistics']['default_matches']:,} files")
        if result['statistics']['mime_distribution']:
            print(f"  Detected MIME types:")
            for mime_type, count in sorted(result['statistics']['mime_distribution'].items(), key=lambda x: x[1], reverse=True)[:10]:
                print(f"    {mime_type}: {count:,} files")
        print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_hybrid(
    archive: Path,
    files: List[Path],
    strategies: Optional[str] = None,
    strategy_weights: Optional[str] = None,
    default_compression: str = 'deflate',
    default_level: int = 6,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with automatic compression selection using hybrid multi-strategy approach.
    
    This command combines multiple compression selection strategies to automatically
    select the best compression method and level for each file. It evaluates files using
    multiple criteria (size, type, entropy, age, etc.) and combines the results using
    weighted scoring.
    
    Available strategies: 'size', 'type', 'entropy', 'age', 'access', 'path', 'extension'
    Default strategies: ['size', 'type', 'entropy']
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        strategies: Comma-separated list of strategy names to use (default: 'size,type,entropy').
        strategy_weights: JSON string mapping strategy names to weights (0.0-1.0).
        default_compression: Default compression method when strategies don't agree.
        default_level: Default compression level when strategies don't agree (0-9).
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    import json
    from .utils import create_archive_with_hybrid_compression
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Parse strategies from comma-separated string
    strategies_list = None
    if strategies:
        strategies_list = [s.strip() for s in strategies.split(',')]
        valid_strategies = {'size', 'type', 'entropy', 'age', 'access', 'path', 'extension'}
        for strategy in strategies_list:
            if strategy not in valid_strategies:
                _print_error(f"Invalid strategy: {strategy}. Valid strategies: {sorted(valid_strategies)}", exit_code=2)
    
    # Parse strategy weights from JSON string
    strategy_weights_dict = None
    if strategy_weights:
        try:
            strategy_weights_dict = json.loads(strategy_weights)
        except json.JSONDecodeError as e:
            _print_error(f"Invalid JSON in strategy weights: {e}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with hybrid compression: {archive}")
            print(f"Files/directories: {len(files)}")
            if strategies_list:
                print(f"Strategies: {', '.join(strategies_list)}")
            else:
                print(f"Strategies: size, type, entropy (default)")
            if strategy_weights_dict:
                print(f"Strategy weights: {strategy_weights_dict}")
            print(f"Default compression: {default_compression} level {default_level}")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, comp_method: str, comp_level: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [{comp_method} level {comp_level}]")
        
        # Create archive with hybrid compression
        result = create_archive_with_hybrid_compression(
            archive_path=archive,
            file_paths=files,
            strategies=strategies_list,
            strategy_weights=strategy_weights_dict,
            default_compression=default_compression,
            default_level=default_level,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with Hybrid Compression")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        print("🎯 Strategy Usage:")
        if result['statistics']['strategy_usage']:
            for strategy, count in sorted(result['statistics']['strategy_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {strategy}: {count:,} files")
        print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_metadata_combined(
    archive: Path,
    files: List[Path],
    metadata_rules: Optional[str] = None,
    default_compression: str = 'deflate',
    default_level: int = 6,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with automatic compression selection based on combined file metadata.
    
    This command analyzes multiple file metadata attributes simultaneously to automatically
    select compression methods and levels. It combines file size, type, age, permissions,
    owner, group, path depth, and extension to make compression decisions.
    
    Metadata rules support conditions on multiple attributes. Rules are evaluated in order,
    first match wins. Files not matching any rule use default compression.
    
    Available metadata attributes:
    - 'size': File size category ('small' <1KB, 'medium' 1KB-1MB, 'large' >=1MB)
    - 'type': File type category ('text', 'image', 'archive', 'document', 'binary')
    - 'age': File age category ('recent' <7d, 'medium' 7-90d, 'old' >90d)
    - 'permission': File permission category ('executable', 'readonly', 'writable')
    - 'owner': File owner (UID or username if available)
    - 'group': File group (GID or groupname if available)
    - 'depth': Directory depth (number of path levels)
    - 'extension': File extension (lowercase, with leading dot)
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        metadata_rules: JSON string with list of rule dictionaries. Each rule should contain
                       metadata conditions and compression settings. Example:
                       '[{"size":"large","type":"text","compression":"lzma","level":9},
                         {"age":"old","permission":"readonly","compression":"lzma","level":9}]'
        default_compression: Default compression method for files not matching any rule.
        default_level: Default compression level for files not matching any rule (0-9).
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    import json
    from .utils import create_archive_with_metadata_combined_compression
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Parse metadata rules from JSON string
    metadata_rules_list = None
    if metadata_rules:
        try:
            metadata_rules_list = json.loads(metadata_rules)
            if not isinstance(metadata_rules_list, list):
                _print_error("Metadata rules must be a JSON array of rule objects", exit_code=2)
        except json.JSONDecodeError as e:
            _print_error(f"Invalid JSON in metadata rules: {e}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with metadata-combined compression: {archive}")
            print(f"Files/directories: {len(files)}")
            if metadata_rules_list:
                print(f"Metadata rules: {len(metadata_rules_list)} rules")
            print(f"Default compression: {default_compression} level {default_level}")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, comp_method: str, comp_level: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [{comp_method} level {comp_level}]")
        
        # Create archive with metadata-combined compression
        result = create_archive_with_metadata_combined_compression(
            archive_path=archive,
            file_paths=files,
            metadata_rules=metadata_rules_list,
            default_compression=default_compression,
            default_level=default_level,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with Metadata-Combined Compression")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        print("🎯 Rule Match Statistics:")
        if result['statistics']['rule_matches']:
            print(f"  Rule matches:")
            for rule_idx, count in sorted(result['statistics']['rule_matches'].items(), key=lambda x: int(x[0])):
                print(f"    Rule {rule_idx}: {count:,} files")
        print(f"  Default compression: {result['statistics']['default_matches']:,} files")
        print()
        
        print("📊 Metadata Distribution:")
        if result['statistics']['metadata_distribution']:
            # Group by category
            size_dist = {k.split(':', 1)[1]: v for k, v in result['statistics']['metadata_distribution'].items() if k.startswith('size:')}
            type_dist = {k.split(':', 1)[1]: v for k, v in result['statistics']['metadata_distribution'].items() if k.startswith('type:')}
            age_dist = {k.split(':', 1)[1]: v for k, v in result['statistics']['metadata_distribution'].items() if k.startswith('age:')}
            perm_dist = {k.split(':', 1)[1]: v for k, v in result['statistics']['metadata_distribution'].items() if k.startswith('permission:')}
            
            if size_dist:
                print(f"  Size categories:")
                for size_cat, count in sorted(size_dist.items(), key=lambda x: x[1], reverse=True):
                    print(f"    {size_cat}: {count:,} files")
            if type_dist:
                print(f"  Type categories:")
                for type_cat, count in sorted(type_dist.items(), key=lambda x: x[1], reverse=True):
                    print(f"    {type_cat}: {count:,} files")
            if age_dist:
                print(f"  Age categories:")
                for age_cat, count in sorted(age_dist.items(), key=lambda x: x[1], reverse=True):
                    print(f"    {age_cat}: {count:,} files")
            if perm_dist:
                print(f"  Permission categories:")
                for perm_cat, count in sorted(perm_dist.items(), key=lambda x: x[1], reverse=True):
                    print(f"    {perm_cat}: {count:,} files")
        print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_relationship_based(
    archive: Path,
    files: List[Path],
    relationship_detection: str = 'path',
    relationship_threshold: float = 0.7,
    group_compression: str = 'deflate',
    group_level: int = 6,
    default_compression: str = 'deflate',
    default_level: int = 6,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with automatic compression selection based on file relationships.
    
    This command analyzes file relationships to group related files together and apply
    similar compression strategies. Related files are identified based on path similarity,
    naming patterns, directory structure, or content similarity.
    
    Relationship detection methods:
    - 'path': Group files by path similarity (common directory structure)
    - 'naming': Group files by naming pattern similarity
    - 'extension': Group files by extension (same file type)
    - 'directory': Group files by immediate parent directory
    - 'hybrid': Combine multiple relationship detection methods
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        relationship_detection: Method for detecting file relationships (default: 'path').
        relationship_threshold: Similarity threshold (0.0-1.0 for 'path'/'naming', 1-3 for 'hybrid').
        group_compression: Compression method for related file groups.
        group_level: Compression level for related file groups (0-9).
        default_compression: Default compression method for files not in any group.
        default_level: Default compression level for files not in any group (0-9).
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_relationship_based_compression
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Validate relationship_detection
    valid_methods = {'path', 'naming', 'extension', 'directory', 'hybrid'}
    if relationship_detection not in valid_methods:
        _print_error(f"Invalid relationship_detection: {relationship_detection}. Valid: {sorted(valid_methods)}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with relationship-based compression: {archive}")
            print(f"Files/directories: {len(files)}")
            print(f"Relationship detection: {relationship_detection}")
            print(f"Relationship threshold: {relationship_threshold}")
            print(f"Group compression: {group_compression} level {group_level}")
            print(f"Default compression: {default_compression} level {default_level}")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, comp_method: str, comp_level: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [{comp_method} level {comp_level}]")
        
        # Create archive with relationship-based compression
        result = create_archive_with_relationship_based_compression(
            archive_path=archive,
            file_paths=files,
            relationship_detection=relationship_detection,
            relationship_threshold=relationship_threshold,
            group_compression=group_compression,
            group_level=group_level,
            default_compression=default_compression,
            default_level=default_level,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with Relationship-Based Compression")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        print("🔗 Relationship Groups:")
        print(f"  Relationship groups: {result['statistics']['relationship_groups']:,}")
        print(f"  Grouped files: {result['statistics']['grouped_files']:,}")
        print(f"  Ungrouped files: {result['statistics']['ungrouped_files']:,}")
        if result['statistics']['group_sizes']:
            print(f"  Group size distribution:")
            for group_id, size in sorted(result['statistics']['group_sizes'].items(), key=lambda x: x[1], reverse=True)[:10]:
                print(f"    Group {group_id}: {size:,} files")
        print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_stability_based(
    archive: Path,
    files: List[Path],
    stable_threshold_ratio: float = 0.8,
    unstable_threshold_ratio: float = 0.2,
    stable_compression: str = 'lzma',
    stable_level: int = 9,
    unstable_compression: str = 'deflate',
    unstable_level: int = 6,
    default_compression: str = 'deflate',
    default_level: int = 6,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with automatic compression selection based on file stability (modification frequency).
    
    This command analyzes file stability by comparing modification time to creation time to
    determine how frequently files change. Files that haven't changed in a long time relative
    to their age are considered "stable" and benefit from better compression. Files that change
    frequently are considered "unstable" and benefit from faster compression.
    
    Stability ratio = (time_since_modification / file_age)
    - High ratio (>= stable_threshold_ratio): Stable files -> better compression
    - Low ratio (<= unstable_threshold_ratio): Unstable files -> faster compression
    - Medium ratio: Moderately stable files -> balanced compression
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        stable_threshold_ratio: Stability ratio threshold for stable files (0.0-1.0, default: 0.8).
        unstable_threshold_ratio: Stability ratio threshold for unstable files (0.0-1.0, default: 0.2).
        stable_compression: Compression method for stable files (default: lzma).
        stable_level: Compression level for stable files (0-9, default: 9).
        unstable_compression: Compression method for unstable files (default: deflate).
        unstable_level: Compression level for unstable files (0-9, default: 6).
        default_compression: Default compression method for moderately stable files (default: deflate).
        default_level: Default compression level for moderately stable files (0-9, default: 6).
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_stability_based_compression
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Validate threshold ratios
    if not (0.0 <= unstable_threshold_ratio <= stable_threshold_ratio <= 1.0):
        _print_error(f"Invalid threshold ratios: unstable_threshold_ratio ({unstable_threshold_ratio}) must be <= stable_threshold_ratio ({stable_threshold_ratio}) and both must be between 0.0 and 1.0", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with stability-based compression: {archive}")
            print(f"Files/directories: {len(files)}")
            print(f"Stable threshold ratio: {stable_threshold_ratio}")
            print(f"Unstable threshold ratio: {unstable_threshold_ratio}")
            print(f"Stable compression: {stable_compression} level {stable_level}")
            print(f"Unstable compression: {unstable_compression} level {unstable_level}")
            print(f"Default compression: {default_compression} level {default_level}")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, comp_method: str, comp_level: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [{comp_method} level {comp_level}]")
        
        # Create archive with stability-based compression
        result = create_archive_with_stability_based_compression(
            archive_path=archive,
            file_paths=files,
            stable_threshold_ratio=stable_threshold_ratio,
            unstable_threshold_ratio=unstable_threshold_ratio,
            stable_compression=stable_compression,
            stable_level=stable_level,
            unstable_compression=unstable_compression,
            unstable_level=unstable_level,
            default_compression=default_compression,
            default_level=default_level,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with Stability-Based Compression")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        print("📊 Stability Analysis:")
        print(f"  Stable files: {result['statistics']['stable_files']:,} ({_format_size(result['statistics']['stable_files_size'])})")
        print(f"  Moderately stable files: {result['statistics']['moderate_files']:,} ({_format_size(result['statistics']['moderate_files_size'])})")
        print(f"  Unstable files: {result['statistics']['unstable_files']:,} ({_format_size(result['statistics']['unstable_files_size'])})")
        print(f"  Average stability ratio: {result['statistics']['average_stability_ratio']:.2f}")
        print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_priority_based(
    archive: Path,
    files: List[Path],
    priority_rules: Optional[str] = None,
    default_compression: str = 'deflate',
    default_level: int = 6,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with automatic compression selection based on file priority or importance levels.
    
    This command assigns priority levels to files based on path patterns, file names, extensions,
    or other criteria, and applies different compression strategies based on those priorities.
    
    Priority levels: 'critical', 'high', 'medium', 'low', 'archive'
    - Critical files: May use faster compression for quick access or maximum compression for preservation
    - High-priority files: Balanced compression for good performance
    - Medium-priority files: Default compression settings
    - Low-priority files: May use better compression for space savings
    - Archive-priority files: Maximum compression for long-term storage
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        priority_rules: JSON string mapping priority levels to rules with matching criteria and compression settings.
        default_compression: Default compression method for files not matching any rule.
        default_level: Default compression level for files not matching any rule (0-9).
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    import json
    from .utils import create_archive_with_priority_based_compression
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Parse priority rules from JSON string
    priority_rules_dict = None
    if priority_rules:
        try:
            priority_rules_dict = json.loads(priority_rules)
            if not isinstance(priority_rules_dict, dict):
                _print_error("Priority rules must be a JSON object mapping priority levels to rules", exit_code=2)
        except json.JSONDecodeError as e:
            _print_error(f"Invalid JSON in priority rules: {e}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with priority-based compression: {archive}")
            print(f"Files/directories: {len(files)}")
            if priority_rules_dict:
                print(f"Priority rules: {len(priority_rules_dict)} priority levels")
            print(f"Default compression: {default_compression} level {default_level}")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, comp_method: str, comp_level: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [{comp_method} level {comp_level}]")
        
        # Create archive with priority-based compression
        result = create_archive_with_priority_based_compression(
            archive_path=archive,
            file_paths=files,
            priority_rules=priority_rules_dict,
            default_compression=default_compression,
            default_level=default_level,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with Priority-Based Compression")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        print("🎯 Priority Distribution:")
        if result['statistics']['priority_distribution']:
            for priority, count in sorted(result['statistics']['priority_distribution'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {priority}: {count:,} files")
        print(f"  Default compression: {result['statistics']['default_matches']:,} files")
        if result['statistics']['rule_matches']:
            print(f"  Rule matches:")
            for priority, count in sorted(result['statistics']['rule_matches'].items(), key=lambda x: x[1], reverse=True):
                print(f"    {priority}: {count:,} files")
        print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_count_based(
    archive: Path,
    files: List[Path],
    few_files_threshold: int = 10,
    many_files_threshold: int = 100,
    few_files_compression: str = 'deflate',
    few_files_level: int = 9,
    many_files_compression: str = 'deflate',
    many_files_level: int = 6,
    default_compression: str = 'deflate',
    default_level: int = 6,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with automatic compression selection based on file count.
    
    This command analyzes the total number of files to be archived and automatically
    selects compression methods and levels based on file count thresholds. Archives with
    few files may benefit from maximum compression, while archives with many files may
    benefit from faster compression to reduce overall processing time.
    
    Count-based compression selection:
    - Few files (< few_files_threshold): Better compression (default: deflate level 9)
      - Archives with few files can afford slower compression for better ratios
      - Maximum compression is practical when file count is low
    - Many files (> many_files_threshold): Faster compression (default: deflate level 6)
      - Archives with many files benefit from faster compression
      - Reduces overall processing time
    - Medium file count (between thresholds): Balanced compression (default: deflate level 6)
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        few_files_threshold: File count threshold for "few files" category (default: 10).
        many_files_threshold: File count threshold for "many files" category (default: 100).
        few_files_compression: Compression method for archives with few files (default: deflate).
        few_files_level: Compression level for archives with few files (0-9, default: 9).
        many_files_compression: Compression method for archives with many files (default: deflate).
        many_files_level: Compression level for archives with many files (0-9, default: 6).
        default_compression: Default compression method for medium file count archives (default: deflate).
        default_level: Default compression level for medium file count archives (0-9, default: 6).
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_count_based_compression
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with count-based compression: {archive}")
            print(f"Files/directories: {len(files)}")
            print(f"Few files threshold: {few_files_threshold}")
            print(f"Many files threshold: {many_files_threshold}")
            print(f"Few files compression: {few_files_compression} level {few_files_level}")
            print(f"Many files compression: {many_files_compression} level {many_files_level}")
            print(f"Default compression: {default_compression} level {default_level}")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, comp_method: str, comp_level: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [{comp_method} level {comp_level}]")
        
        # Create archive with count-based compression
        result = create_archive_with_count_based_compression(
            archive_path=archive,
            file_paths=files,
            few_files_threshold=few_files_threshold,
            many_files_threshold=many_files_threshold,
            few_files_compression=few_files_compression,
            few_files_level=few_files_level,
            many_files_compression=many_files_compression,
            many_files_level=many_files_level,
            default_compression=default_compression,
            default_level=default_level,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with Count-Based Compression")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        print("📊 Count Category:")
        count_category = result['statistics']['count_category']
        file_count = result['statistics']['file_count']
        print(f"  Category: {count_category}")
        print(f"  File count: {file_count:,} files")
        print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        if result['statistics']['level_usage']:
            print("📈 Compression Level Usage:")
            for level, count in sorted(result['statistics']['level_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  Level {level}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_total_size_based(
    archive: Path,
    files: List[Path],
    small_archive_threshold: int = 100 * 1024 * 1024,
    large_archive_threshold: int = 1024 * 1024 * 1024,
    small_archive_compression: str = 'deflate',
    small_archive_level: int = 9,
    large_archive_compression: str = 'deflate',
    large_archive_level: int = 6,
    default_compression: str = 'deflate',
    default_level: int = 6,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with automatic compression selection based on total archive size.
    
    This command analyzes the total uncompressed size of all files to be archived and
    automatically selects compression methods and levels based on total size thresholds.
    Small archives may benefit from maximum compression, while large archives may benefit
    from faster compression to reduce overall processing time.
    
    Total size-based compression selection:
    - Small archives (< small_archive_threshold): Better compression (default: deflate level 9)
      - Archives with small total size can afford slower compression for better ratios
      - Maximum compression is practical when total size is low
    - Large archives (> large_archive_threshold): Faster compression (default: deflate level 6)
      - Archives with large total size benefit from faster compression
      - Reduces overall processing time
    - Medium archives (between thresholds): Balanced compression (default: deflate level 6)
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        small_archive_threshold: Total size threshold in bytes for "small archive" category (default: 100 MB).
        large_archive_threshold: Total size threshold in bytes for "large archive" category (default: 1 GB).
        small_archive_compression: Compression method for small archives (default: deflate).
        small_archive_level: Compression level for small archives (0-9, default: 9).
        large_archive_compression: Compression method for large archives (default: deflate).
        large_archive_level: Compression level for large archives (0-9, default: 6).
        default_compression: Default compression method for medium archives (default: deflate).
        default_level: Default compression level for medium archives (0-9, default: 6).
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_total_size_based_compression
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with total size-based compression: {archive}")
            print(f"Files/directories: {len(files)}")
            print(f"Small archive threshold: {_format_size(small_archive_threshold)}")
            print(f"Large archive threshold: {_format_size(large_archive_threshold)}")
            print(f"Small archive compression: {small_archive_compression} level {small_archive_level}")
            print(f"Large archive compression: {large_archive_compression} level {large_archive_level}")
            print(f"Default compression: {default_compression} level {default_level}")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, comp_method: str, comp_level: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [{comp_method} level {comp_level}]")
        
        # Create archive with total size-based compression
        result = create_archive_with_total_size_based_compression(
            archive_path=archive,
            file_paths=files,
            small_archive_threshold=small_archive_threshold,
            large_archive_threshold=large_archive_threshold,
            small_archive_compression=small_archive_compression,
            small_archive_level=small_archive_level,
            large_archive_compression=large_archive_compression,
            large_archive_level=large_archive_level,
            default_compression=default_compression,
            default_level=default_level,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with Total Size-Based Compression")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        print("📊 Total Size Category:")
        size_category = result['statistics']['size_category']
        total_uncompressed_size = result['statistics']['total_uncompressed_size']
        print(f"  Category: {size_category}")
        print(f"  Total uncompressed size: {_format_size(total_uncompressed_size)} ({total_uncompressed_size:,} bytes)")
        print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        if result['statistics']['level_usage']:
            print("📈 Compression Level Usage:")
            for level, count in sorted(result['statistics']['level_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  Level {level}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_efficiency_based(
    archive: Path,
    files: List[Path],
    sample_size: int = 10,
    sample_percent: Optional[float] = None,
    test_methods: Optional[str] = None,
    test_levels: Optional[str] = None,
    min_sample_files: int = 3,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with automatic compression selection based on efficiency prediction.
    
    This command tests compression efficiency on a sample of files to predict the best
    compression method and level for the entire archive. It compresses a sample of files
    with different methods and levels, then selects the combination that provides the best
    average compression ratio.
    
    Efficiency prediction:
    - Selects a representative sample of files (by count or percentage)
    - Tests compression with different methods and levels on the sample
    - Calculates average compression ratio for each method/level combination
    - Selects the combination with the best average compression ratio
    - Applies the selected compression to all files in the archive
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        sample_size: Number of files to sample for testing (default: 10).
        sample_percent: Percentage of files to sample (0.0-1.0, overrides sample_size if specified).
        test_methods: Comma-separated list of compression methods to test (default: deflate,bzip2,lzma).
        test_levels: Comma-separated list of compression levels to test (default: 1,6,9).
        min_sample_files: Minimum number of files to test (default: 3).
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    import json
    from .utils import create_archive_with_efficiency_based_compression
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Parse test methods
    test_methods_list = None
    if test_methods:
        test_methods_list = [m.strip() for m in test_methods.split(',')]
        for method in test_methods_list:
            if method not in ['stored', 'deflate', 'bzip2', 'lzma']:
                _print_error(f"Invalid compression method: {method}", exit_code=2)
    
    # Parse test levels
    test_levels_list = None
    if test_levels:
        try:
            test_levels_list = [int(l.strip()) for l in test_levels.split(',')]
            for level in test_levels_list:
                if not (0 <= level <= 9):
                    _print_error(f"Invalid compression level: {level} (must be 0-9)", exit_code=2)
        except ValueError as e:
            _print_error(f"Invalid test levels format: {e}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with efficiency-based compression: {archive}")
            print(f"Files/directories: {len(files)}")
            if sample_percent:
                print(f"Sample: {sample_percent*100:.1f}% of files")
            else:
                print(f"Sample: {sample_size} files")
            print(f"Min sample files: {min_sample_files}")
            if test_methods_list:
                print(f"Test methods: {', '.join(test_methods_list)}")
            if test_levels_list:
                print(f"Test levels: {', '.join(map(str, test_levels_list))}")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, comp_method: str, comp_level: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [{comp_method} level {comp_level}]")
        
        # Create archive with efficiency-based compression
        result = create_archive_with_efficiency_based_compression(
            archive_path=archive,
            file_paths=files,
            sample_size=sample_size,
            sample_percent=sample_percent or 0.0,
            test_methods=test_methods_list,
            test_levels=test_levels_list,
            min_sample_files=min_sample_files,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with Efficiency-Based Compression")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        print("🎯 Efficiency Prediction:")
        selected_method = result['statistics']['selected_method']
        selected_level = result['statistics']['selected_level']
        sample_size_used = result['statistics']['sample_size']
        print(f"  Selected method: {selected_method}")
        print(f"  Selected level: {selected_level}")
        print(f"  Sample size: {sample_size_used} files")
        print()
        
        if result['statistics']['test_results']:
            print("📊 Test Results:")
            sorted_tests = sorted(result['statistics']['test_results'].items(), key=lambda x: x[1]['avg_ratio'])
            for test_key, test_result in sorted_tests[:10]:  # Show top 10
                print(f"  {test_key}: {test_result['avg_ratio']:.2%} (original: {_format_size(test_result['total_original'])}, compressed: {_format_size(test_result['total_compressed'])})")
            if len(sorted_tests) > 10:
                print(f"  ... and {len(sorted_tests) - 10} more test combinations")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_type_distribution_based(
    archive: Path,
    files: List[Path],
    dominant_threshold: float = 0.5,
    type_compression_map: Optional[str] = None,
    default_compression: str = 'deflate',
    default_level: int = 6,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with automatic compression selection based on file type distribution.
    
    This command analyzes the distribution of file types in the archive and automatically
    selects compression methods and levels based on which file types are most common.
    It optimizes compression for the dominant file types while using appropriate compression
    for other types.
    
    Type distribution analysis:
    - Detects file types for all files using content-based detection
    - Calculates distribution of file types (count and size)
    - Identifies dominant file types (above threshold percentage)
    - Applies compression optimized for dominant types
    - Uses type-specific compression for other file types
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        dominant_threshold: Threshold percentage (0.0-1.0) for considering a type "dominant" (default: 0.5).
        type_compression_map: JSON string mapping file types to compression settings.
        default_compression: Default compression method for files not matching any type rule.
        default_level: Default compression level for files not matching any type rule (0-9).
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    import json
    from .utils import create_archive_with_type_distribution_based_compression
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Parse type compression map from JSON string
    type_compression_map_dict = None
    if type_compression_map:
        try:
            type_compression_map_dict = json.loads(type_compression_map)
            if not isinstance(type_compression_map_dict, dict):
                _print_error("Type compression map must be a JSON object mapping file types to compression settings", exit_code=2)
        except json.JSONDecodeError as e:
            _print_error(f"Invalid JSON in type compression map: {e}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with type distribution-based compression: {archive}")
            print(f"Files/directories: {len(files)}")
            print(f"Dominant threshold: {dominant_threshold*100:.1f}%")
            if type_compression_map_dict:
                print(f"Type compression map: {len(type_compression_map_dict)} types")
            print(f"Default compression: {default_compression} level {default_level}")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, comp_method: str, comp_level: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [{comp_method} level {comp_level}]")
        
        # Create archive with type distribution-based compression
        result = create_archive_with_type_distribution_based_compression(
            archive_path=archive,
            file_paths=files,
            dominant_threshold=dominant_threshold,
            type_compression_map=type_compression_map_dict,
            default_compression=default_compression,
            default_level=default_level,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with Type Distribution-Based Compression")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        print("📊 Type Distribution:")
        type_dist = result['statistics']['type_distribution']
        for file_type, count in sorted(type_dist.items(), key=lambda x: x[1], reverse=True):
            percentage = result['statistics']['type_percentages'].get(file_type, 0.0) * 100
            size = result['statistics']['type_size_distribution'].get(file_type, 0)
            print(f"  {file_type}: {count:,} files ({percentage:.1f}%), {_format_size(size)}")
        print()
        
        print("🎯 Compression Strategy:")
        strategy = result['statistics']['compression_strategy']
        dominant_types = result['statistics']['dominant_types']
        print(f"  Strategy: {strategy}")
        if dominant_types:
            print(f"  Dominant types: {', '.join(dominant_types)}")
        print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_adaptive(
    archive: Path,
    files: List[Path],
    initial_compression: str = 'deflate',
    initial_level: int = 6,
    adaptation_threshold: float = 0.95,
    test_methods: Optional[str] = None,
    test_levels: Optional[str] = None,
    adaptation_window: int = 10,
    min_improvement: float = 0.05,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with adaptive compression that adjusts based on actual compression ratios.
    
    This command creates archives with adaptive compression that adjusts compression method
    and level based on actual compression ratios achieved during archiving. It tests compression
    on files and adapts the compression strategy when compression ratios are poor, switching
    to better compression methods or levels to improve overall archive compression.
    
    Adaptive compression:
    - Starts with initial compression method and level
    - Tests compression on each file to measure actual compression ratio
    - If compression ratio is poor (> adaptation_threshold), tests alternative methods/levels
    - Switches to better compression if improvement exceeds min_improvement threshold
    - Adapts compression strategy based on actual results, not predictions
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        initial_compression: Initial compression method to try (default: deflate).
        initial_level: Initial compression level to try (0-9, default: 6).
        adaptation_threshold: Compression ratio threshold for triggering adaptation (0.0-1.0, default: 0.95).
        test_methods: Comma-separated list of compression methods to test when adaptation is triggered.
        test_levels: Comma-separated list of compression levels to test when adaptation is triggered.
        adaptation_window: Number of recent files to consider for adaptation decisions (default: 10).
        min_improvement: Minimum improvement ratio required to switch compression (0.0-1.0, default: 0.05).
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_adaptive_compression
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Parse test methods
    test_methods_list = None
    if test_methods:
        test_methods_list = [m.strip() for m in test_methods.split(',')]
        for method in test_methods_list:
            if method not in ['stored', 'deflate', 'bzip2', 'lzma']:
                _print_error(f"Invalid compression method: {method}", exit_code=2)
    
    # Parse test levels
    test_levels_list = None
    if test_levels:
        try:
            test_levels_list = [int(l.strip()) for l in test_levels.split(',')]
            for level in test_levels_list:
                if not (0 <= level <= 9):
                    _print_error(f"Invalid compression level: {level} (must be 0-9)", exit_code=2)
        except ValueError as e:
            _print_error(f"Invalid test levels format: {e}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with adaptive compression: {archive}")
            print(f"Files/directories: {len(files)}")
            print(f"Initial compression: {initial_compression} level {initial_level}")
            print(f"Adaptation threshold: {adaptation_threshold:.2%}")
            print(f"Min improvement: {min_improvement:.2%}")
            print(f"Adaptation window: {adaptation_window} files")
            if test_methods_list:
                print(f"Test methods: {', '.join(test_methods_list)}")
            if test_levels_list:
                print(f"Test levels: {', '.join(map(str, test_levels_list))}")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, comp_method: str, comp_level: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [{comp_method} level {comp_level}]")
        
        # Create archive with adaptive compression
        result = create_archive_with_adaptive_compression(
            archive_path=archive,
            file_paths=files,
            initial_compression=initial_compression,
            initial_level=initial_level,
            adaptation_threshold=adaptation_threshold,
            test_methods=test_methods_list,
            test_levels=test_levels_list,
            adaptation_window=adaptation_window,
            min_improvement=min_improvement,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with Adaptive Compression")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        print("🔄 Adaptation Statistics:")
        adaptations_triggered = result['statistics']['adaptations_triggered']
        adaptations_applied = result['statistics']['adaptations_applied']
        average_improvement = result['statistics']['average_improvement']
        print(f"  Adaptations triggered: {adaptations_triggered:,} files")
        print(f"  Adaptations applied: {adaptations_applied:,} files")
        if adaptations_applied > 0:
            print(f"  Average improvement: {average_improvement:.2%}")
        print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage (Final):")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        if result['statistics']['initial_method_usage']:
            print("📊 Initial Compression Method Usage:")
            for method, count in sorted(result['statistics']['initial_method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_target_based(
    archive: Path,
    files: List[Path],
    target_ratio: Optional[float] = None,
    target_space_saved: Optional[int] = None,
    target_space_saved_percent: Optional[float] = None,
    initial_compression: str = 'deflate',
    initial_level: int = 6,
    test_methods: Optional[str] = None,
    test_levels: Optional[str] = None,
    max_iterations: int = 3,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with compression adjusted to meet target compression ratio or space savings goals.
    
    This command creates archives with compression that adjusts to meet specific targets:
    - Target compression ratio: Achieves a specific compression ratio (e.g., 0.5 = 50% of original size)
    - Target space saved: Saves a specific amount of space in bytes
    - Target space saved percent: Saves a specific percentage of space
    
    The function iteratively adjusts compression methods and levels to meet the target,
    starting with initial compression and progressively trying better compression until
    the target is met or max_iterations is reached.
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        target_ratio: Optional target compression ratio (0.0-1.0). Lower values = better compression.
        target_space_saved: Optional target space saved in bytes.
        target_space_saved_percent: Optional target space saved percentage (0.0-100.0).
        initial_compression: Initial compression method to try (default: deflate).
        initial_level: Initial compression level to try (0-9, default: 6).
        test_methods: Comma-separated list of compression methods to test when target not met.
        test_levels: Comma-separated list of compression levels to test when target not met.
        max_iterations: Maximum number of iterations to try (default: 3).
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_target_based_compression
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Validate only one target specified
    target_count = sum([
        target_ratio is not None,
        target_space_saved is not None,
        target_space_saved_percent is not None,
    ])
    if target_count > 1:
        _print_error("Only one target can be specified (target_ratio, target_space_saved, or target_space_saved_percent)", exit_code=2)
    
    # Parse test methods
    test_methods_list = None
    if test_methods:
        test_methods_list = [m.strip() for m in test_methods.split(',')]
        for method in test_methods_list:
            if method not in ['stored', 'deflate', 'bzip2', 'lzma']:
                _print_error(f"Invalid compression method: {method}", exit_code=2)
    
    # Parse test levels
    test_levels_list = None
    if test_levels:
        try:
            test_levels_list = [int(l.strip()) for l in test_levels.split(',')]
            for level in test_levels_list:
                if not (0 <= level <= 9):
                    _print_error(f"Invalid compression level: {level} (must be 0-9)", exit_code=2)
        except ValueError as e:
            _print_error(f"Invalid test levels format: {e}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with target-based compression: {archive}")
            print(f"Files/directories: {len(files)}")
            if target_ratio is not None:
                print(f"Target compression ratio: {target_ratio:.2%}")
            elif target_space_saved is not None:
                print(f"Target space saved: {_format_size(target_space_saved)} ({target_space_saved:,} bytes)")
            elif target_space_saved_percent is not None:
                print(f"Target space saved: {target_space_saved_percent:.1f}%")
            else:
                print("No target specified (will use initial compression)")
            print(f"Initial compression: {initial_compression} level {initial_level}")
            print(f"Max iterations: {max_iterations}")
            if test_methods_list:
                print(f"Test methods: {', '.join(test_methods_list)}")
            if test_levels_list:
                print(f"Test levels: {', '.join(map(str, test_levels_list))}")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, comp_method: str, comp_level: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [{comp_method} level {comp_level}]")
        
        # Create archive with target-based compression
        result = create_archive_with_target_based_compression(
            archive_path=archive,
            file_paths=files,
            target_ratio=target_ratio,
            target_space_saved=target_space_saved,
            target_space_saved_percent=target_space_saved_percent,
            initial_compression=initial_compression,
            initial_level=initial_level,
            test_methods=test_methods_list,
            test_levels=test_levels_list,
            max_iterations=max_iterations,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with Target-Based Compression")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        if result['target_type']:
            print("🎯 Target Status:")
            target_type = result['target_type']
            target_value = result['target_value']
            target_achieved = result['target_achieved']
            iterations = result['iterations_performed']
            
            if target_type == 'ratio':
                print(f"  Target: Compression ratio <= {target_value:.2%}")
                print(f"  Achieved: {result['compression_ratio']:.2%}")
            elif target_type == 'space_saved':
                print(f"  Target: Space saved >= {_format_size(target_value)} ({target_value:,} bytes)")
                print(f"  Achieved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['total_space_saved']:,} bytes)")
            elif target_type == 'space_saved_percent':
                print(f"  Target: Space saved >= {target_value:.1f}%")
                print(f"  Achieved: {result['statistics']['space_saved_percent']:.1f}%")
            
            print(f"  Status: {'✅ Target achieved' if target_achieved else '❌ Target not achieved'}")
            if not target_achieved:
                gap = result['statistics']['target_gap']
                if target_type == 'ratio':
                    print(f"  Gap: {gap:.2%}")
                elif target_type == 'space_saved':
                    print(f"  Gap: {_format_size(gap)} ({gap:,} bytes)")
                elif target_type == 'space_saved_percent':
                    print(f"  Gap: {gap:.1f}%")
            print(f"  Iterations: {iterations}")
            print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_speed_based(
    archive: Path,
    files: List[Path],
    speed_mode: str = 'balanced',
    max_time_seconds: Optional[float] = None,
    fast_compression: str = 'deflate',
    fast_level: int = 3,
    balanced_compression: str = 'deflate',
    balanced_level: int = 6,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with compression optimized for speed rather than compression ratio.
    
    This command creates archives with compression optimized for speed, prioritizing
    fast compression over maximum space savings. It's useful when time is more important
    than compression ratio, such as for quick backups, temporary archives, or time-sensitive operations.
    
    Speed-based compression modes:
    - 'fast': Maximum speed, minimal compression (default: deflate level 3)
    - 'balanced': Balanced speed and compression (default: deflate level 6)
    - 'time_budget': Optimizes for time budget (requires max_time_seconds)
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        speed_mode: Speed optimization mode ('fast', 'balanced', 'time_budget'). Default: 'balanced'.
        max_time_seconds: Maximum time in seconds for compression (only used with speed_mode='time_budget').
        fast_compression: Compression method for fast mode (default: deflate).
        fast_level: Compression level for fast mode (0-9, default: 3).
        balanced_compression: Compression method for balanced mode (default: deflate).
        balanced_level: Compression level for balanced mode (0-9, default: 6).
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_speed_based_compression
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with speed-based compression: {archive}")
            print(f"Files/directories: {len(files)}")
            print(f"Speed mode: {speed_mode}")
            if max_time_seconds:
                print(f"Max time: {max_time_seconds:.1f} seconds")
            print(f"Fast compression: {fast_compression} level {fast_level}")
            print(f"Balanced compression: {balanced_compression} level {balanced_level}")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, comp_method: str, comp_level: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [{comp_method} level {comp_level}]")
        
        # Create archive with speed-based compression
        result = create_archive_with_speed_based_compression(
            archive_path=archive,
            file_paths=files,
            speed_mode=speed_mode,
            max_time_seconds=max_time_seconds,
            fast_compression=fast_compression,
            fast_level=fast_level,
            balanced_compression=balanced_compression,
            balanced_level=balanced_level,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with Speed-Based Compression")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        print("⚡ Speed Statistics:")
        speed_mode_used = result['statistics']['speed_mode']
        compression_time = result['compression_time']
        files_per_second = result['statistics']['files_per_second']
        bytes_per_second = result['statistics']['bytes_per_second']
        print(f"  Speed mode: {speed_mode_used}")
        print(f"  Compression time: {compression_time:.2f} seconds")
        print(f"  Files per second: {files_per_second:.1f}")
        print(f"  Bytes per second: {_format_size(int(bytes_per_second))}/s ({bytes_per_second:,.0f} bytes/s)")
        if result['statistics']['time_budget_met'] is not None:
            budget_status = "✅ Met" if result['statistics']['time_budget_met'] else "❌ Exceeded"
            print(f"  Time budget: {budget_status}")
        print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_quality_based(
    archive: Path,
    files: List[Path],
    quality_mode: str = 'balanced',
    min_compression_ratio: Optional[float] = None,
    quality_threshold: float = 0.7,
    test_methods: Optional[str] = None,
    test_levels: Optional[str] = None,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with compression optimized for quality and efficiency metrics.
    
    This command creates archives with compression optimized for quality, considering
    both compression ratio and compression efficiency. It evaluates compression quality
    using metrics like compression ratio, compression efficiency, and overall archive quality.
    
    Quality-based compression modes:
    - 'balanced': Balanced quality metrics (default)
    - 'high_quality': Maximum quality, efficiency secondary
    - 'efficient': Maximum efficiency, quality secondary
    - 'minimum_ratio': Ensures minimum compression ratio (requires min_compression_ratio)
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        quality_mode: Quality optimization mode ('balanced', 'high_quality', 'efficient', 'minimum_ratio').
        min_compression_ratio: Optional minimum compression ratio (0.0-1.0, only used with minimum_ratio mode).
        quality_threshold: Quality score threshold for triggering better compression (0.0-1.0, default: 0.7).
        test_methods: Comma-separated list of compression methods to test for quality optimization.
        test_levels: Comma-separated list of compression levels to test.
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_quality_based_compression
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Parse test methods
    test_methods_list = None
    if test_methods:
        test_methods_list = [m.strip() for m in test_methods.split(',')]
        for method in test_methods_list:
            if method not in ['stored', 'deflate', 'bzip2', 'lzma']:
                _print_error(f"Invalid compression method: {method}", exit_code=2)
    
    # Parse test levels
    test_levels_list = None
    if test_levels:
        try:
            test_levels_list = [int(l.strip()) for l in test_levels.split(',')]
            for level in test_levels_list:
                if not (0 <= level <= 9):
                    _print_error(f"Invalid compression level: {level} (must be 0-9)", exit_code=2)
        except ValueError as e:
            _print_error(f"Invalid test levels format: {e}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with quality-based compression: {archive}")
            print(f"Files/directories: {len(files)}")
            print(f"Quality mode: {quality_mode}")
            if min_compression_ratio is not None:
                print(f"Min compression ratio: {min_compression_ratio:.2%}")
            print(f"Quality threshold: {quality_threshold:.2f}")
            if test_methods_list:
                print(f"Test methods: {', '.join(test_methods_list)}")
            if test_levels_list:
                print(f"Test levels: {', '.join(map(str, test_levels_list))}")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, comp_method: str, comp_level: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [{comp_method} level {comp_level}]")
        
        # Create archive with quality-based compression
        result = create_archive_with_quality_based_compression(
            archive_path=archive,
            file_paths=files,
            quality_mode=quality_mode,
            min_compression_ratio=min_compression_ratio,
            quality_threshold=quality_threshold,
            test_methods=test_methods_list,
            test_levels=test_levels_list,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with Quality-Based Compression")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        print("⭐ Quality Statistics:")
        quality_mode_used = result['statistics']['quality_mode']
        avg_quality = result['statistics']['average_quality_score']
        quality_dist = result['statistics']['quality_distribution']
        print(f"  Quality mode: {quality_mode_used}")
        print(f"  Average quality score: {avg_quality:.2f}")
        print(f"  Quality distribution:")
        for category, count in sorted(quality_dist.items(), key=lambda x: ['high', 'medium', 'low'].index(x[0]) if x[0] in ['high', 'medium', 'low'] else 999):
            print(f"    {category}: {count:,} files")
        if result['statistics']['min_ratio_met'] is not None:
            ratio_status = "✅ Met" if result['statistics']['min_ratio_met'] else "❌ Not met"
            print(f"  Minimum ratio requirement: {ratio_status}")
        print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_age_based(
    archive: Path,
    files: List[Path],
    recent_age_days: int = 7,
    old_age_days: int = 90,
    recent_compression: str = 'deflate',
    recent_level: int = 6,
    old_compression: str = 'deflate',
    old_level: int = 9,
    default_compression: str = 'deflate',
    default_level: int = 6,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with compression optimized based on file age (time since last modification).
    
    This command creates archives with compression optimized based on file modification times.
    Files that were recently modified use faster compression, while older files use maximum
    compression for better space savings.
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        recent_age_days: Age threshold in days for "recent files" (default: 7).
        old_age_days: Age threshold in days for "old files" (default: 90).
        recent_compression: Compression method for recently modified files (default: 'deflate').
        recent_level: Compression level for recently modified files (0-9, default: 6).
        old_compression: Compression method for old files (default: 'deflate').
        old_level: Compression level for old files (0-9, default: 9).
        default_compression: Default compression method for medium age files (default: 'deflate').
        default_level: Default compression level for medium age files (0-9, default: 6).
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_age_based_compression
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with age-based compression: {archive}")
            print(f"Files/directories: {len(files)}")
            print(f"Recent age threshold: {recent_age_days} days")
            print(f"Old age threshold: {old_age_days} days")
            print(f"Recent files: {recent_compression} level {recent_level}")
            print(f"Old files: {old_compression} level {old_level}")
            print(f"Medium age files: {default_compression} level {default_level}")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, comp_method: str, comp_level: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [{comp_method} level {comp_level}]")
        
        # Create archive with age-based compression
        result = create_archive_with_age_based_compression(
            archive_path=archive,
            file_paths=files,
            recent_age_days=recent_age_days,
            old_age_days=old_age_days,
            recent_compression=recent_compression,
            recent_level=recent_level,
            old_compression=old_compression,
            old_level=old_level,
            default_compression=default_compression,
            default_level=default_level,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with Age-Based Compression")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        print("⏰ Age Statistics:")
        stats = result['statistics']
        print(f"  Recent files (< {recent_age_days} days): {stats['recent_files']:,} files ({_format_size(stats['recent_files_size'])} {stats['recent_files_size']:,} bytes)")
        print(f"  Medium age files: {stats['medium_files']:,} files ({_format_size(stats['medium_files_size'])} {stats['medium_files_size']:,} bytes)")
        print(f"  Old files (> {old_age_days} days): {stats['old_files']:,} files ({_format_size(stats['old_files_size'])} {stats['old_files_size']:,} bytes)")
        print(f"  Average file age: {stats['average_age_days']:.1f} days")
        print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_size_distribution_based(
    archive: Path,
    files: List[Path],
    small_file_threshold: int = 1024 * 1024,
    large_file_threshold: int = 10 * 1024 * 1024,
    mostly_small_compression: str = 'deflate',
    mostly_small_level: int = 6,
    mostly_large_compression: str = 'lzma',
    mostly_large_level: int = 9,
    mixed_compression: str = 'deflate',
    mixed_level: int = 6,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with compression optimized based on file size distribution.
    
    This command creates archives with compression optimized based on the distribution
    of file sizes. Archives with mostly small files use balanced compression, while
    archives with mostly large files use maximum compression.
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        small_file_threshold: Size threshold in bytes for small files (default: 1 MB).
        large_file_threshold: Size threshold in bytes for large files (default: 10 MB).
        mostly_small_compression: Compression method for archives with mostly small files (default: 'deflate').
        mostly_small_level: Compression level for archives with mostly small files (0-9, default: 6).
        mostly_large_compression: Compression method for archives with mostly large files (default: 'lzma').
        mostly_large_level: Compression level for archives with mostly large files (0-9, default: 9).
        mixed_compression: Compression method for mixed size archives (default: 'deflate').
        mixed_level: Compression level for mixed size archives (0-9, default: 6).
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_size_distribution_based_compression
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with size distribution-based compression: {archive}")
            print(f"Files/directories: {len(files)}")
            print(f"Small file threshold: {_format_size(small_file_threshold)} ({small_file_threshold:,} bytes)")
            print(f"Large file threshold: {_format_size(large_file_threshold)} ({large_file_threshold:,} bytes)")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, comp_method: str, comp_level: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [{comp_method} level {comp_level}]")
        
        # Create archive with size distribution-based compression
        result = create_archive_with_size_distribution_based_compression(
            archive_path=archive,
            file_paths=files,
            small_file_threshold=small_file_threshold,
            large_file_threshold=large_file_threshold,
            mostly_small_compression=mostly_small_compression,
            mostly_small_level=mostly_small_level,
            mostly_large_compression=mostly_large_compression,
            mostly_large_level=mostly_large_level,
            mixed_compression=mixed_compression,
            mixed_level=mixed_level,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with Size Distribution-Based Compression")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        print("📊 Size Distribution Statistics:")
        stats = result['statistics']
        dist_cat = stats['distribution_category']
        print(f"  Distribution category: {dist_cat}")
        print(f"  Small files (< {_format_size(small_file_threshold)}): {stats['small_files']:,} files ({stats['small_files_percent']:.1f}%) - {_format_size(stats['small_files_size'])}")
        print(f"  Medium files: {stats['medium_files']:,} files ({stats['medium_files_percent']:.1f}%) - {_format_size(stats['medium_files_size'])}")
        print(f"  Large files (> {_format_size(large_file_threshold)}): {stats['large_files']:,} files ({stats['large_files_percent']:.1f}%) - {_format_size(stats['large_files_size'])}")
        print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_activity_based(
    archive: Path,
    files: List[Path],
    high_activity_threshold_hours: int = 24,
    low_activity_threshold_days: int = 30,
    high_activity_compression: str = 'deflate',
    high_activity_level: int = 6,
    low_activity_compression: str = 'lzma',
    low_activity_level: int = 9,
    medium_activity_compression: str = 'deflate',
    medium_activity_level: int = 6,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with compression optimized based on file activity level.
    
    This command creates archives with compression optimized based on file activity levels.
    Files that were modified very recently (high activity) use faster compression, while
    files not modified for a long time (low activity) use maximum compression.
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        high_activity_threshold_hours: Time threshold in hours for high activity files (default: 24).
        low_activity_threshold_days: Time threshold in days for low activity files (default: 30).
        high_activity_compression: Compression method for high activity files (default: 'deflate').
        high_activity_level: Compression level for high activity files (0-9, default: 6).
        low_activity_compression: Compression method for low activity files (default: 'lzma').
        low_activity_level: Compression level for low activity files (0-9, default: 9).
        medium_activity_compression: Compression method for medium activity files (default: 'deflate').
        medium_activity_level: Compression level for medium activity files (0-9, default: 6).
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_activity_based_compression
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with activity-based compression: {archive}")
            print(f"Files/directories: {len(files)}")
            print(f"High activity threshold: {high_activity_threshold_hours} hours")
            print(f"Low activity threshold: {low_activity_threshold_days} days")
            print(f"High activity files: {high_activity_compression} level {high_activity_level}")
            print(f"Low activity files: {low_activity_compression} level {low_activity_level}")
            print(f"Medium activity files: {medium_activity_compression} level {medium_activity_level}")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, comp_method: str, comp_level: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [{comp_method} level {comp_level}]")
        
        # Create archive with activity-based compression
        result = create_archive_with_activity_based_compression(
            archive_path=archive,
            file_paths=files,
            high_activity_threshold_hours=high_activity_threshold_hours,
            low_activity_threshold_days=low_activity_threshold_days,
            high_activity_compression=high_activity_compression,
            high_activity_level=high_activity_level,
            low_activity_compression=low_activity_compression,
            low_activity_level=low_activity_level,
            medium_activity_compression=medium_activity_compression,
            medium_activity_level=medium_activity_level,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with Activity-Based Compression")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        print("⚡ Activity Statistics:")
        stats = result['statistics']
        print(f"  High activity files (< {high_activity_threshold_hours} hours): {stats['high_activity_files']:,} files ({_format_size(stats['high_activity_files_size'])} {stats['high_activity_files_size']:,} bytes)")
        print(f"  Medium activity files: {stats['medium_activity_files']:,} files ({_format_size(stats['medium_activity_files_size'])} {stats['medium_activity_files_size']:,} bytes)")
        print(f"  Low activity files (> {low_activity_threshold_days} days): {stats['low_activity_files']:,} files ({_format_size(stats['low_activity_files_size'])} {stats['low_activity_files_size']:,} bytes)")
        print(f"  Average time since modification: {stats['average_time_since_modification_hours']:.1f} hours ({stats['average_time_since_modification_hours'] / 24:.1f} days)")
        print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_activity_based(
    archive: Path,
    files: List[Path],
    high_activity_threshold_hours: int = 24,
    low_activity_threshold_days: int = 30,
    high_activity_compression: str = 'deflate',
    high_activity_level: int = 6,
    low_activity_compression: str = 'lzma',
    low_activity_level: int = 9,
    medium_activity_compression: str = 'deflate',
    medium_activity_level: int = 6,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with compression optimized based on file activity level.
    
    This command creates archives with compression optimized based on file modification
    times to determine activity levels. Files that were recently modified (high activity)
    use faster compression, while files not modified for a long time (low activity) use
    maximum compression for better space savings.
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        high_activity_threshold_hours: Time threshold in hours for "high activity" files (default: 24).
        low_activity_threshold_days: Time threshold in days for "low activity" files (default: 30).
        high_activity_compression: Compression method for high activity files (default: 'deflate').
        high_activity_level: Compression level for high activity files (0-9, default: 6).
        low_activity_compression: Compression method for low activity files (default: 'lzma').
        low_activity_level: Compression level for low activity files (0-9, default: 9).
        medium_activity_compression: Compression method for medium activity files (default: 'deflate').
        medium_activity_level: Compression level for medium activity files (0-9, default: 6).
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_activity_based_compression
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with activity-based compression: {archive}")
            print(f"Files/directories: {len(files)}")
            print(f"High activity threshold: {high_activity_threshold_hours} hours")
            print(f"Low activity threshold: {low_activity_threshold_days} days")
            print(f"High activity files: {high_activity_compression} level {high_activity_level}")
            print(f"Low activity files: {low_activity_compression} level {low_activity_level}")
            print(f"Medium activity files: {medium_activity_compression} level {medium_activity_level}")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, comp_method: str, comp_level: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [{comp_method} level {comp_level}]")
        
        # Create archive with activity-based compression
        result = create_archive_with_activity_based_compression(
            archive_path=archive,
            file_paths=files,
            high_activity_threshold_hours=high_activity_threshold_hours,
            low_activity_threshold_days=low_activity_threshold_days,
            high_activity_compression=high_activity_compression,
            high_activity_level=high_activity_level,
            low_activity_compression=low_activity_compression,
            low_activity_level=low_activity_level,
            medium_activity_compression=medium_activity_compression,
            medium_activity_level=medium_activity_level,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with Activity-Based Compression")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        print("⚡ Activity Statistics:")
        stats = result['statistics']
        print(f"  High activity files (< {high_activity_threshold_hours} hours): {stats['high_activity_files']:,} files ({_format_size(stats['high_activity_files_size'])} {stats['high_activity_files_size']:,} bytes)")
        print(f"  Medium activity files: {stats['medium_activity_files']:,} files ({_format_size(stats['medium_activity_files_size'])} {stats['medium_activity_files_size']:,} bytes)")
        print(f"  Low activity files (> {low_activity_threshold_days} days): {stats['low_activity_files']:,} files ({_format_size(stats['low_activity_files_size'])} {stats['low_activity_files_size']:,} bytes)")
        print(f"  Average time since modification: {stats['average_time_since_modification_hours']:.1f} hours")
        print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_performance_requirements_based(
    archive: Path,
    files: List[Path],
    max_time_seconds: Optional[float] = None,
    min_compression_ratio: Optional[float] = None,
    max_compression_time_per_file: Optional[float] = None,
    test_methods: Optional[str] = None,
    test_levels: Optional[str] = None,
    default_compression: str = 'deflate',
    default_level: int = 6,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with compression selected to meet performance requirements.
    
    This command creates archives with compression automatically selected to meet
    specified performance requirements such as time budgets, minimum compression ratios,
    or maximum compression time per file.
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        max_time_seconds: Maximum total time allowed for compression in seconds.
        min_compression_ratio: Minimum compression ratio required (0.0-1.0).
        max_compression_time_per_file: Maximum compression time per file in seconds.
        test_methods: Comma-separated list of compression methods to test.
        test_levels: Comma-separated list of compression levels to test.
        default_compression: Default compression method if no requirements specified (default: 'deflate').
        default_level: Default compression level if no requirements specified (0-9, default: 6).
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_performance_requirements_based_compression
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Parse test methods
    test_methods_list = None
    if test_methods:
        test_methods_list = [m.strip() for m in test_methods.split(',')]
        for method in test_methods_list:
            if method not in ['stored', 'deflate', 'bzip2', 'lzma']:
                _print_error(f"Invalid compression method: {method}", exit_code=2)
    
    # Parse test levels
    test_levels_list = None
    if test_levels:
        try:
            test_levels_list = [int(l.strip()) for l in test_levels.split(',')]
            for level in test_levels_list:
                if not (0 <= level <= 9):
                    _print_error(f"Invalid compression level: {level} (must be 0-9)", exit_code=2)
        except ValueError as e:
            _print_error(f"Invalid test levels format: {e}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with performance requirements-based compression: {archive}")
            print(f"Files/directories: {len(files)}")
            if max_time_seconds is not None:
                print(f"Max time budget: {max_time_seconds:.1f} seconds")
            if min_compression_ratio is not None:
                print(f"Min compression ratio: {min_compression_ratio:.2%}")
            if max_compression_time_per_file is not None:
                print(f"Max compression time per file: {max_compression_time_per_file:.1f} seconds")
            if test_methods_list:
                print(f"Test methods: {', '.join(test_methods_list)}")
            if test_levels_list:
                print(f"Test levels: {', '.join(map(str, test_levels_list))}")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, comp_method: str, comp_level: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [{comp_method} level {comp_level}]")
        
        # Create archive with performance requirements-based compression
        result = create_archive_with_performance_requirements_based_compression(
            archive_path=archive,
            file_paths=files,
            max_time_seconds=max_time_seconds,
            min_compression_ratio=min_compression_ratio,
            max_compression_time_per_file=max_compression_time_per_file,
            test_methods=test_methods_list,
            test_levels=test_levels_list,
            default_compression=default_compression,
            default_level=default_level,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with Performance Requirements-Based Compression")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print(f"  Compression time: {result['compression_time']:.2f} seconds")
        print()
        
        print("⚙️ Performance Requirements:")
        req_met = result['requirements_met']
        if 'time_budget_met' in req_met:
            status = "✅ Met" if req_met['time_budget_met'] else "❌ Exceeded"
            print(f"  Time budget: {status}")
        if 'min_ratio_met' in req_met:
            status = "✅ Met" if req_met['min_ratio_met'] else "❌ Not met"
            print(f"  Minimum compression ratio: {status}")
        if 'max_time_per_file_met' in req_met:
            status = "✅ Met" if req_met['max_time_per_file_met'] else "❌ Exceeded"
            print(f"  Max compression time per file: {status}")
        if not req_met:
            print("  No requirements specified")
        print()
        
        print("📊 Performance Statistics:")
        stats = result['statistics']
        print(f"  Average compression time per file: {stats['average_compression_time_per_file']:.3f} seconds")
        print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_performance_requirements_based(
    archive: Path,
    files: List[Path],
    max_time_seconds: Optional[float] = None,
    min_compression_ratio: Optional[float] = None,
    max_compression_time_per_file: Optional[float] = None,
    test_methods: Optional[str] = None,
    test_levels: Optional[str] = None,
    default_compression: str = 'deflate',
    default_level: int = 6,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with compression selected to meet performance requirements and constraints.
    
    This command creates archives with compression automatically selected to meet specified
    performance requirements such as time budgets, minimum compression ratios, or maximum
    compression time per file.
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        max_time_seconds: Maximum total time allowed for compression in seconds.
        min_compression_ratio: Minimum compression ratio required (0.0-1.0).
        max_compression_time_per_file: Maximum compression time per file in seconds.
        test_methods: Comma-separated list of compression methods to test (default: deflate,bzip2,lzma).
        test_levels: Comma-separated list of compression levels to test (default: 1,3,6,9).
        default_compression: Default compression method if no requirements specified (default: deflate).
        default_level: Default compression level if no requirements specified (0-9, default: 6).
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_performance_requirements_based_compression
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Parse test methods and levels
    test_methods_list = None
    if test_methods:
        test_methods_list = [m.strip() for m in test_methods.split(',')]
    
    test_levels_list = None
    if test_levels:
        test_levels_list = [int(l.strip()) for l in test_levels.split(',')]
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with performance requirements-based compression: {archive}")
            print(f"Files/directories: {len(files)}")
            if max_time_seconds:
                print(f"Time budget: {max_time_seconds:.1f} seconds")
            if min_compression_ratio:
                print(f"Minimum compression ratio: {min_compression_ratio:.2%}")
            if max_compression_time_per_file:
                print(f"Maximum time per file: {max_compression_time_per_file:.1f} seconds")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, comp_method: str, comp_level: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [{comp_method} level {comp_level}]")
        
        # Create archive with performance requirements-based compression
        result = create_archive_with_performance_requirements_based_compression(
            archive_path=archive,
            file_paths=files,
            max_time_seconds=max_time_seconds,
            min_compression_ratio=min_compression_ratio,
            max_compression_time_per_file=max_compression_time_per_file,
            test_methods=test_methods_list,
            test_levels=test_levels_list,
            default_compression=default_compression,
            default_level=default_level,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with Performance Requirements-Based Compression")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print(f"  Compression time: {result['compression_time']:.2f} seconds")
        print()
        
        print("⚡ Performance Requirements:")
        req_met = result['requirements_met']
        if max_time_seconds:
            status = "✅" if req_met.get('time_budget_met', False) else "❌"
            print(f"  {status} Time budget ({max_time_seconds:.1f}s): {'Met' if req_met.get('time_budget_met', False) else 'Not met'}")
        if min_compression_ratio:
            status = "✅" if req_met.get('min_ratio_met', False) else "❌"
            print(f"  {status} Minimum ratio ({min_compression_ratio:.2%}): {'Met' if req_met.get('min_ratio_met', False) else 'Not met'}")
        if max_compression_time_per_file:
            status = "✅" if req_met.get('max_time_per_file_met', False) else "❌"
            print(f"  {status} Max time per file ({max_compression_time_per_file:.1f}s): {'Met' if req_met.get('max_time_per_file_met', False) else 'Not met'}")
        if not max_time_seconds and not min_compression_ratio and not max_compression_time_per_file:
            print("  No specific requirements specified (used default compression)")
        print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        print(f"   Average time per file: {result['statistics']['average_compression_time_per_file']:.2f} seconds")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_pattern_based(
    archive: Path,
    files: List[Path],
    pattern_analysis_size: int = 16384,
    compression: str = 'deflate',
    compression_level: int = 6,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with automatic compression method selection based on file data patterns.
    
    This command creates archives with compression automatically selected based on detected
    file data patterns such as repetitive data, structured text (JSON, XML, CSV), binary
    patterns, and already-compressed files.
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        pattern_analysis_size: Maximum number of bytes to analyze from each file for pattern detection.
        compression: Default compression method if pattern doesn't strongly suggest a method.
        compression_level: Compression level to use (0-9).
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_pattern_based_compression
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with pattern-based compression: {archive}")
            print(f"Files/directories: {len(files)}")
            print(f"Pattern analysis size: {pattern_analysis_size} bytes")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, comp_method: str) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [{comp_method}]")
        
        # Create archive with pattern-based compression
        result = create_archive_with_pattern_based_compression(
            archive_path=archive,
            file_paths=files,
            pattern_analysis_size=pattern_analysis_size,
            compression=compression,
            compression_level=compression_level,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with Pattern-Based Compression")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        if result['statistics']['pattern_detection']:
            print("🔍 Pattern Detection:")
            for pattern_type, count in sorted(result['statistics']['pattern_detection'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {pattern_type}: {count:,} files")
            print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_compressibility_based(
    archive: Path,
    files: List[Path],
    highly_compressible_threshold: float = 0.5,
    poorly_compressible_threshold: float = 0.9,
    highly_compressible_compression: str = 'lzma',
    highly_compressible_level: int = 9,
    poorly_compressible_compression: str = 'stored',
    poorly_compressible_level: int = 0,
    default_compression: str = 'deflate',
    default_level: int = 6,
    test_sample_size: int = 5,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with compression selected based on file compressibility.
    
    This command creates archives with compression automatically selected based on how
    well files compress. Highly compressible files use maximum compression, while
    poorly compressible files are stored uncompressed to save time.
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        highly_compressible_threshold: Compression ratio threshold for highly compressible files (0.0-1.0, default: 0.5).
        poorly_compressible_threshold: Compression ratio threshold for poorly compressible files (0.0-1.0, default: 0.9).
        highly_compressible_compression: Compression method for highly compressible files (default: 'lzma').
        highly_compressible_level: Compression level for highly compressible files (0-9, default: 9).
        poorly_compressible_compression: Compression method for poorly compressible files (default: 'stored').
        poorly_compressible_level: Compression level for poorly compressible files (0-9, default: 0).
        default_compression: Default compression method for moderately compressible files (default: 'deflate').
        default_level: Default compression level for moderately compressible files (0-9, default: 6).
        test_sample_size: Number of files to test for compressibility analysis (default: 5).
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_compressibility_based_compression
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with compressibility-based compression: {archive}")
            print(f"Files/directories: {len(files)}")
            print(f"Highly compressible threshold: < {highly_compressible_threshold:.2%}")
            print(f"Poorly compressible threshold: > {poorly_compressible_threshold:.2%}")
            print(f"Highly compressible files: {highly_compressible_compression} level {highly_compressible_level}")
            print(f"Poorly compressible files: {poorly_compressible_compression} level {poorly_compressible_level}")
            print(f"Moderately compressible files: {default_compression} level {default_level}")
            print(f"Test sample size: {test_sample_size} files")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, comp_method: str, comp_level: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [{comp_method} level {comp_level}]")
        
        # Create archive with compressibility-based compression
        result = create_archive_with_compressibility_based_compression(
            archive_path=archive,
            file_paths=files,
            highly_compressible_threshold=highly_compressible_threshold,
            poorly_compressible_threshold=poorly_compressible_threshold,
            highly_compressible_compression=highly_compressible_compression,
            highly_compressible_level=highly_compressible_level,
            poorly_compressible_compression=poorly_compressible_compression,
            poorly_compressible_level=poorly_compressible_level,
            default_compression=default_compression,
            default_level=default_level,
            test_sample_size=test_sample_size,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with Compressibility-Based Compression")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        print("📊 Compressibility Statistics:")
        stats = result['statistics']
        print(f"  Highly compressible files (< {highly_compressible_threshold:.2%}): {stats['highly_compressible_files']:,} files ({_format_size(stats['highly_compressible_files_size'])} {stats['highly_compressible_files_size']:,} bytes)")
        print(f"  Moderately compressible files: {stats['moderate_compressible_files']:,} files ({_format_size(stats['moderate_compressible_files_size'])} {stats['moderate_compressible_files_size']:,} bytes)")
        print(f"  Poorly compressible files (> {poorly_compressible_threshold:.2%}): {stats['poorly_compressible_files']:,} files ({_format_size(stats['poorly_compressible_files_size'])} {stats['poorly_compressible_files_size']:,} bytes)")
        print(f"  Average compressibility ratio: {stats['average_compressibility_ratio']:.2%}")
        print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_effectiveness_scoring_based(
    archive: Path,
    files: List[Path],
    high_effectiveness_threshold: float = 0.6,
    low_effectiveness_threshold: float = 0.3,
    high_effectiveness_compression: str = 'lzma',
    high_effectiveness_level: int = 9,
    low_effectiveness_compression: str = 'stored',
    low_effectiveness_level: int = 0,
    default_compression: str = 'deflate',
    default_level: int = 6,
    test_methods: Optional[str] = None,
    test_levels: Optional[str] = None,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with compression selected based on file compression effectiveness scoring.
    
    This command creates archives with compression automatically selected based on compression
    effectiveness scores. Files are tested with different compression methods and levels, scored
    on effectiveness, and compressed accordingly.
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        high_effectiveness_threshold: Effectiveness score threshold for high effectiveness files (default: 0.6).
        low_effectiveness_threshold: Effectiveness score threshold for low effectiveness files (default: 0.3).
        high_effectiveness_compression: Compression method for high effectiveness files (default: 'lzma').
        high_effectiveness_level: Compression level for high effectiveness files (0-9, default: 9).
        low_effectiveness_compression: Compression method for low effectiveness files (default: 'stored').
        low_effectiveness_level: Compression level for low effectiveness files (0-9, default: 0).
        default_compression: Default compression method for medium effectiveness files (default: 'deflate').
        default_level: Default compression level for medium effectiveness files (0-9, default: 6).
        test_methods: Comma-separated list of compression methods to test.
        test_levels: Comma-separated list of compression levels to test.
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_effectiveness_scoring_based_compression
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Parse test methods
    test_methods_list = None
    if test_methods:
        test_methods_list = [m.strip() for m in test_methods.split(',')]
        for method in test_methods_list:
            if method not in ['stored', 'deflate', 'bzip2', 'lzma']:
                _print_error(f"Invalid compression method: {method}", exit_code=2)
    
    # Parse test levels
    test_levels_list = None
    if test_levels:
        try:
            test_levels_list = [int(l.strip()) for l in test_levels.split(',')]
            for level in test_levels_list:
                if not (0 <= level <= 9):
                    _print_error(f"Invalid compression level: {level} (must be 0-9)", exit_code=2)
        except ValueError as e:
            _print_error(f"Invalid test levels format: {e}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with effectiveness scoring-based compression: {archive}")
            print(f"Files/directories: {len(files)}")
            print(f"High effectiveness threshold: > {high_effectiveness_threshold:.2f}")
            print(f"Low effectiveness threshold: < {low_effectiveness_threshold:.2f}")
            print(f"High effectiveness files: {high_effectiveness_compression} level {high_effectiveness_level}")
            print(f"Low effectiveness files: {low_effectiveness_compression} level {low_effectiveness_level}")
            print(f"Medium effectiveness files: {default_compression} level {default_level}")
            if test_methods_list:
                print(f"Test methods: {', '.join(test_methods_list)}")
            if test_levels_list:
                print(f"Test levels: {', '.join(map(str, test_levels_list))}")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, comp_method: str, comp_level: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [{comp_method} level {comp_level}]")
        
        # Create archive with effectiveness scoring-based compression
        result = create_archive_with_effectiveness_scoring_based_compression(
            archive_path=archive,
            file_paths=files,
            high_effectiveness_threshold=high_effectiveness_threshold,
            low_effectiveness_threshold=low_effectiveness_threshold,
            high_effectiveness_compression=high_effectiveness_compression,
            high_effectiveness_level=high_effectiveness_level,
            low_effectiveness_compression=low_effectiveness_compression,
            low_effectiveness_level=low_effectiveness_level,
            default_compression=default_compression,
            default_level=default_level,
            test_methods=test_methods_list,
            test_levels=test_levels_list,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with Effectiveness Scoring-Based Compression")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        print("📊 Effectiveness Statistics:")
        stats = result['statistics']
        print(f"  High effectiveness files (> {high_effectiveness_threshold:.2f}): {stats['high_effectiveness_files']:,} files ({_format_size(stats['high_effectiveness_files_size'])} {stats['high_effectiveness_files_size']:,} bytes)")
        print(f"  Medium effectiveness files: {stats['medium_effectiveness_files']:,} files ({_format_size(stats['medium_effectiveness_files_size'])} {stats['medium_effectiveness_files_size']:,} bytes)")
        print(f"  Low effectiveness files (< {low_effectiveness_threshold:.2f}): {stats['low_effectiveness_files']:,} files ({_format_size(stats['low_effectiveness_files_size'])} {stats['low_effectiveness_files_size']:,} bytes)")
        print(f"  Average effectiveness score: {stats['average_effectiveness_score']:.2f}")
        print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_similarity_based(
    archive: Path,
    files: List[Path],
    similarity_threshold: float = 0.8,
    sample_size: int = 4096,
    group_compression: str = 'deflate',
    group_level: int = 6,
    default_compression: str = 'deflate',
    default_level: int = 6,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with automatic compression selection based on file content similarity.
    
    This command analyzes file content to group similar files together and apply similar
    compression strategies. Files with similar content (based on content hash of samples)
    are grouped and use the same compression method and level.
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        similarity_threshold: Similarity threshold for grouping (0.0-1.0, default: 0.8).
        sample_size: Maximum bytes to sample from each file for similarity comparison (default: 4096).
        group_compression: Compression method for similar file groups (default: deflate).
        group_level: Compression level for similar file groups (0-9, default: 6).
        default_compression: Default compression method for unique files (default: deflate).
        default_level: Default compression level for unique files (0-9, default: 6).
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_similarity_based_compression
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Validate similarity threshold
    if not 0.0 <= similarity_threshold <= 1.0:
        _print_error(f"Similarity threshold must be between 0.0 and 1.0, got: {similarity_threshold}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with similarity-based compression: {archive}")
            print(f"Files/directories: {len(files)}")
            print(f"Similarity threshold: {similarity_threshold}")
            print(f"Sample size: {sample_size} bytes")
            print(f"Group compression: {group_compression} level {group_level}")
            print(f"Default compression: {default_compression} level {default_level}")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, comp_method: str, comp_level: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [{comp_method} level {comp_level}]")
        
        # Create archive with similarity-based compression
        result = create_archive_with_similarity_based_compression(
            archive_path=archive,
            file_paths=files,
            similarity_threshold=similarity_threshold,
            sample_size=sample_size if sample_size > 0 else None,
            group_compression=group_compression,
            group_level=group_level,
            default_compression=default_compression,
            default_level=default_level,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with Similarity-Based Compression")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        print("🔗 Similarity Groups:")
        print(f"  Similarity groups found: {result['statistics']['similarity_groups']:,}")
        print(f"  Grouped files: {result['statistics']['grouped_files']:,}")
        print(f"  Unique files: {result['statistics']['unique_files']:,}")
        if result['statistics']['group_sizes']:
            print(f"  Group size distribution:")
            for size, count in sorted(result['statistics']['group_sizes'].items(), key=lambda x: int(x[0])):
                print(f"    {size} files per group: {count:,} groups")
        print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_naming_based(
    archive: Path,
    files: List[Path],
    naming_patterns: Optional[str] = None,
    default_compression: str = 'deflate',
    default_level: int = 6,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with automatic compression selection based on file naming patterns.
    
    This command analyzes file names (not paths) to automatically select compression
    methods and levels based on naming conventions, prefixes, suffixes, or patterns.
    
    Patterns support glob-style matching (e.g., 'backup_*', '*_temp.*', '*.old').
    Patterns are matched against the filename only (not full path).
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        naming_patterns: JSON string mapping filename patterns to compression settings.
        default_compression: Default compression method for files not matching any pattern.
        default_level: Default compression level for files not matching any pattern (0-9).
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    import json
    from .utils import create_archive_with_naming_based_compression
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Parse naming patterns from JSON string
    naming_patterns_dict = None
    if naming_patterns:
        try:
            naming_patterns_dict = json.loads(naming_patterns)
        except json.JSONDecodeError as e:
            _print_error(f"Invalid JSON in naming patterns: {e}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with naming-based compression: {archive}")
            print(f"Files/directories: {len(files)}")
            if naming_patterns_dict:
                print(f"Naming patterns: {len(naming_patterns_dict)} patterns")
            print(f"Default compression: {default_compression} level {default_level}")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, comp_method: str, comp_level: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [{comp_method} level {comp_level}]")
        
        # Create archive with naming-based compression
        result = create_archive_with_naming_based_compression(
            archive_path=archive,
            file_paths=files,
            naming_patterns=naming_patterns_dict,
            default_compression=default_compression,
            default_level=default_level,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with Naming-Based Compression")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        if result['statistics']['pattern_matches']:
            print("📝 Naming Pattern Matches:")
            for pattern, count in sorted(result['statistics']['pattern_matches'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {pattern}: {count:,} files")
            print()
        
        print(f"📊 Default matches: {result['statistics']['default_matches']:,} files")
        print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_depth_based(
    archive: Path,
    files: List[Path],
    depth_thresholds: Optional[str] = None,
    depth_compressions: Optional[str] = None,
    default_compression: str = 'deflate',
    default_level: int = 6,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with automatic compression selection based on directory depth.
    
    This command analyzes directory depth (number of directory levels) to automatically
    select compression methods and levels. Files at different directory depths can use
    different compression strategies.
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        depth_thresholds: JSON array of depth thresholds for custom depth ranges.
        depth_compressions: JSON array of compression settings for each depth range.
        default_compression: Default compression method for files beyond configured depths.
        default_level: Default compression level for files beyond configured depths (0-9).
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    import json
    from .utils import create_archive_with_depth_based_compression
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Parse depth thresholds and compressions from JSON strings
    depth_thresholds_list = None
    if depth_thresholds:
        try:
            depth_thresholds_list = json.loads(depth_thresholds)
            if not isinstance(depth_thresholds_list, list):
                _print_error("depth_thresholds must be a JSON array", exit_code=2)
        except json.JSONDecodeError as e:
            _print_error(f"Invalid JSON in depth thresholds: {e}", exit_code=2)
    
    depth_compressions_list = None
    if depth_compressions:
        try:
            depth_compressions_list = json.loads(depth_compressions)
            if not isinstance(depth_compressions_list, list):
                _print_error("depth_compressions must be a JSON array", exit_code=2)
        except json.JSONDecodeError as e:
            _print_error(f"Invalid JSON in depth compressions: {e}", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with depth-based compression: {archive}")
            print(f"Files/directories: {len(files)}")
            if depth_thresholds_list:
                print(f"Depth thresholds: {depth_thresholds_list}")
            if depth_compressions_list:
                print(f"Depth compressions: {len(depth_compressions_list)} settings")
            print(f"Default compression: {default_compression} level {default_level}")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, comp_method: str, comp_level: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [{comp_method} level {comp_level}]")
        
        # Create archive with depth-based compression
        result = create_archive_with_depth_based_compression(
            archive_path=archive,
            file_paths=files,
            depth_thresholds=depth_thresholds_list,
            depth_compressions=depth_compressions_list,
            default_compression=default_compression,
            default_level=default_level,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with Depth-Based Compression")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        if result['statistics']['depth_distribution']:
            print("📁 Depth Distribution:")
            for depth, count in sorted(result['statistics']['depth_distribution'].items(), key=lambda x: int(x[0])):
                print(f"  Depth {depth}: {count:,} files")
            print()
        
        if result['statistics']['depth_category_distribution']:
            print("📂 Depth Category Distribution:")
            for category, count in sorted(result['statistics']['depth_category_distribution'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {category}: {count:,} files")
            print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_create_access_based(
    archive: Path,
    files: List[Path],
    frequent_threshold_days: int = 7,
    rare_threshold_days: int = 90,
    frequent_compression: str = 'deflate',
    frequent_level: int = 6,
    rare_compression: str = 'lzma',
    rare_level: int = 9,
    archive_comment: Optional[str] = None,
    password: Optional[str] = None,
    aes_version: int = 1,
    preserve_metadata: bool = True,
    quiet: bool = False,
) -> None:
    """Create an archive with automatic compression selection based on file access time.
    
    This command analyzes file access times (atime) to automatically select compression
    methods and levels. Frequently accessed files use faster compression, while rarely
    accessed files use better compression for archival purposes.
    
    Note: Access time (atime) may not be available on all systems or may be disabled
    for performance. This function falls back to modification time (mtime) if atime
    is unavailable.
    
    Args:
        archive: Path to the archive file to create.
        files: List of file/directory paths to add to archive.
        frequent_threshold_days: Number of days to consider a file 'frequently accessed' (default: 7).
        rare_threshold_days: Number of days to consider a file 'rarely accessed' (default: 90).
        frequent_compression: Compression method for frequently accessed files (default: deflate).
        frequent_level: Compression level for frequently accessed files (0-9, default: 6).
        rare_compression: Compression method for rarely accessed files (default: lzma).
        rare_level: Compression level for rarely accessed files (0-9, default: 9).
        archive_comment: Archive comment.
        password: Password for encryption.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        quiet: Suppress progress output.
    """
    from .utils import create_archive_with_access_based_compression
    
    # Validate files exist
    for file_path in files:
        if not file_path.exists():
            _print_error(f"Path not found: {file_path}", exit_code=2)
    
    # Validate archive doesn't exist
    if archive.exists():
        _print_error(f"Archive already exists: {archive}", exit_code=2)
    
    # Validate thresholds
    if frequent_threshold_days < 0 or rare_threshold_days < 0:
        _print_error("Threshold days must be >= 0", exit_code=2)
    if frequent_threshold_days >= rare_threshold_days:
        _print_error("frequent_threshold_days must be < rare_threshold_days", exit_code=2)
    
    # Convert password to bytes if provided
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    
    # Convert comment to bytes if provided
    comment_bytes = None
    if archive_comment:
        comment_bytes = archive_comment.encode('utf-8') if isinstance(archive_comment, str) else archive_comment
    
    try:
        if not quiet:
            print(f"Creating archive with access-based compression: {archive}")
            print(f"Files/directories: {len(files)}")
            print(f"Frequent threshold: {frequent_threshold_days} days")
            print(f"Rare threshold: {rare_threshold_days} days")
            print("-" * 80)
        
        # Progress callback
        def progress_callback(entry_name: str, current: int, total: int, comp_method: str, comp_level: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name} [{comp_method} level {comp_level}]")
        
        # Create archive with access-based compression
        result = create_archive_with_access_based_compression(
            archive_path=archive,
            file_paths=files,
            frequent_threshold_days=frequent_threshold_days,
            rare_threshold_days=rare_threshold_days,
            frequent_compression=frequent_compression,
            frequent_level=frequent_level,
            rare_compression=rare_compression,
            rare_level=rare_level,
            archive_comment=comment_bytes or "",
            password=password_bytes,
            aes_version=aes_version,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback if not quiet else None,
        )
        
        # Print results
        print("=" * 80)
        print("Archive Created Successfully with Access-Based Compression")
        print("=" * 80)
        print()
        
        print("📦 Archive Information:")
        print(f"  Path: {result['archive_path']}")
        print(f"  Total files: {result['total_files']:,}")
        print(f"  Total size: {_format_size(result['total_size'])} ({result['total_size']:,} bytes)")
        print(f"  Compressed size: {_format_size(result['compressed_size'])} ({result['compressed_size']:,} bytes)")
        print(f"  Compression ratio: {result['compression_ratio']:.2%}")
        print()
        
        print("⏰ File Access Categories:")
        print(f"  Frequent files (< {frequent_threshold_days} days): {result['statistics']['frequent_files']:,} files ({_format_size(result['statistics']['frequent_files_size'])})")
        print(f"  Medium files ({frequent_threshold_days}-{rare_threshold_days} days): {result['statistics']['medium_files']:,} files ({_format_size(result['statistics']['medium_files_size'])})")
        print(f"  Rare files (> {rare_threshold_days} days): {result['statistics']['rare_files']:,} files ({_format_size(result['statistics']['rare_files_size'])})")
        print()
        
        if result['statistics']['method_usage']:
            print("🔧 Compression Method Usage:")
            for method, count in sorted(result['statistics']['method_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {method}: {count:,} files")
            print()
        
        print(f"✅ Archive created successfully!")
        print(f"   Space saved: {_format_size(result['statistics']['total_space_saved'])} ({result['statistics']['space_saved_percent']:.1f}%)")
        
    except Exception as e:
        _print_error(f"Failed to create archive: {e}", exit_code=1)
        raise


def _cmd_batch_convert_smart(
    archives: List[Path],
    output_dir: Path,
    target_format: str = "zip",
    strategy: str = "best_compression",
    sample_size: Optional[int] = None,
    test_methods: Optional[List[str]] = None,
    test_levels: Optional[List[int]] = None,
    source_format: Optional[str] = None,
    password: Optional[str] = None,
    password_file: Optional[Path] = None,
    use_external_tool_for_rar: bool = False,
    no_preserve_metadata: bool = False,
    stop_on_error: bool = False,
    quiet: bool = False,
) -> None:
    """Batch convert multiple archives with automatic optimal compression selection.
    
    This command converts multiple archives from one format to another, analyzing
    each file in each archive to determine optimal compression settings and creating
    optimally compressed target archives.
    
    Args:
        archives: List of paths to source archive files to convert.
        output_dir: Directory where converted archives will be created.
        target_format: Target archive format ('zip', 'tar', '7z', default: zip).
        strategy: Compression strategy ('best_compression', 'balanced', 'fastest', 'fastest_decompression').
        sample_size: Maximum bytes to sample from each file for analysis (None = analyze entire file).
        test_methods: Compression methods to test (default: all methods).
        test_levels: Compression levels to test (default: [1, 3, 6, 9]).
        source_format: Source format name (auto-detected if not specified).
        password: Password for encrypted source archives.
        password_file: File containing password for encrypted source archives.
        use_external_tool_for_rar: Use external tools to extract compressed RAR entries.
        no_preserve_metadata: Do not preserve file metadata.
        stop_on_error: Stop processing on first error.
        quiet: Suppress progress output.
    """
    from .utils import batch_convert_with_smart_compression
    
    # Validate archives exist
    for archive in archives:
        if not archive.exists():
            _print_error(f"Archive not found: {archive}", exit_code=2)
    
    # Handle password
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    elif password_file:
        if not password_file.exists():
            _print_error(f"Password file not found: {password_file}", exit_code=2)
        try:
            password_bytes = password_file.read_bytes().strip()
        except Exception as e:
            _print_error(f"Error reading password file: {e}", exit_code=2)
    
    try:
        # Create progress callback
        def progress_callback(archive_path: str, current: int, total: int, status: str) -> None:
            if not quiet:
                if status == 'processing':
                    print(f"  [{current}/{total}] Processing: {Path(archive_path).name}", end='\r')
                elif status.startswith('analyzing'):
                    print(f"  [{current}/{total}] {status}: {Path(archive_path).name}", end='\r')
                elif status == 'success':
                    print(f"  [{current}/{total}] ✅ {Path(archive_path).name}")
                elif status == 'error':
                    print(f"  [{current}/{total}] ❌ {Path(archive_path).name}")
        
        if not quiet:
            print(f"Batch converting {len(archives)} archives with smart compression...")
            print(f"Target format: {target_format}")
            print(f"Strategy: {strategy}")
            print(f"Output directory: {output_dir}")
            print("-" * 80)
        
        # Batch convert with smart compression
        result = batch_convert_with_smart_compression(
            source_archives=archives,
            output_dir=output_dir,
            target_format=target_format,
            compression_strategy=strategy,
            sample_size=sample_size,
            test_methods=test_methods,
            test_levels=test_levels,
            source_format=source_format,
            preserve_metadata=not no_preserve_metadata,
            password=password_bytes,
            use_external_tool_for_rar=use_external_tool_for_rar,
            progress_callback=progress_callback if not quiet else None,
            stop_on_error=stop_on_error,
        )
        
        if not quiet:
            print()  # New line after progress
            print("-" * 80)
        
        # Print results
        print("✅ Batch conversion complete!")
        print(f"  Total archives: {result['total']}")
        print(f"  Successful: {result['successful']}")
        print(f"  Failed: {result['failed']}")
        
        if result['successful'] > 0:
            print("\n📊 Conversion Statistics:")
            total_original_size = 0
            total_compressed_size = 0
            
            for conv_result in result['results']:
                if conv_result['success']:
                    stats = conv_result['conversion_stats']
                    total_original_size += stats['total_size']
                    total_compressed_size += stats['compressed_size']
                    print(f"  {Path(conv_result['source_path']).name}:")
                    print(f"    → {Path(conv_result['target_path']).name}")
                    print(f"    Compression ratio: {stats['compression_ratio']:.2%}")
                    print(f"    Space saved: {stats['statistics']['space_saved_percent']:.1f}%")
            
            if result['successful'] > 1:
                overall_ratio = total_compressed_size / total_original_size if total_original_size > 0 else 0.0
                overall_saved = total_original_size - total_compressed_size
                overall_saved_percent = (overall_saved / total_original_size * 100) if total_original_size > 0 else 0.0
                print(f"\n  Overall:")
                print(f"    Total original size: {total_original_size:,} bytes ({total_original_size / 1024 / 1024:.2f} MB)")
                print(f"    Total compressed size: {total_compressed_size:,} bytes ({total_compressed_size / 1024 / 1024:.2f} MB)")
                print(f"    Overall compression ratio: {overall_ratio:.2%}")
                print(f"    Total space saved: {overall_saved:,} bytes ({overall_saved_percent:.1f}%)")
        
        if result['failed'] > 0:
            print("\n❌ Failed Conversions:")
            for conv_result in result['results']:
                if not conv_result['success']:
                    print(f"  {Path(conv_result['source_path']).name}: {conv_result['error']}")
        
    except Exception as e:
        _print_error(f"Error batch converting archives: {e}", exit_code=1)


def _cmd_extract_extractable(
    source: Path,
    target: Path,
    source_format: Optional[str] = None,
    target_format: Optional[str] = None,
    compression: Optional[str] = None,
    compression_level: Optional[int] = None,
    password: Optional[str] = None,
    password_file: Optional[Path] = None,
    preserve_metadata: bool = True,
) -> None:
    """Create a new archive containing only extractable entries from the source archive.
    
    This command analyzes a source archive to identify which entries can be extracted
    (using supported compression methods) and creates a new archive containing only those
    extractable entries. Entries with unsupported compression methods (e.g., PPMd, RAR
    compression) are excluded from the output archive.
    
    Args:
        source: Path to the source archive file.
        target: Path to the target archive file to create.
        source_format: Optional source format name (auto-detected if not specified).
        target_format: Optional target format name (auto-detected from extension if not specified).
        compression: Optional compression method for target archive.
        compression_level: Optional compression level (0-9).
        password: Password for encrypted source archives (ZIP only).
        password_file: File containing password for encrypted source archives.
        preserve_metadata: If True, preserve file metadata (timestamps, permissions).
    """
    from .utils import detect_archive_format, extract_extractable_entries
    
    # Validate source archive exists
    if not source.exists():
        _print_error(f"Source archive not found: {source}", exit_code=2)
    
    # Validate target path
    if target.exists():
        _print_error(f"Target file already exists: {target}. Remove it first or choose a different path.", exit_code=2)
    
    # Handle password
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    elif password_file:
        if not password_file.exists():
            _print_error(f"Password file not found: {password_file}", exit_code=2)
        try:
            password_bytes = password_file.read_bytes().strip()
        except Exception as e:
            _print_error(f"Error reading password file: {e}", exit_code=2)
    
    try:
        # Create progress callback
        def progress_callback(entry_name: str, current: int, total: int) -> None:
            percent = (current / total * 100) if total > 0 else 0
            print(f"  [{current}/{total}] ({percent:.1f}%) Extracting: {entry_name}", end='\r')
        
        print(f"Extracting extractable entries from: {source}")
        print(f"Output: {target}")
        if source_format:
            print(f"Source format: {source_format}")
        if target_format:
            print(f"Target format: {target_format}")
        if compression:
            print(f"Compression: {compression}")
        print("-" * 80)
        
        # Extract extractable entries
        result = extract_extractable_entries(
            source,
            target,
            source_format=source_format,
            target_format=target_format,
            compression=compression,
            compression_level=compression_level,
            password=password_bytes,
            preserve_metadata=preserve_metadata,
            progress_callback=progress_callback,
        )
        
        print()  # New line after progress
        print("-" * 80)
        
        # Print results
        print("✅ Extraction complete!")
        print(f"  Source format: {result['source_format']}")
        print(f"  Target format: {result['target_format']}")
        print(f"  Total entries in source: {result['total_entries']}")
        print(f"  Extractable entries found: {result['extractable_entries']}")
        print(f"  Non-extractable entries: {result['non_extractable_entries']}")
        print(f"  Entries extracted: {result['entries_extracted']}")
        print(f"  Entries skipped: {result['entries_skipped']}")
        print(f"  Total size: {result['total_size']:,} bytes ({result['total_size'] / 1024 / 1024:.2f} MB)")
        
        if result['non_extractable_entries'] > 0:
            print(f"\n⚠️  Non-extractable entries ({result['non_extractable_entries']}):")
            for entry_name in result['non_extractable_list'][:10]:
                print(f"    - {entry_name}")
            if len(result['non_extractable_list']) > 10:
                print(f"    ... and {len(result['non_extractable_list']) - 10} more")
            print("\n💡 Tip: Use external tools or convert the archive to extract non-extractable entries.")
        
        if result['errors']:
            print(f"\n⚠️  Errors encountered ({len(result['errors'])}):")
            for error in result['errors'][:5]:
                print(f"    - {error}")
            if len(result['errors']) > 5:
                print(f"    ... and {len(result['errors']) - 5} more errors")
        
    except Exception as e:
        _print_error(f"Error extracting extractable entries: {e}", exit_code=1)


def _cmd_normalize(
    archive: Path,
    output: Path,
    normalize_paths: bool = True,
    remove_empty_dirs: bool = True,
    standardize_compression: Optional[str] = None,
    compression_level: Optional[int] = None,
    sort_entries: bool = True,
    preserve_metadata: bool = True,
    password: Optional[str] = None,
    password_file: Optional[Path] = None,
    format: Optional[str] = None,
    quiet: bool = False,
) -> None:
    """Normalize an archive by standardizing paths, compression, and metadata.
    
    Args:
        archive: Path to the source archive file.
        output: Path to the output normalized archive file (will be created/overwritten).
        normalize_paths: Normalize path separators to forward slashes (default: True).
        remove_empty_dirs: Remove empty directory entries (default: True).
        standardize_compression: Compression method to standardize all entries to (default: preserve original).
        compression_level: Compression level (0-9) when standardize_compression is set (default: format-specific).
        sort_entries: Sort entries alphabetically (default: True).
        preserve_metadata: Preserve file metadata (timestamps, permissions) (default: True).
        password: Password for encrypted source archives (ZIP only).
        password_file: File containing password for encrypted source archives.
        format: Archive format (auto-detected if not specified).
        quiet: Suppress progress output.
    """
    from .utils import detect_archive_format, normalize_archive
    
    # Detect format if not specified
    if format is None:
        format = detect_archive_format(archive)
    
    if format is None:
        _print_error("Could not detect archive format. Please specify --format.", exit_code=2)
    
    # Select reader and writer classes based on format
    reader_class_map = {
        'zip': ZipReader,
        'tar': TarReader,
        '7z': SevenZipReader,
    }
    
    writer_class_map = {
        'zip': ZipWriter,
        'tar': TarWriter,
        '7z': SevenZipWriter,
    }
    
    reader_class = reader_class_map.get(format)
    if reader_class is None:
        _print_error(f"Unsupported format for normalization: {format}", exit_code=2)
    
    writer_class = writer_class_map.get(format)
    if writer_class is None:
        _print_error(f"Normalization not supported for format: {format} (writing not available)", exit_code=2)
    
    # Validate archive exists
    if not archive.exists():
        _print_error(f"Archive not found: {archive}", exit_code=2)
    
    # Validate output path
    if output.exists():
        _print_error(f"Output file already exists: {output}. Remove it first or choose a different path.", exit_code=2)
    
    # Handle password
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    elif password_file:
        if not password_file.exists():
            _print_error(f"Password file not found: {password_file}", exit_code=2)
        try:
            password_bytes = password_file.read_bytes().strip()
        except Exception as e:
            _print_error(f"Error reading password file: {e}", exit_code=2)
    
    # Normalize compression method name
    compression_normalized = None
    if standardize_compression:
        compression_map = {
            'stored': 'stored',
            'deflate': 'deflate',
            'bzip2': 'bzip2',
            'lzma': 'lzma',
        }
        compression_normalized = compression_map.get(standardize_compression.lower())
        if compression_normalized is None:
            _print_error(
                f"Invalid compression method: {standardize_compression}. "
                f"Must be one of: stored, deflate, bzip2, lzma",
                exit_code=2
            )
    
    # Validate compression level
    if compression_level is not None:
        if compression_level < 0 or compression_level > 9:
            _print_error("Compression level must be between 0 and 9.", exit_code=2)
    
    try:
        # Create progress callback
        def progress_callback(entry_name: str, current: int, total: int) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                print(f"  [{current}/{total}] ({percent:.1f}%) {entry_name}", end='\r')
        
        if not quiet:
            print(f"Normalizing archive: {archive}")
            print(f"Output: {output}")
            if normalize_paths:
                print("  ✓ Normalizing paths")
            if remove_empty_dirs:
                print("  ✓ Removing empty directories")
            if standardize_compression:
                print(f"  ✓ Standardizing compression: {compression_normalized}")
                if compression_level is not None:
                    print(f"    Compression level: {compression_level}")
            if sort_entries:
                print("  ✓ Sorting entries")
            print("-" * 80)
        
        # Normalize archive
        result = normalize_archive(
            archive,
            output,
            reader_class=reader_class,
            writer_class=writer_class,
            normalize_paths=normalize_paths,
            remove_empty_dirs=remove_empty_dirs,
            standardize_compression=compression_normalized,
            compression_level=compression_level,
            sort_entries=sort_entries,
            preserve_metadata=preserve_metadata,
            password=password_bytes,
            progress_callback=progress_callback if not quiet else None,
        )
        
        if not quiet:
            print()  # New line after progress
            print("-" * 80)
            print("✅ Normalization complete!")
            print(f"  Original entries: {result['original_entries']}")
            print(f"  Normalized entries: {result['normalized_entries']}")
            if result['empty_dirs_removed'] > 0:
                print(f"  Empty directories removed: {result['empty_dirs_removed']}")
            if result['paths_normalized'] > 0:
                print(f"  Paths normalized: {result['paths_normalized']}")
            if result['compression_standardized'] > 0:
                print(f"  Entries with standardized compression: {result['compression_standardized']}")
            print(f"  Original size: {_format_size(result['original_size'])}")
            print(f"  Normalized size: {_format_size(result['normalized_size'])}")
            
            if result['size_change'] != 0:
                if result['size_change'] > 0:
                    print(f"  Size increase: {_format_size(result['size_change'])} ({result['size_change_percent']:.1f}%)")
                else:
                    print(f"  Size reduction: {_format_size(-result['size_change'])} ({-result['size_change_percent']:.1f}%)")
            else:
                print(f"  Size unchanged")
            
            if result['errors']:
                print(f"\n  ⚠️  Errors: {len(result['errors'])}")
                for error in result['errors'][:5]:
                    print(f"    - {error['entry']}: {error['error']}")
                if len(result['errors']) > 5:
                    print(f"    ... and {len(result['errors']) - 5} more errors")
    
    except Exception as e:
        _print_error(f"Failed to normalize archive: {e}", exit_code=1)


def _cmd_recover(
    archive: Path,
    output_dir: Path,
    no_skip_crc: bool = False,
    no_partial_recovery: bool = False,
    password: Optional[str] = None,
    password_file: Optional[Path] = None,
    format: Optional[str] = None,
    quiet: bool = False,
) -> None:
    """Attempt to recover data from a corrupted archive by extracting readable entries.
    
    This command attempts to extract as much data as possible from a corrupted archive,
    even when some entries are corrupted or unreadable. Useful for data recovery scenarios.
    
    Args:
        archive: Path to the corrupted archive file.
        output_dir: Directory where recovered files will be extracted.
        no_skip_crc: If True, do not skip CRC verification (may skip entries with CRC errors).
        no_partial_recovery: If True, do not attempt partial recovery of corrupted entries.
        password: Password for encrypted archives.
        password_file: File containing password for encrypted archives.
        format: Archive format (auto-detected if not specified).
        quiet: Suppress progress output.
    """
    from .utils import detect_archive_format, recover_corrupted_archive
    
    # Detect format if not specified
    if format is None:
        format = detect_archive_format(archive)
    
    if format is None:
        _print_error("Could not detect archive format. Please specify --format.", exit_code=2)
    
    # Select reader class based on format
    reader_class_map = {
        'zip': ZipReader,
        'tar': TarReader,
        'gzip': GzipReader,
        'bzip2': Bzip2Reader,
        'xz': XzReader,
        '7z': SevenZipReader,
        'rar': RarReader,
    }
    
    reader_class = reader_class_map.get(format)
    if reader_class is None:
        _print_error(f"Unsupported format for recovery: {format}", exit_code=2)
    
    # Validate archive exists
    if not archive.exists():
        _print_error(f"Archive not found: {archive}", exit_code=2)
    
    # Handle password
    password_bytes = None
    if password:
        password_bytes = password.encode('utf-8')
    elif password_file:
        if not password_file.exists():
            _print_error(f"Password file not found: {password_file}", exit_code=2)
        try:
            password_bytes = password_file.read_bytes().strip()
        except Exception as e:
            _print_error(f"Error reading password file: {e}", exit_code=2)
    
    try:
        # Create progress callback
        def progress_callback(entry_name: str, current: int, total: int, status: str) -> None:
            if not quiet:
                percent = (current / total * 100) if total > 0 else 0
                status_symbol = {
                    'recovered': '✅',
                    'partial': '⚠️',
                    'failed': '❌',
                    'skipped': '⏭️',
                    'processing': '🔄',
                }.get(status, '•')
                print(f"  [{current}/{total}] ({percent:.1f}%) {status_symbol} {Path(entry_name).name}", end='\r')
        
        if not quiet:
            print(f"Recovering data from corrupted archive: {archive}")
            print(f"Output directory: {output_dir}")
            print(f"Skip CRC: {not no_skip_crc}")
            print(f"Attempt partial recovery: {not no_partial_recovery}")
            print("-" * 80)
        
        # Recover corrupted archive
        result = recover_corrupted_archive(
            archive_path=archive,
            output_dir=output_dir,
            reader_class=reader_class,
            skip_crc=not no_skip_crc,
            attempt_partial_recovery=not no_partial_recovery,
            password=password_bytes,
            progress_callback=progress_callback if not quiet else None,
        )
        
        if not quiet:
            print()  # New line after progress
            print("-" * 80)
        
        # Print results
        print("✅ Recovery complete!")
        print(f"  Total entries found: {result['total_entries']}")
        print(f"  Successfully recovered: {result['recovered_entries']}")
        print(f"  Partially recovered: {result['partial_entries']}")
        print(f"  Failed to recover: {result['failed_entries']}")
        print(f"  Skipped (directories): {result['skipped_entries']}")
        print(f"  Recovery rate: {result['recovery_rate']:.1%}")
        
        if result['recovered_entries'] > 0:
            print(f"\n📦 Successfully Recovered ({result['recovered_entries']} files):")
            print(f"  Total size: {result['total_size_recovered']:,} bytes ({result['total_size_recovered'] / 1024 / 1024:.2f} MB)")
            if not quiet and result['recovered_files']:
                print(f"  Sample files:")
                for file_path in result['recovered_files'][:10]:
                    print(f"    ✅ {file_path}")
                if len(result['recovered_files']) > 10:
                    print(f"    ... and {len(result['recovered_files']) - 10} more")
        
        if result['partial_entries'] > 0:
            print(f"\n⚠️  Partially Recovered ({result['partial_entries']} files - may be incomplete):")
            print(f"  Total size: {result['total_size_partial']:,} bytes ({result['total_size_partial'] / 1024 / 1024:.2f} MB)")
            if not quiet and result['partial_files']:
                print(f"  Sample files:")
                for file_path in result['partial_files'][:10]:
                    print(f"    ⚠️  {file_path}")
                if len(result['partial_files']) > 10:
                    print(f"    ... and {len(result['partial_files']) - 10} more")
        
        if result['failed_entries'] > 0:
            print(f"\n❌ Failed to Recover ({result['failed_entries']} entries):")
            if not quiet and result['failed_files']:
                for failed in result['failed_files'][:10]:
                    print(f"    ❌ {failed['entry_name']}: {failed['error']}")
                if len(result['failed_files']) > 10:
                    print(f"    ... and {len(result['failed_files']) - 10} more")
        
        if result['recovered_entries'] > 0 or result['partial_entries'] > 0:
            print(f"\n💾 Recovered files saved to: {output_dir}")
        
    except Exception as e:
        _print_error(f"Error recovering archive: {e}", exit_code=1)


def _cmd_filter(
    archive: Path,
    output: Path,
    include_patterns: Optional[List[str]] = None,
    exclude_patterns: Optional[List[str]] = None,
    include_extensions: Optional[List[str]] = None,
    exclude_extensions: Optional[List[str]] = None,
    min_size: Optional[int] = None,
    max_size: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    case_sensitive: bool = False,
    preserve_metadata: bool = True,
    compression: Optional[str] = None,
    compression_level: Optional[int] = None,
    password: Optional[str] = None,
    password_file: Optional[Path] = None,
    format: Optional[str] = None,
    quiet: bool = False,
) -> None:
    """Create a new archive containing only entries matching filter criteria.
    
    Args:
        archive: Path to the source archive file.
        output: Path to the output archive file (will be created/overwritten).
        include_patterns: Glob patterns to include (e.g., ['*.txt', 'data/*']).
        exclude_patterns: Glob patterns to exclude (e.g., ['*.tmp', 'temp/*']).
        include_extensions: File extensions to include (with dot, e.g., '.txt').
        exclude_extensions: File extensions to exclude.
        min_size: Minimum file size in bytes.
        max_size: Maximum file size in bytes.
        start_date: Start date string (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS).
        end_date: End date string (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS).
        case_sensitive: Use case-sensitive pattern matching.
        preserve_metadata: Preserve file metadata (timestamps, permissions).
        compression: Compression method for output archive (None preserves original).
        compression_level: Compression level (0-9, None preserves original).
        password: Password for encrypted source archives (ZIP only).
        password_file: File containing password for encrypted source archives.
        format: Archive format (auto-detected if not specified).
        quiet: Suppress progress output.
    """
    from datetime import datetime
    from .utils import detect_archive_format, filter_archive
    
    # Detect format if not specified
    if format is None:
        format = detect_archive_format(archive)
    
    if format is None:
        _print_error("Could not detect archive format. Please specify --format.", exit_code=2)
    
    # Select reader and writer classes based on format
    reader_class_map = {
        'zip': ZipReader,
        'tar': TarReader,
        '7z': SevenZipReader,
    }
    
    writer_class_map = {
        'zip': ZipWriter,
        'tar': TarWriter,
        '7z': SevenZipWriter,
    }
    
    reader_class = reader_class_map.get(format)
    if reader_class is None:
        _print_error(f"Unsupported format for filtering: {format}", exit_code=2)
        return
    
    writer_class = writer_class_map.get(format)
    if writer_class is None:
        _print_error(f"Filtering not supported for format: {format} (writing not available)", exit_code=2)
        return
    
    # Get password if provided
    password_bytes = _get_password(password, password_file)
    
    # Parse dates if provided
    start_dt = None
    end_dt = None
    
    if start_date:
        try:
            # Try parsing with time first
            start_dt = datetime.strptime(start_date, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                # Try parsing date only
                start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            except ValueError:
                _print_error(f"Invalid start date format: {start_date}. Use YYYY-MM-DD or YYYY-MM-DD HH:MM:SS", exit_code=2)
                return
    
    if end_date:
        try:
            # Try parsing with time first
            end_dt = datetime.strptime(end_date, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                # Try parsing date only
                end_dt = datetime.strptime(end_date, "%Y-%m-%d")
                # Set to end of day
                end_dt = end_dt.replace(hour=23, minute=59, second=59)
            except ValueError:
                _print_error(f"Invalid end date format: {end_date}. Use YYYY-MM-DD or YYYY-MM-DD HH:MM:SS", exit_code=2)
                return
    
    # Create progress callback
    progress_cb = None
    if not quiet:
        def progress_callback(entry_name: str, current: int, total: int) -> None:
            percent = (current / total * 100) if total > 0 else 0
            print(f"  [{current}/{total}] Filtering: {entry_name} ({percent:.1f}%)", end='\r')
        progress_cb = progress_callback
    
    try:
        if not quiet:
            print(f"Filtering archive: {archive}")
            print(f"Output archive: {output}")
            print()
        
        # Filter archive
        result = filter_archive(
            source_path=archive,
            output_path=output,
            reader_class=reader_class,
            writer_class=writer_class,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            include_extensions=include_extensions,
            exclude_extensions=exclude_extensions,
            min_size=min_size,
            max_size=max_size,
            start_date=start_dt,
            end_date=end_dt,
            case_sensitive=case_sensitive,
            preserve_metadata=preserve_metadata,
            compression=compression,
            compression_level=compression_level,
            password=password_bytes,
            progress_callback=progress_cb,
        )
        
        # Print summary
        if not quiet:
            print()  # New line after progress output
            print("✅ Filtering complete!")
            print()
            print(f"Results:")
            print(f"  Total entries: {result['total_entries']}")
            print(f"  Matched entries: {result['matched_entries']}")
            print(f"  Copied entries: {result['copied_entries']}")
            print(f"  Skipped entries: {result['skipped_entries']}")
            if result['failed_entries'] > 0:
                print(f"  Failed entries: {result['failed_entries']}")
                for failed in result['failed_files']:
                    print(f"    - {failed['entry_name']}: {failed['error']}")
            print(f"  Total size: {result['total_size']:,} bytes")
            print(f"  Compressed size: {result['compressed_size']:,} bytes")
            if result['compressed_size'] > 0:
                ratio = (1 - result['compressed_size'] / result['total_size']) * 100 if result['total_size'] > 0 else 0
                print(f"  Compression ratio: {ratio:.1f}%")
        
    except Exception as e:
        _print_error(f"Error filtering archive: {e}", exit_code=1)


def _cmd_create_index(
    archive: Path,
    index: Optional[Path] = None,
    include_content_hash: bool = False,
    include_metadata: bool = True,
    format: Optional[str] = None,
    quiet: bool = False,
) -> None:
    """Create a searchable index file for an archive.
    
    Args:
        archive: Path to the archive to index.
        index: Path to the index file (default: archive.index.json).
        include_content_hash: Calculate content hash for each entry (slower but enables duplicate detection).
        include_metadata: Include full metadata (timestamps, compression info, etc.).
        format: Archive format (auto-detected if not specified).
        quiet: Suppress progress output.
    """
    from .utils import detect_archive_format
    
    # Detect format if not specified
    if format is None:
        format = detect_archive_format(archive)
    
    if format is None:
        _print_error("Could not detect archive format. Please specify --format.", exit_code=2)
    
    # Select reader class based on format
    reader_class_map = {
        'zip': ZipReader,
        'tar': TarReader,
        '7z': SevenZipReader,
        'rar': RarReader,
    }
    
    reader_class = reader_class_map.get(format)
    if reader_class is None:
        _print_error(f"Unsupported format for indexing: {format}", exit_code=2)
        return
    
    # Progress callback
    def progress_callback(entry_name: str, current: int, total: int) -> None:
        if not quiet:
            percent = (current / total * 100) if total > 0 else 0
            print(f"  [{current}/{total}] ({percent:.1f}%) Indexing: {entry_name}", end='\r')
    
    try:
        if not quiet:
            print(f"Creating index for archive: {archive}")
            if index:
                print(f"Index file: {index}")
            print("-" * 80)
        
        # Create index
        result = create_archive_index(
            archive_path=archive,
            index_path=index,
            reader_class=reader_class,
            include_content_hash=include_content_hash,
            include_metadata=include_metadata,
            progress_callback=progress_callback,
        )
        
        if not quiet:
            print()  # New line after progress
            print("-" * 80)
            print("✅ Index created successfully!")
            print()
            print(f"Index file: {result['index_path']}")
            print(f"Archive: {result['archive_path']}")
            print(f"Format: {result['archive_format']}")
            print(f"Total entries indexed: {result['total_entries']}")
            print(f"Index size: {result['index_size']:,} bytes ({result['index_size'] / 1024:.2f} KB)")
            print(f"Created: {result['creation_time']}")
            
            if result['errors']:
                print(f"\n⚠️  Warnings ({len(result['errors'])}):")
                for error in result['errors'][:10]:  # Show first 10 errors
                    print(f"  - {error}")
                if len(result['errors']) > 10:
                    print(f"  ... and {len(result['errors']) - 10} more")
    
    except Exception as e:
        _print_error(f"Error creating index: {e}", exit_code=1)


def _cmd_search_index(
    index: Path,
    pattern: str,
    use_regex: bool = False,
    case_sensitive: bool = True,
    search_metadata: bool = False,
    min_size: Optional[int] = None,
    max_size: Optional[int] = None,
    compression_method: Optional[str] = None,
    has_content_hash: Optional[bool] = None,
) -> None:
    """Search an archive index without opening the archive.
    
    Args:
        index: Path to the index file.
        pattern: Pattern to search for (glob pattern by default, regex if --regex).
        use_regex: Treat pattern as a regular expression.
        case_sensitive: Use case-sensitive pattern matching.
        search_metadata: Also search in metadata fields.
        min_size: Minimum file size in bytes.
        max_size: Maximum file size in bytes.
        compression_method: Filter by compression method name.
        has_content_hash: Filter entries that have/don't have content hash.
    """
    try:
        # Search index
        results = search_archive_index(
            index_path=index,
            pattern=pattern,
            use_regex=use_regex,
            case_sensitive=case_sensitive,
            search_metadata=search_metadata,
            min_size=min_size,
            max_size=max_size,
            compression_method=compression_method,
            has_content_hash=has_content_hash,
        )
        
        if not results:
            print(f"No matches found for pattern: {pattern}")
            return
        
        print(f"Found {len(results)} matching entries:")
        print("-" * 80)
        
        for entry in results:
            size_str = f"{entry.get('size', 0):,} bytes"
            comp_str = ""
            if entry.get('compressed_size'):
                comp_str = f" ({entry.get('compressed_size', 0):,} compressed)"
            
            print(f"  {entry.get('name', '')}")
            print(f"    Size: {size_str}{comp_str}")
            
            if entry.get('mtime'):
                print(f"    Modified: {entry.get('mtime')}")
            
            if entry.get('compression_method'):
                print(f"    Compression: {entry.get('compression_method')}")
            
            if entry.get('is_directory'):
                print(f"    Type: Directory")
            
            print()
    
    except Exception as e:
        _print_error(f"Error searching index: {e}", exit_code=1)


def _cmd_update_index(
    archive: Path,
    index: Optional[Path] = None,
    force: bool = False,
    include_content_hash: bool = False,
    include_metadata: bool = True,
    format: Optional[str] = None,
    quiet: bool = False,
) -> None:
    """Update an existing archive index or create a new one.
    
    Args:
        archive: Path to the archive to index.
        index: Path to the index file (default: archive.index.json).
        force: Force rebuild even if index appears up-to-date.
        include_content_hash: Calculate content hash for each entry.
        include_metadata: Include full metadata.
        format: Archive format (auto-detected if not specified).
        quiet: Suppress progress output.
    """
    from .utils import detect_archive_format
    
    # Detect format if not specified
    if format is None:
        format = detect_archive_format(archive)
    
    if format is None:
        _print_error("Could not detect archive format. Please specify --format.", exit_code=2)
    
    # Select reader class based on format
    reader_class_map = {
        'zip': ZipReader,
        'tar': TarReader,
        '7z': SevenZipReader,
        'rar': RarReader,
    }
    
    reader_class = reader_class_map.get(format)
    if reader_class is None:
        _print_error(f"Unsupported format for indexing: {format}", exit_code=2)
        return
    
    # Progress callback
    def progress_callback(entry_name: str, current: int, total: int) -> None:
        if not quiet:
            percent = (current / total * 100) if total > 0 else 0
            print(f"  [{current}/{total}] ({percent:.1f}%) Indexing: {entry_name}", end='\r')
    
    try:
        if not quiet:
            print(f"Updating index for archive: {archive}")
            if index:
                print(f"Index file: {index}")
            print("-" * 80)
        
        # Update index
        result = update_archive_index(
            archive_path=archive,
            index_path=index,
            reader_class=reader_class,
            force_rebuild=force,
            include_content_hash=include_content_hash,
            include_metadata=include_metadata,
            progress_callback=progress_callback,
        )
        
        if not quiet:
            print()  # New line after progress
            print("-" * 80)
            if result.get('updated', True):
                print("✅ Index updated successfully!")
            else:
                print("✅ Index is up-to-date (no update needed)")
            print()
            print(f"Index file: {result['index_path']}")
            print(f"Archive: {result['archive_path']}")
            print(f"Format: {result['archive_format']}")
            print(f"Total entries indexed: {result['total_entries']}")
            print(f"Index size: {result['index_size']:,} bytes ({result['index_size'] / 1024:.2f} KB)")
            print(f"Created: {result['creation_time']}")
            
            if result.get('errors'):
                print(f"\n⚠️  Warnings ({len(result['errors'])}):")
                for error in result['errors'][:10]:  # Show first 10 errors
                    print(f"  - {error}")
                if len(result['errors']) > 10:
                    print(f"  ... and {len(result['errors']) - 10} more")
    
    except Exception as e:
        _print_error(f"Error updating index: {e}", exit_code=1)


def _cmd_create_from_file_list(
    archive: Path,
    file_list: Path,
    base_dir: Optional[Path] = None,
    compression: str = "deflate",
    compression_level: int = 6,
    comment: str = "",
    password: Optional[str] = None,
    password_file: Optional[Path] = None,
    aes_version: int = 1,
    quiet: bool = False,
) -> None:
    """Create an archive from a file list (text file containing paths).
    
    Args:
        archive: Path where the archive will be created.
        file_list: Path to text file containing list of files/directories to add.
        base_dir: Optional base directory for resolving relative paths in file list.
        compression: Compression method ('stored', 'deflate', 'bzip2', 'lzma', 'ppmd').
        compression_level: Compression level (0-9 for DEFLATE/LZMA, 1-9 for BZIP2).
        comment: Optional archive comment.
        password: Optional password for encryption (str).
        password_file: Optional path to file containing password.
        aes_version: AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256).
        quiet: If True, suppress progress output.
    """
    if archive.exists():
        _print_error(f"Refusing to overwrite existing archive: {archive}", exit_code=2)
    
    if not file_list.exists():
        _print_error(f"File list not found: {file_list}", exit_code=2)
    
    # Get password if provided
    password_bytes = _get_password(password, password_file)
    
    # Create progress callback
    progress_cb = None
    if not quiet:
        def progress_callback(entry_name: str, current: int, total: int) -> None:
            print(f"  [{current}/{total}] Adding: {entry_name}")
        progress_cb = progress_callback
    
    try:
        # Create archive from file list
        result = create_archive_from_file_list(
            archive_path=archive,
            file_list_path=file_list,
            base_dir=str(base_dir) if base_dir else None,
            compression=compression,
            compression_level=compression_level,
            archive_comment=comment,
            password=password_bytes,
            aes_version=aes_version,
            progress_callback=progress_cb,
        )
        
        # Display results
        if not quiet:
            print(f"\nArchive created: {archive}")
            print(f"Files added: {result['total_files']}")
            print(f"Directories: {result['total_directories']}")
            print(f"Total size: {result['total_size']:,} bytes ({result['total_size'] / (1024**2):.2f} MB)")
            print(f"Compressed size: {result['compressed_size']:,} bytes ({result['compressed_size'] / (1024**2):.2f} MB)")
            if result['total_size'] > 0:
                ratio = (1 - result['compressed_size'] / result['total_size']) * 100
                print(f"Compression ratio: {ratio:.1f}%")
            
            if result['skipped_files']:
                print(f"\nSkipped files ({len(result['skipped_files'])}):")
                for skipped in result['skipped_files'][:10]:  # Show first 10
                    print(f"  • {skipped['path']}: {skipped['error']}")
                if len(result['skipped_files']) > 10:
                    print(f"  ... and {len(result['skipped_files']) - 10} more")
    
    except Exception as e:
        _print_error(f"Failed to create archive from file list: {e}", exit_code=1)


def _cmd_health_check(
    archive: Path,
    format_name: Optional[str] = None,
) -> None:
    """Perform a quick health check on an archive without full CRC validation.
    
    Args:
        archive: Path to the archive to check.
        format_name: Optional format name (auto-detected if not provided).
    """
    try:
        # Perform quick health check
        result = quick_health_check(archive, format_name=format_name)
        
        # Display results
        print(f"Archive: {archive}")
        print(f"Format: {result['format'].upper() if result['format'] else 'Unknown'}")
        print("=" * 80)
        
        # Overall health status
        if result['healthy']:
            print("\n✅ Archive is healthy")
        else:
            print("\n❌ Archive has issues")
        
        # Basic information
        print(f"\n📊 Basic Information:")
        print(f"  Can open: {'✅ Yes' if result['can_open'] else '❌ No'}")
        print(f"  Can list entries: {'✅ Yes' if result['can_list'] else '❌ No'}")
        print(f"  Total entries: {result['total_entries']}")
        print(f"  Files: {result['file_count']}")
        print(f"  Directories: {result['directory_count']}")
        
        # Size information
        if result['total_size'] > 0:
            print(f"\n📦 Size Information:")
            print(f"  Total uncompressed size: {result['total_size']:,} bytes ({result['total_size'] / (1024**2):.2f} MB)")
            print(f"  Total compressed size: {result['total_compressed_size']:,} bytes ({result['total_compressed_size'] / (1024**2):.2f} MB)")
            if result['total_size'] > 0:
                ratio = (1 - result['total_compressed_size'] / result['total_size']) * 100
                print(f"  Compression ratio: {ratio:.1f}%")
        
        # Issues
        if result['issues']:
            print(f"\n❌ Issues Found ({len(result['issues'])}):")
            for issue in result['issues']:
                print(f"  • {issue}")
        
        # Warnings
        if result['warnings']:
            print(f"\n⚠️  Warnings ({len(result['warnings'])}):")
            for warning in result['warnings']:
                print(f"  • {warning}")
        
        # Error
        if result['error']:
            print(f"\n❌ Error: {result['error']}")
        
        # Exit with appropriate code
        if not result['healthy']:
            sys.exit(1)
        
    except Exception as e:
        _print_error(f"Error performing health check: {e}", exit_code=1)


def _cmd_analyze(
    archive: Path,
    format_name: Optional[str] = None,
) -> None:
    """Analyze an archive and report on supported/unsupported features.
    
    Args:
        archive: Path to the archive to analyze.
        format_name: Optional format name (auto-detected if not provided).
    """
    try:
        # Analyze archive features
        result = analyze_archive_features(archive, format_name=format_name)
        
        # Display results
        print(f"Archive: {archive}")
        print(f"Format: {result['format'].upper()}")
        print("=" * 80)
        
        # Supported features
        if result['supported_features']:
            print("\n✅ Supported Features:")
            for feature in result['supported_features']:
                print(f"  • {feature}")
        
        # Unsupported features
        if result['unsupported_features']:
            print("\n❌ Unsupported Features:")
            for feature in result['unsupported_features']:
                print(f"  • {feature}")
        
        # Compression methods
        if result['compression_methods']:
            print("\n📦 Compression Methods:")
            for method_name, method_info in result['compression_methods'].items():
                status = "✅" if method_info['supported'] else "❌"
                count = method_info['count']
                print(f"  {status} {method_name}: {count} entries")
        
        # Encryption
        print("\n🔐 Encryption:")
        if result['encryption']['supported']:
            print("  ✅ Encryption is supported")
            if result['encryption']['encrypted_entries']:
                print(f"  • {len(result['encryption']['encrypted_entries'])} encrypted entries")
        else:
            print("  ❌ Encryption is not supported")
            if result['encryption']['encrypted_entries']:
                print(f"  • {len(result['encryption']['encrypted_entries'])} encrypted entries (cannot extract)")
        
        # Warnings
        if result['warnings']:
            print("\n⚠️  Warnings:")
            for warning in result['warnings']:
                print(f"  • {warning}")
        
        # Recommendations
        if result['recommendations']:
            print("\n💡 Recommendations:")
            for recommendation in result['recommendations']:
                print(f"  • {recommendation}")
        
        # Entry summary
        if result['entries']:
            total_entries = len(result['entries'])
            supported_entries = sum(1 for e in result['entries'] if e.get('supported', False))
            unsupported_entries = total_entries - supported_entries
            
            print("\n📊 Entry Summary:")
            print(f"  Total entries: {total_entries}")
            print(f"  Supported entries: {supported_entries}")
            if unsupported_entries > 0:
                print(f"  Unsupported entries: {unsupported_entries}")
                
                # Show unsupported entries
                unsupported_list = [
                    e['name'] for e in result['entries']
                    if not e.get('supported', False) and not e.get('is_directory', False)
                ]
                if unsupported_list:
                    print("\n  Unsupported entries:")
                    for entry_name in unsupported_list[:10]:  # Show first 10
                        entry_info = next((e for e in result['entries'] if e['name'] == entry_name), None)
                        if entry_info:
                            method_name = entry_info.get('compression_method_name', 'Unknown')
                            print(f"    • {entry_name} ({method_name})")
                    if len(unsupported_list) > 10:
                        print(f"    ... and {len(unsupported_list) - 10} more")
        
        print("=" * 80)
        
    except Exception as e:
        _print_error(f"Error analyzing archive: {e}", exit_code=1)


def _cmd_analyze_compression(
    files: list[Path],
    sample_size: Optional[int] = None,
    test_methods: Optional[list[str]] = None,
    test_levels: Optional[list[int]] = None,
    quiet: bool = False,
) -> None:
    """Analyze files and suggest optimal compression methods and levels.
    
    Args:
        files: List of file paths to analyze.
        sample_size: Optional maximum number of bytes to sample from each file.
        test_methods: List of compression methods to test.
        test_levels: List of compression levels to test.
        quiet: If True, suppress progress output.
    """
    from .utils import analyze_compression_options
    
    # Helper function to format file sizes
    def format_size(size_bytes: int) -> str:
        """Format size in bytes to human-readable format."""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size_bytes < 1024.0:
                return f"{size_bytes:.2f} {unit}"
            size_bytes /= 1024.0
        return f"{size_bytes:.2f} PB"
    
    # Progress callback
    def progress_callback(file_path, current_file, total_files, method, level):
        if not quiet:
            if method and level is not None:
                print(f"[{current_file}/{total_files}] Testing {file_path} with {method} level {level}...", end='\r', flush=True)
            else:
                print(f"[{current_file}/{total_files}] Analyzing {file_path}...", end='\r', flush=True)
    
    try:
        # Convert test_methods and test_levels
        if test_methods:
            methods = test_methods
        else:
            methods = None
        
        if test_levels:
            levels = test_levels
        else:
            levels = None
        
        # Analyze files
        result = analyze_compression_options(
            file_paths=files,
            sample_size=sample_size,
            test_methods=methods,
            test_levels=levels,
            progress_callback=progress_callback if not quiet else None,
        )
        
        if not quiet:
            print()  # New line after progress
        
        # Display results
        print("=" * 80)
        print("Compression Analysis Results")
        print("=" * 80)
        
        # Summary
        summary = result['summary']
        print(f"\n📊 Summary:")
        print(f"  Total files analyzed: {summary['total_files']}")
        print(f"  Total original size: {format_size(summary['total_original_size'])}")
        if summary['best_overall_method']:
            print(f"  Best overall method: {summary['best_overall_method']}")
            print(f"  Best overall level: {summary['best_overall_level']}")
        print(f"  Average compression ratio: {summary['average_compression_ratio']:.2%}")
        
        # Overall recommendations
        if summary['recommendations']:
            print(f"\n💡 Overall Recommendations:")
            for rec in summary['recommendations']:
                print(f"  • Method: {rec['method']}, Level: {rec['level']} - {rec['reason']}")
        
        # Per-file results
        print(f"\n📁 Per-File Analysis:")
        for file_result in result['files']:
            if 'error' in file_result:
                print(f"\n  ❌ {file_result['path']}: {file_result['error']}")
                continue
            
            print(f"\n  📄 {file_result['path']}")
            print(f"     Size: {format_size(file_result['size'])}")
            print(f"     Type: {file_result['file_type']}")
            
            if file_result['recommendations']:
                print(f"     Top Recommendations:")
                for idx, rec in enumerate(file_result['recommendations'][:3], 1):
                    print(f"       {idx}. {rec['method']} level {rec['level']} - {rec['reason']}")
                    print(f"          Compressed size: {format_size(rec['compressed_size'])}")
                    print(f"          Compression ratio: {rec['compression_ratio']:.2%}")
                    print(f"          Size reduction: {rec['size_reduction_percent']:.1f}%")
                    print(f"          Speed score: {rec['speed_score']}/10")
        
        print("\n" + "=" * 80)
        
    except Exception as e:
        _print_error(f"Error analyzing compression options: {e}", exit_code=1)


def _cmd_sync(
    archive: Path,
    source_directory: Path,
    compression: str = "deflate",
    compression_level: int = 6,
    remove_deleted: bool = False,
    compare_by: str = "mtime",
    preserve_metadata: bool = True,
    password: Optional[str] = None,
    password_file: Optional[Path] = None,
    quiet: bool = False,
) -> None:
    """Synchronize an archive with a directory.
    
    Args:
        archive: Path to the archive file (will be created if it doesn't exist).
        source_directory: Path to the source directory to sync with.
        compression: Compression method ('stored', 'deflate', 'bzip2', 'lzma', 'ppmd').
        compression_level: Compression level (0-9 for DEFLATE/LZMA, 1-9 for BZIP2).
        remove_deleted: If True, remove entries from archive that no longer exist in source.
        compare_by: How to detect changes ('mtime', 'size', 'both').
        preserve_metadata: If True, preserve file metadata (timestamps, permissions).
        password: Optional password for encryption (str).
        password_file: Optional path to file containing password.
        quiet: If True, suppress progress output.
    """
    if not source_directory.exists():
        _print_error(f"Source directory does not exist: {source_directory}", exit_code=2)
    
    if not source_directory.is_dir():
        _print_error(f"Source path is not a directory: {source_directory}", exit_code=2)
    
    # Get password if provided
    password_bytes = _get_password(password, password_file)
    
    # Create progress callback
    progress_cb = None
    if not quiet:
        def progress_callback(entry_name: str, current: int, total: int) -> None:
            print(f"  [{current}/{total}] Processing: {entry_name}")
        progress_cb = progress_callback
    
    try:
        # Synchronize archive with directory
        result = sync_archive_with_directory(
            archive_path=archive,
            source_directory=source_directory,
            compression=compression,
            compression_level=compression_level,
            remove_deleted=remove_deleted,
            compare_by=compare_by,
            preserve_metadata=preserve_metadata,
            password=password_bytes,
            progress_callback=progress_cb,
        )
        
        # Display results
        if not quiet:
            print(f"\nArchive synchronized: {archive}")
            print(f"Source directory: {source_directory}")
            print("=" * 80)
            print(f"\n📊 Synchronization Results:")
            print(f"  Files added: {result['files_added']}")
            print(f"  Files updated: {result['files_updated']}")
            print(f"  Files removed: {result['files_removed']}")
            print(f"  Files unchanged: {result['files_unchanged']}")
            print(f"  Directories added: {result['directories_added']}")
            
            if result['total_size'] > 0:
                print(f"\n📦 Size Information:")
                print(f"  Total size: {result['total_size']:,} bytes ({result['total_size'] / (1024**2):.2f} MB)")
                print(f"  Compressed size: {result['compressed_size']:,} bytes ({result['compressed_size'] / (1024**2):.2f} MB)")
                if result['total_size'] > 0:
                    ratio = (1 - result['compressed_size'] / result['total_size']) * 100
                    print(f"  Compression ratio: {ratio:.1f}%")
            
            # Show added files (first 10)
            if result['added_files']:
                print(f"\n➕ Added Files ({len(result['added_files'])}):")
                for file_path in result['added_files'][:10]:
                    print(f"  • {file_path}")
                if len(result['added_files']) > 10:
                    print(f"  ... and {len(result['added_files']) - 10} more")
            
            # Show updated files (first 10)
            if result['updated_files']:
                print(f"\n🔄 Updated Files ({len(result['updated_files'])}):")
                for file_path in result['updated_files'][:10]:
                    print(f"  • {file_path}")
                if len(result['updated_files']) > 10:
                    print(f"  ... and {len(result['updated_files']) - 10} more")
            
            # Show removed files (first 10)
            if result['removed_files']:
                print(f"\n➖ Removed Files ({len(result['removed_files'])}):")
                for file_path in result['removed_files'][:10]:
                    print(f"  • {file_path}")
                if len(result['removed_files']) > 10:
                    print(f"  ... and {len(result['removed_files']) - 10} more")
            
            # Show skipped files
            if result['skipped_files']:
                print(f"\n⚠️  Skipped Files ({len(result['skipped_files'])}):")
                for skipped in result['skipped_files'][:10]:
                    if isinstance(skipped, tuple):
                        file_path, error = skipped
                        print(f"  • {file_path}: {error}")
                    else:
                        print(f"  • {skipped}")
                if len(result['skipped_files']) > 10:
                    print(f"  ... and {len(result['skipped_files']) - 10} more")
            
            print("=" * 80)
    
    except Exception as e:
        _print_error(f"Failed to synchronize archive: {e}", exit_code=1)


def _cmd_test(archive: Path, skip_crc: bool = False) -> int:
    """Test archive integrity without extracting.

    Args:
        archive: Path to the archive to test.
        skip_crc: If True, skip CRC32 verification.

    Returns:
        Exit code: 0 if all tests pass, 1 if any test fails.
    """
    crc_mode = "skip" if skip_crc else "strict"
    failed_entries = []
    passed_count = 0
    failed_count = 0
    
    try:
        with ZipReader(archive, crc_verification=crc_mode) as z:
            total_entries = z.get_entry_count()
            
            if total_entries == 0:
                print(f"Archive: {archive}")
                print("Status: OK (empty archive)")
                return 0
            
            print(f"Testing archive: {archive}")
            print(f"Entries: {total_entries}")
            print("-" * 80)
            
            # Test each file entry
            for entry in z.iter_files():
                try:
                    # Try to open and read the entry (this will trigger CRC validation if enabled)
                    with z.open(entry.name) as f:
                        # Read all data to trigger decompression and CRC check
                        while True:
                            chunk = f.read(1024 * 1024)  # Read in 1MB chunks
                            if not chunk:
                                break
                    passed_count += 1
                    print(f"  OK: {entry.name}")
                except ZipCrcError as e:
                    failed_count += 1
                    failed_entries.append((entry.name, str(e)))
                    print(f"  FAIL: {entry.name} - {e}")
                except (ZipError, Exception) as e:
                    failed_count += 1
                    failed_entries.append((entry.name, str(e)))
                    print(f"  ERROR: {entry.name} - {e}")
            
            # Summary
            print("-" * 80)
            if failed_count == 0:
                print(f"Status: OK ({passed_count} entries verified)")
                return 0
            else:
                print(f"Status: FAILED ({failed_count} failed, {passed_count} passed)")
                return 1
                
    except ZipError as e:
        _print_error(f"Archive test failed: {e}", exit_code=1)
        return 1
    except Exception as e:
        _print_error(f"Unexpected error during archive test: {e}", exit_code=1)
        return 1


def _cmd_create_checksum(
    archive: Path,
    output: Optional[Path] = None,
    algorithm: str = 'sha256',
    format: Optional[str] = None,
    quiet: bool = False,
) -> None:
    """Create a checksum file for archive entries.
    
    Args:
        archive: Path to the archive to create checksums for.
        output: Path to the checksum file to create.
        algorithm: Hash algorithm to use.
        format: Archive format (auto-detected if not specified).
        quiet: If True, suppress progress output.
    """
    from .utils import detect_archive_format
    
    # Detect format if not specified
    if format is None:
        format = detect_archive_format(archive)
        if format is None:
            _print_error(f"Could not detect archive format for: {archive}. Please specify --format.", exit_code=2)
    
    # Select reader class based on format
    reader_class_map = {
        'zip': ZipReader,
        'tar': TarReader,
        '7z': SevenZipReader,
        'rar': RarReader,
    }
    
    reader_class = reader_class_map.get(format)
    if reader_class is None:
        _print_error(f"Unsupported format for checksum creation: {format}", exit_code=2)
    
    # Validate archive exists
    if not archive.exists():
        _print_error(f"Archive not found: {archive}", exit_code=2)
    
    # Progress callback
    def progress_callback(entry_name: str, current: int, total: int) -> None:
        if not quiet:
            print(f"Processing {current}/{total}: {entry_name}", end='\r')
    
    try:
        checksum_file_path = create_checksum_file(
            archive_path=archive,
            checksum_file_path=output,
            algorithm=algorithm,
            reader_class=reader_class,
            progress_callback=progress_callback if not quiet else None,
        )
        
        if not quiet:
            print()  # New line after progress
        print(f"Checksum file created: {checksum_file_path}")
        print(f"Algorithm: {algorithm.upper()}")
    except ValueError as e:
        _print_error(str(e), exit_code=2)
    except OSError as e:
        _print_error(f"Failed to create checksum file: {e}", exit_code=1)


def _cmd_verify_checksum(
    archive: Path,
    checksum_file: Path,
    algorithm: Optional[str] = None,
    format: Optional[str] = None,
    quiet: bool = False,
) -> None:
    """Verify an archive against a checksum file.
    
    Args:
        archive: Path to the archive to verify.
        checksum_file: Path to the checksum file to verify against.
        algorithm: Hash algorithm used in checksum file.
        format: Archive format (auto-detected if not specified).
        quiet: If True, suppress progress output.
    """
    from .utils import detect_archive_format
    
    # Detect format if not specified
    if format is None:
        format = detect_archive_format(archive)
        if format is None:
            _print_error(f"Could not detect archive format for: {archive}. Please specify --format.", exit_code=2)
    
    # Select reader class based on format
    reader_class_map = {
        'zip': ZipReader,
        'tar': TarReader,
        '7z': SevenZipReader,
        'rar': RarReader,
    }
    
    reader_class = reader_class_map.get(format)
    if reader_class is None:
        _print_error(f"Unsupported format for checksum verification: {format}", exit_code=2)
    
    # Validate files exist
    if not archive.exists():
        _print_error(f"Archive not found: {archive}", exit_code=2)
    if not checksum_file.exists():
        _print_error(f"Checksum file not found: {checksum_file}", exit_code=2)
    
    # Progress callback
    def progress_callback(entry_name: str, current: int, total: int, status: str = '') -> None:
        if not quiet:
            status_str = f" [{status}]" if status else ""
            print(f"Verifying {current}/{total}: {entry_name}{status_str}", end='\r')
    
    try:
        result = verify_checksum_file(
            archive_path=archive,
            checksum_file_path=checksum_file,
            algorithm=algorithm,
            reader_class=reader_class,
            progress_callback=progress_callback if not quiet else None,
        )
        
        if not quiet:
            print()  # New line after progress
        
        # Display results
        print(f"Archive: {archive}")
        print(f"Checksum file: {checksum_file}")
        print(f"Algorithm: {result['algorithm'].upper()}")
        print("-" * 80)
        print(f"Total entries: {result['total_entries']}")
        print(f"Verified: {result['verified_entries']}")
        print(f"Failed: {result['failed_entries']}")
        print(f"Missing in checksum file: {result['missing_entries']}")
        print(f"Extra in checksum file: {result['extra_entries']}")
        
        if result['errors']:
            print("-" * 80)
            print("Errors:")
            for error in result['errors']:
                print(f"  {error['entry_name']}: {error['error_message']}")
        
        print("-" * 80)
        if result['valid']:
            print("Status: OK (all checksums verified)")
        else:
            print("Status: FAILED (checksum verification failed)")
            sys.exit(1)
    except ValueError as e:
        _print_error(str(e), exit_code=2)
    except OSError as e:
        _print_error(f"Failed to verify checksum file: {e}", exit_code=1)


def _cmd_benchmark(
    benchmark_type: str,
    num_files: int = 20,
    file_size_mb: float = 0.1,
    data_size_mb: int = 10,
    compression: str = "deflate",
    compression_level: int = 6,
    output_file: Optional[Path] = None
) -> None:
    """
    Run performance benchmarks and display results.
    
    Args:
        benchmark_type: Type of benchmark to run ("multi-threaded" or "memory-mapped")
        num_files: Number of files for multi-threaded benchmark
        file_size_mb: Size of each file in megabytes for multi-threaded benchmark
        data_size_mb: Size of test data in megabytes for memory-mapped benchmark
        compression: Compression method to use
        compression_level: Compression level
        output_file: Optional file to save JSON results
    """
    if BenchmarkRunner is None:
        _print_error(
            "Benchmarking module is not available. "
            "The benchmark module may not be installed or there was an import error.",
            exit_code=1
        )
        return
    
    runner = BenchmarkRunner()
    
    print("=" * 80)
    print("DNZIP Performance Benchmark")
    print("=" * 80)
    print()
    
    if benchmark_type == "multi-threaded":
        print(f"Running multi-threaded compression benchmark...")
        print(f"  Files: {num_files}")
        print(f"  File Size: {file_size_mb} MB each")
        print(f"  Compression: {compression} (level {compression_level})")
        print()
        
        comparisons = run_multi_threaded_comparison(
            num_files=num_files,
            file_size_mb=file_size_mb,
            compression=compression,
            compression_level=compression_level
        )
        
        # Display results
        baseline = comparisons[0].baseline if comparisons else None
        if baseline:
            print("Baseline (1 thread):")
            print(f"  Time: {baseline.time_seconds:.3f}s")
            if baseline.speed_mbps:
                print(f"  Speed: {baseline.speed_mbps:.2f} MB/s")
            print()
        
        for comparison in comparisons:
            print(runner.format_comparison(comparison))
            print()
        
        # Save results if requested
        if output_file:
            results = []
            for comparison in comparisons:
                results.append(comparison.baseline)
                results.append(comparison.comparison)
            runner.save_results(results, str(output_file))
            print(f"Results saved to: {output_file}")
    
    elif benchmark_type == "memory-mapped":
        print(f"Running memory-mapped I/O benchmark...")
        print(f"  Data Size: {data_size_mb} MB")
        print(f"  Compression: {compression} (level {compression_level})")
        print()
        
        comparison = run_memory_mapped_comparison(
            data_size_mb=data_size_mb,
            compression=compression,
            compression_level=compression_level
        )
        
        print(runner.format_comparison(comparison))
        print()
        
        # Save results if requested
        if output_file:
            results = [comparison.baseline, comparison.comparison]
            runner.save_results(results, str(output_file))
            print(f"Results saved to: {output_file}")
    
    else:
        _print_error(f"Unknown benchmark type: {benchmark_type}", exit_code=1)
        return
    
    print("=" * 80)
    print("Benchmark completed!")
    print("=" * 80)


def _cmd_benchmark_compression(
    archive: Path,
    methods: Optional[list[str]] = None,
    levels: Optional[list[int]] = None,
    max_entries: Optional[int] = None,
    no_sample: bool = False,
    no_timing: bool = False,
    format: Optional[str] = None,
    quiet: bool = False
) -> None:
    """
    Benchmark different compression methods and levels on an archive.
    
    Args:
        archive: Path to the archive file to benchmark
        methods: List of compression methods to test
        levels: List of compression levels to test
        max_entries: Maximum number of entries to test
        no_sample: If True, test first N entries instead of sampling
        no_timing: If True, skip timing measurements
        format: Archive format (auto-detected if not specified)
        quiet: If True, suppress progress output
    """
    from .utils import benchmark_archive_compression, detect_archive_format
    
    # Detect format if not specified
    if format is None:
        format = detect_archive_format(archive)
        if format is None:
            _print_error(f"Could not detect archive format for: {archive}. Please specify --format.", exit_code=2)
            return
    
    # Select reader class based on format
    reader_class_map = {
        'zip': ZipReader,
        'tar': TarReader,
        '7z': SevenZipReader,
        'rar': RarReader,
    }
    
    reader_class = reader_class_map.get(format)
    if reader_class is None:
        _print_error(f"Unsupported format for compression benchmarking: {format}", exit_code=2)
        return
    
    # Validate archive exists
    if not archive.exists():
        _print_error(f"Archive not found: {archive}", exit_code=2)
        return
    
    # Progress callback
    def progress_callback(entry_name: str, current: int, total: int, method: str, level: int) -> None:
        if not quiet:
            print(f"Testing {current}/{total}: {method} level {level} - {entry_name}", end='\r')
    
    try:
        # Run benchmark
        result = benchmark_archive_compression(
            archive_path=archive,
            test_methods=methods,
            test_levels=levels,
            max_entries=max_entries,
            sample_entries=not no_sample,
            include_timing=not no_timing,
            reader_class=reader_class,
            progress_callback=progress_callback if not quiet else None,
        )
        
        if not quiet:
            print()  # New line after progress
        
        # Display results
        print("=" * 80)
        print("Archive Compression Benchmark Results")
        print("=" * 80)
        print()
        print(f"Archive: {result['archive_path']}")
        print(f"Original Size: {_format_size(result['original_size'])}")
        print(f"Total Entries: {result['total_entries']}")
        print(f"Tested Entries: {result['tested_entries']}")
        print()
        
        # Display test results summary
        print("Test Results Summary:")
        print("-" * 80)
        print(f"{'Method':<10} {'Level':<6} {'Ratio':<8} {'Reduction':<12} {'Size':<12}", end="")
        if not no_timing:
            print(f" {'Comp Time':<12} {'Decomp Time':<12} {'Comp Speed':<12} {'Decomp Speed':<12}")
        else:
            print()
        print("-" * 80)
        
        for test_result in result['test_results'][:10]:  # Show top 10
            method = test_result['method']
            level = test_result['level']
            ratio = test_result['compression_ratio']
            reduction = test_result['size_reduction_percent']
            size = _format_size(test_result['total_compressed_size'])
            
            print(f"{method:<10} {level:<6} {ratio:<8.3f} {reduction:<11.1f}% {size:<12}", end="")
            
            if not no_timing:
                comp_time = test_result.get('compression_time_seconds')
                decomp_time = test_result.get('decompression_time_seconds')
                comp_speed = test_result.get('compression_speed_mbps')
                decomp_speed = test_result.get('decompression_speed_mbps')
                
                comp_time_str = f"{comp_time:.3f}s" if comp_time else "N/A"
                decomp_time_str = f"{decomp_time:.3f}s" if decomp_time else "N/A"
                comp_speed_str = f"{comp_speed:.2f} MB/s" if comp_speed else "N/A"
                decomp_speed_str = f"{decomp_speed:.2f} MB/s" if decomp_speed else "N/A"
                
                print(f" {comp_time_str:<12} {decomp_time_str:<12} {comp_speed_str:<12} {decomp_speed_str:<12}")
            else:
                print()
        
        if len(result['test_results']) > 10:
            print(f"... and {len(result['test_results']) - 10} more results")
        
        print()
        
        # Display recommendations
        print("Recommendations:")
        print("-" * 80)
        
        recs = result['recommendations']
        
        if 'best_compression' in recs:
            rec = recs['best_compression']
            print(f"Best Compression: {rec['method']} level {rec['level']}")
            print(f"  Compression Ratio: {rec['compression_ratio']:.3f}")
            print(f"  Size Reduction: {rec['size_reduction_percent']:.1f}%")
        
        if 'fastest_compression' in recs:
            rec = recs['fastest_compression']
            print(f"Fastest Compression: {rec['method']} level {rec['level']}")
            print(f"  Speed: {rec['compression_speed_mbps']:.2f} MB/s")
            print(f"  Time: {rec['compression_time_seconds']:.3f}s")
        
        if 'fastest_decompression' in recs:
            rec = recs['fastest_decompression']
            print(f"Fastest Decompression: {rec['method']} level {rec['level']}")
            print(f"  Speed: {rec['decompression_speed_mbps']:.2f} MB/s")
            print(f"  Time: {rec['decompression_time_seconds']:.3f}s")
        
        if 'best_balance' in recs:
            rec = recs['best_balance']
            print(f"Best Balance: {rec['method']} level {rec['level']}")
            print(f"  Compression Ratio: {rec['compression_ratio']:.3f}")
            print(f"  Compression Speed: {rec['compression_speed_mbps']:.2f} MB/s")
        
        if 'most_efficient' in recs:
            rec = recs['most_efficient']
            print(f"Most Efficient: {rec['method']} level {rec['level']}")
            print(f"  Size Reduction per Second: {rec['size_reduction_per_second']:.0f} bytes/s")
            print(f"  Compression Ratio: {rec['compression_ratio']:.3f}")
        
        print()
        
        # Display summary
        summary = result['summary']
        print("Summary:")
        print("-" * 80)
        print(f"Best Method: {summary['best_method']}")
        print(f"Best Level: {summary['best_level']}")
        print(f"Average Compression Ratio: {summary['average_compression_ratio']:.3f}")
        print(f"Potential Size Savings: {_format_size(summary['potential_size_savings'])} ({summary['potential_size_savings_percent']:.1f}%)")
        
        print()
        print("=" * 80)
        
    except ValueError as e:
        _print_error(str(e), exit_code=2)
    except OSError as e:
        _print_error(f"Failed to benchmark archive: {e}", exit_code=1)


def _format_size(size_bytes: int) -> str:
    """Format size in human-readable format."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} TB"


def _build_parser() -> argparse.ArgumentParser:
    """Create and configure the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="dnzip",
        description="DNZIP - Pure Python ZIP/ZIP64 engine (library and CLI).",
    )
    
    # Global security audit logging option (available for all commands)
    parser.add_argument(
        "--security-audit-log",
        type=str,
        metavar="FILE",
        default=None,
        help="Enable security audit logging and write logs to FILE. "
             "Security events (validation failures, path traversal attempts, etc.) "
             "will be logged to the specified file. If not specified, security audit logging is disabled.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # list
    p_list = subparsers.add_parser("list", help="List entries in an archive")
    p_list.add_argument("archive", type=Path, help="Path to the ZIP/ZIP64 archive")

    # info
    p_info = subparsers.add_parser("info", help="Show detailed info about archive entries")
    p_info.add_argument("archive", type=Path, help="Path to the archive file")
    p_info.add_argument(
        "--format",
        type=str,
        choices=["zip", "tar", "7z", "rar"],
        default=None,
        help="Archive format (zip, tar, 7z, rar). If not specified, format is auto-detected.",
    )

    # properties
    p_properties = subparsers.add_parser("properties", help="Show archive properties in JSON format")
    p_properties.add_argument("archive", type=Path, help="Path to the archive file")
    p_properties.add_argument(
        "--format",
        type=str,
        choices=["zip", "tar", "7z", "rar"],
        default=None,
        help="Archive format (zip, tar, 7z, rar). If not specified, format is auto-detected.",
    )

    # statistics
    p_statistics = subparsers.add_parser("statistics", help="Show comprehensive statistics about an archive")
    p_statistics.add_argument("archive", type=Path, help="Path to the archive to analyze")

    # search
    p_search = subparsers.add_parser("search", help="Search for files within an archive matching a pattern")
    p_search.add_argument("archive", type=Path, help="Path to the archive to search")
    p_search.add_argument("pattern", type=str, help="Pattern to search for (glob pattern by default, regex if --regex is used)")
    p_search.add_argument(
        "--regex",
        action="store_true",
        help="Treat pattern as a regular expression instead of glob pattern",
    )
    p_search.add_argument(
        "--case-insensitive",
        action="store_true",
        help="Perform case-insensitive pattern matching",
    )
    p_search.add_argument(
        "--format",
        type=str,
        choices=["zip", "tar", "7z", "rar"],
        help="Archive format (auto-detected if not specified)",
    )

    # search-content
    p_search_content = subparsers.add_parser("search-content", help="Search for text or binary patterns within archive file contents")
    p_search_content.add_argument("archive", type=Path, help="Path to the archive to search")
    p_search_content.add_argument("search_text", type=str, help="Text or binary pattern to search for within file contents")
    p_search_content.add_argument(
        "--filename-pattern",
        type=str,
        metavar="PATTERN",
        help="Optional glob pattern to filter which files to search (e.g., '*.txt')",
    )
    p_search_content.add_argument(
        "--regex",
        action="store_true",
        help="Treat search_text as a regular expression instead of plain text",
    )
    p_search_content.add_argument(
        "--case-insensitive",
        action="store_true",
        help="Perform case-insensitive search (text mode only)",
    )
    p_search_content.add_argument(
        "--text-encoding",
        type=str,
        default="utf-8",
        help="Text encoding to use when reading file contents (default: utf-8)",
    )
    p_search_content.add_argument(
        "--binary",
        action="store_true",
        help="Perform binary search (search_text interpreted as hex bytes, e.g., '0x89504e47' or '\\x89PNG')",
    )
    p_search_content.add_argument(
        "--max-file-size",
        type=int,
        metavar="BYTES",
        help="Maximum file size in bytes to search (default: no limit)",
    )
    p_search_content.add_argument(
        "--format",
        type=str,
        choices=["zip", "tar", "7z", "rar"],
        help="Archive format (auto-detected if not specified)",
    )
    p_search_content.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )

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
    p_extract.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    p_extract.add_argument(
        "--allow-absolute-paths",
        action="store_true",
        help="Allow absolute paths in entry names (default: False, for security)",
    )
    p_extract.add_argument(
        "--max-path-length",
        type=int,
        default=None,
        help="Maximum allowed path length (default: no limit)",
    )
    p_extract.add_argument(
        "--password",
        type=str,
        metavar="PASSWORD",
        help="Password for encrypted archives",
    )
    p_extract.add_argument(
        "--password-file",
        type=Path,
        metavar="FILE",
        help="Read password from file",
    )

    # extract-filtered (selective extraction with filtering)
    p_extract_filtered = subparsers.add_parser(
        "extract-filtered",
        help="Extract archive entries matching filter criteria (patterns, sizes, dates, extensions)"
    )
    p_extract_filtered.add_argument("archive", type=Path, help="Path to the archive")
    p_extract_filtered.add_argument(
        "-d",
        "--directory",
        type=Path,
        default=Path("."),
        help="Target directory to extract into (default: current directory)",
    )
    p_extract_filtered.add_argument(
        "--include-patterns",
        nargs="+",
        metavar="PATTERN",
        help="Glob patterns to include (e.g., '*.txt' 'data/*')",
    )
    p_extract_filtered.add_argument(
        "--exclude-patterns",
        nargs="+",
        metavar="PATTERN",
        help="Glob patterns to exclude (e.g., '*.tmp' 'temp/*')",
    )
    p_extract_filtered.add_argument(
        "--include-extensions",
        nargs="+",
        metavar="EXT",
        help="File extensions to include (e.g., '.txt' '.py')",
    )
    p_extract_filtered.add_argument(
        "--exclude-extensions",
        nargs="+",
        metavar="EXT",
        help="File extensions to exclude (e.g., '.tmp' '.bak')",
    )
    p_extract_filtered.add_argument(
        "--min-size",
        type=int,
        metavar="BYTES",
        help="Minimum file size in bytes (inclusive)",
    )
    p_extract_filtered.add_argument(
        "--max-size",
        type=int,
        metavar="BYTES",
        help="Maximum file size in bytes (inclusive)",
    )
    p_extract_filtered.add_argument(
        "--start-date",
        type=str,
        metavar="DATE",
        help="Start date for modification time filter (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)",
    )
    p_extract_filtered.add_argument(
        "--end-date",
        type=str,
        metavar="DATE",
        help="End date for modification time filter (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)",
    )
    p_extract_filtered.add_argument(
        "--case-sensitive",
        action="store_true",
        help="Use case-sensitive pattern matching",
    )
    p_extract_filtered.add_argument(
        "--allow-absolute-paths",
        action="store_true",
        help="Allow absolute paths in entry names (default: False, for security)",
    )
    p_extract_filtered.add_argument(
        "--max-path-length",
        type=int,
        default=None,
        help="Maximum allowed path length (default: no limit)",
    )
    p_extract_filtered.add_argument(
        "--password",
        type=str,
        metavar="PASSWORD",
        help="Password for encrypted archives",
    )
    p_extract_filtered.add_argument(
        "--password-file",
        type=Path,
        metavar="FILE",
        help="Read password from file",
    )
    p_extract_filtered.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )

    # extract-with-conflict-resolution
    p_extract_conflict = subparsers.add_parser(
        "extract-with-conflict-resolution",
        help="Extract archive entries with intelligent conflict resolution (overwrite, skip, rename, timestamp, size)"
    )
    p_extract_conflict.add_argument("archive", type=Path, help="Path to the archive")
    p_extract_conflict.add_argument(
        "-d",
        "--directory",
        type=Path,
        default=Path("."),
        help="Target directory to extract into (default: current directory)",
    )
    p_extract_conflict.add_argument(
        "--conflict-strategy",
        type=str,
        choices=["overwrite", "skip", "rename", "timestamp", "size"],
        default="rename",
        help="Strategy for handling file conflicts: overwrite (always overwrite), skip (skip existing), rename (auto-rename), timestamp (overwrite if newer), size (overwrite if size differs). Default: rename",
    )
    p_extract_conflict.add_argument(
        "--allow-absolute-paths",
        action="store_true",
        help="Allow absolute paths in entry names (default: False, for security)",
    )
    p_extract_conflict.add_argument(
        "--max-path-length",
        type=int,
        default=None,
        help="Maximum allowed path length (default: no limit)",
    )
    p_extract_conflict.add_argument(
        "--password",
        type=str,
        metavar="PASSWORD",
        help="Password for encrypted archives",
    )
    p_extract_conflict.add_argument(
        "--password-file",
        type=Path,
        metavar="FILE",
        help="Read password from file",
    )
    p_extract_conflict.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress output",
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
        choices=["stored", "deflate", "bzip2", "lzma", "ppmd"],
        default="deflate",
        help='Compression method to use ("stored", "deflate", "bzip2", "lzma", or "ppmd", default: "deflate"). '
             'Note: PPMd compression is not yet implemented and will raise an error.',
    )
    p_create.add_argument(
        "--compression-level",
        type=int,
        default=6,
        metavar="LEVEL",
        help='Compression level (0-9 for DEFLATE/LZMA, 1-9 for BZIP2, where 0=no compression, 1=fastest, 9=best compression, default: 6 for DEFLATE/LZMA, 9 for BZIP2).',
    )
    p_create.add_argument(
        "--comment",
        type=str,
        default="",
        help="Archive comment to add to the ZIP file",
    )
    p_create.add_argument(
        "--split-size",
        type=str,
        metavar="SIZE",
        help="Create a split archive with maximum part size (e.g., '64MB', '100KB', '1GB'). "
             "Each part except the last will be limited to this size. "
             "Part files will be named archive.z01, archive.z02, ..., archive.zip.",
    )
    p_create.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    p_create.add_argument(
        "--threads",
        type=int,
        default=1,
        metavar="N",
        help="Number of threads to use for parallel compression (default: 1, single-threaded). "
             "Maximum recommended: number of CPU cores. "
             "Note: Multi-threading is disabled with split archives.",
    )
    p_create.add_argument(
        "--password",
        type=str,
        metavar="PASSWORD",
        help="Password for encrypting entries (will prompt securely if not provided). "
             "Note: Requires optional dependency 'pycryptodome' or 'cryptography'.",
    )
    p_create.add_argument(
        "--password-file",
        type=Path,
        metavar="FILE",
        help="Read password from file (more secure than --password). "
             "Password should be in the file, optionally followed by newline.",
    )
    p_create.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        metavar="VERSION",
        help="AES encryption version (1=AES-128, 2=AES-192, 3=AES-256, default: 1). "
             "Only used if --password or --password-file is provided.",
    )

    # create-from-file-list
    p_create_from_list = subparsers.add_parser(
        "create-from-file-list",
        help="Create an archive from a file list (text file containing paths)"
    )
    p_create_from_list.add_argument("archive", type=Path, help="Path of the archive to create")
    p_create_from_list.add_argument(
        "file_list",
        type=Path,
        help="Path to text file containing list of files/directories to add (one per line, # for comments)",
    )
    p_create_from_list.add_argument(
        "--base-dir",
        type=Path,
        metavar="DIR",
        help="Base directory for resolving relative paths in file list (default: current directory)",
    )
    p_create_from_list.add_argument(
        "-c",
        "--compression",
        choices=["stored", "deflate", "bzip2", "lzma", "ppmd"],
        default="deflate",
        help='Compression method to use ("stored", "deflate", "bzip2", "lzma", or "ppmd", default: "deflate"). '
             'Note: PPMd compression is not yet implemented and will raise an error.',
    )
    p_create_from_list.add_argument(
        "--compression-level",
        type=int,
        default=6,
        metavar="LEVEL",
        help='Compression level (0-9 for DEFLATE/LZMA, 1-9 for BZIP2, where 0=no compression, 1=fastest, 9=best compression, default: 6 for DEFLATE/LZMA, 9 for BZIP2).',
    )
    p_create_from_list.add_argument(
        "--comment",
        type=str,
        default="",
        help="Archive comment to add to the ZIP file",
    )
    p_create_from_list.add_argument(
        "--password",
        type=str,
        metavar="PASSWORD",
        help="Password for encrypting entries (will prompt securely if not provided). "
             "Note: Requires optional dependency 'pycryptodome' or 'cryptography'.",
    )
    p_create_from_list.add_argument(
        "--password-file",
        type=Path,
        metavar="FILE",
        help="Read password from file (more secure than --password). "
             "Password should be in the file, optionally followed by newline.",
    )
    p_create_from_list.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        metavar="VERSION",
        help="AES encryption version (1=AES-128, 2=AES-192, 3=AES-256, default: 1). "
             "Only used if --password or --password-file is provided.",
    )
    p_create_from_list.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )

    # test/verify
    p_test = subparsers.add_parser("test", help="Test archive integrity without extracting")
    p_test.add_argument("archive", type=Path, help="Path to the ZIP/ZIP64 archive")
    p_test.add_argument(
        "--skip-crc",
        action="store_true",
        help="Skip CRC32 verification (faster but less thorough)",
    )
    
    # verify (alias for test)
    p_verify = subparsers.add_parser("verify", help="Verify archive integrity (alias for 'test')")
    p_verify.add_argument("archive", type=Path, help="Path to the ZIP/ZIP64 archive")
    p_verify.add_argument(
        "--skip-crc",
        action="store_true",
        help="Skip CRC32 verification (faster but less thorough)",
    )
    
    # benchmark
    p_benchmark = subparsers.add_parser("benchmark", help="Run performance benchmarks and compare results")
    p_benchmark.add_argument(
        "benchmark_type",
        choices=["multi-threaded", "memory-mapped"],
        help="Type of benchmark to run: 'multi-threaded' (compare thread counts) or 'memory-mapped' (compare I/O methods)"
    )
    p_benchmark.add_argument(
        "--num-files",
        type=int,
        default=20,
        metavar="N",
        help="Number of files for multi-threaded benchmark (default: 20)"
    )
    p_benchmark.add_argument(
        "--file-size-mb",
        type=float,
        default=0.1,
        metavar="SIZE",
        help="Size of each file in megabytes for multi-threaded benchmark (default: 0.1)"
    )
    p_benchmark.add_argument(
        "--data-size-mb",
        type=int,
        default=10,
        metavar="SIZE",
        help="Size of test data in megabytes for memory-mapped benchmark (default: 10)"
    )
    p_benchmark.add_argument(
        "--compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Compression method to use (default: deflate)"
    )
    p_benchmark.add_argument(
        "--compression-level",
        type=int,
        default=6,
        metavar="LEVEL",
        help="Compression level (0-9 for DEFLATE/LZMA, 1-9 for BZIP2, default: 6)"
    )
    p_benchmark.add_argument(
        "-o",
        "--output",
        type=Path,
        metavar="FILE",
        help="Save benchmark results to JSON file"
    )
    
    # benchmark-compression
    p_benchmark_compression = subparsers.add_parser(
        "benchmark-compression",
        help="Benchmark different compression methods and levels on an archive"
    )
    p_benchmark_compression.add_argument(
        "archive",
        type=Path,
        help="Path to the archive file to benchmark"
    )
    p_benchmark_compression.add_argument(
        "--methods",
        nargs="+",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default=None,
        metavar="METHOD",
        help="Compression methods to test (default: all methods)"
    )
    p_benchmark_compression.add_argument(
        "--levels",
        nargs="+",
        type=int,
        default=None,
        metavar="LEVEL",
        help="Compression levels to test (default: 1, 3, 6, 9)"
    )
    p_benchmark_compression.add_argument(
        "--max-entries",
        type=int,
        default=None,
        metavar="N",
        help="Maximum number of entries to test (default: all entries)"
    )
    p_benchmark_compression.add_argument(
        "--no-sample",
        action="store_true",
        help="If --max-entries is specified, test first N entries instead of sampling"
    )
    p_benchmark_compression.add_argument(
        "--no-timing",
        action="store_true",
        help="Skip timing measurements (faster, only compression ratios)"
    )
    p_benchmark_compression.add_argument(
        "--format",
        choices=["zip", "tar", "7z", "rar"],
        default=None,
        help="Archive format (auto-detected if not specified)"
    )
    p_benchmark_compression.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress output"
    )
    
    # gzip compress
    p_gzip_compress = subparsers.add_parser("gzip-compress", help="Compress a file using GZIP format")
    p_gzip_compress.add_argument("input_file", type=Path, help="Path to the file to compress")
    p_gzip_compress.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Path to the output GZIP file (default: input_file.gz)",
    )
    p_gzip_compress.add_argument(
        "--filename",
        type=str,
        help="Original filename to store in GZIP header (default: input filename)",
    )
    p_gzip_compress.add_argument(
        "--comment",
        type=str,
        help="Comment to store in GZIP header",
    )
    p_gzip_compress.add_argument(
        "--compression-level",
        type=int,
        default=6,
        choices=range(0, 10),
        metavar="LEVEL",
        help="Compression level (0-9, where 0=no compression, 1=fastest, 9=best compression, default: 6)",
    )
    
    # gzip decompress
    p_gzip_decompress = subparsers.add_parser("gzip-decompress", help="Decompress a GZIP file")
    p_gzip_decompress.add_argument("input_file", type=Path, help="Path to the GZIP file to decompress")
    p_gzip_decompress.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Path to the output file (default: input_file without .gz extension)",
    )
    p_gzip_decompress.add_argument(
        "--skip-crc",
        action="store_true",
        help="Skip CRC32 verification (faster but less thorough)",
    )
    
    # bzip2 compress
    p_bzip2_compress = subparsers.add_parser("bzip2-compress", help="Compress a file using BZIP2 format")
    p_bzip2_compress.add_argument("input_file", type=Path, help="Path to the file to compress")
    p_bzip2_compress.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Path to the output BZIP2 file (default: input_file.bz2)",
    )
    p_bzip2_compress.add_argument(
        "--compression-level",
        type=int,
        default=9,
        choices=range(1, 10),
        metavar="LEVEL",
        help="Compression level (1-9, where 1=fastest, 9=best compression, default: 9)",
    )
    
    # bzip2 decompress
    p_bzip2_decompress = subparsers.add_parser("bzip2-decompress", help="Decompress a BZIP2 file")
    p_bzip2_decompress.add_argument("input_file", type=Path, help="Path to the BZIP2 file to decompress")
    p_bzip2_decompress.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Path to the output file (default: input_file without .bz2 extension)",
    )
    
    # xz compress
    p_xz_compress = subparsers.add_parser("xz-compress", help="Compress a file using XZ format")
    p_xz_compress.add_argument("input_file", type=Path, help="Path to the file to compress")
    p_xz_compress.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Path to the output XZ file (default: input_file.xz)",
    )
    p_xz_compress.add_argument(
        "--compression-level",
        type=int,
        default=6,
        choices=range(0, 10),
        metavar="LEVEL",
        help="Compression level (0-9, where 0=fastest, 9=best compression, default: 6)",
    )
    
    # xz decompress
    p_xz_decompress = subparsers.add_parser("xz-decompress", help="Decompress an XZ file")
    p_xz_decompress.add_argument("input_file", type=Path, help="Path to the XZ file to decompress")
    p_xz_decompress.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Path to the output file (default: input_file without .xz extension)",
    )
    
    # tar create
    p_tar_create = subparsers.add_parser("tar-create", help="Create a TAR archive")
    p_tar_create.add_argument("archive", type=Path, help="Path to the output TAR archive (.tar)")
    p_tar_create.add_argument("sources", nargs="+", type=Path, help="Files and directories to add to the archive")
    
    # tar list
    p_tar_list = subparsers.add_parser("tar-list", help="List entries in a TAR archive")
    p_tar_list.add_argument("archive", type=Path, help="Path to the TAR archive (.tar)")
    
    # tar extract
    p_tar_extract = subparsers.add_parser("tar-extract", help="Extract a TAR archive")
    p_tar_extract.add_argument("archive", type=Path, help="Path to the TAR archive (.tar)")
    p_tar_extract.add_argument(
        "-d",
        "--output-dir",
        type=Path,
        default=Path("."),
        help="Directory to extract files to (default: current directory)",
    )
    p_tar_extract.add_argument(
        "--allow-absolute-paths",
        action="store_true",
        help="Allow absolute paths in entry names (default: False, for security)",
    )
    p_tar_extract.add_argument(
        "--max-path-length",
        type=int,
        default=None,
        help="Maximum allowed path length (default: no limit)",
    )
    
    # 7z create
    p_7z_create = subparsers.add_parser("7z-create", help="Create a 7Z archive (framework only - not yet implemented)")
    p_7z_create.add_argument("archive", type=Path, help="Path to the output 7Z archive (.7z)")
    p_7z_create.add_argument("sources", nargs="+", type=Path, help="Files and directories to add to the archive")
    p_7z_create.add_argument(
        "-c",
        "--compression",
        choices=["copy", "lzma", "lzma2", "bzip2", "ppmd"],
        default="lzma2",
        help="Compression method to use (default: lzma2). Note: PPMd compression is not yet implemented and will raise an error.",
    )
    p_7z_create.add_argument(
        "--compression-level",
        type=int,
        default=6,
        help="Compression level (0-9, default: 6)",
    )
    
    # 7z list
    p_7z_list = subparsers.add_parser("7z-list", help="List entries in a 7Z archive (framework only - not yet implemented)")
    p_7z_list.add_argument("archive", type=Path, help="Path to the 7Z archive (.7z)")
    
    # 7z extract
    p_7z_extract = subparsers.add_parser("7z-extract", help="Extract a 7Z archive (framework only - not yet implemented)")
    p_7z_extract.add_argument("archive", type=Path, help="Path to the 7Z archive (.7z)")
    p_7z_extract.add_argument(
        "-d",
        "--output-dir",
        type=Path,
        default=Path("."),
        help="Directory to extract files to (default: current directory)",
    )
    p_7z_extract.add_argument(
        "--allow-absolute-paths",
        action="store_true",
        help="Allow absolute paths in entry names (default: False, for security)",
    )
    p_7z_extract.add_argument(
        "--max-path-length",
        type=int,
        default=None,
        help="Maximum allowed path length (default: no limit)",
    )
    
    # rar list
    p_rar_list = subparsers.add_parser("rar-list", help="List entries in a RAR archive (read-only, basic support)")
    p_rar_list.add_argument("archive", type=Path, help="Path to the RAR archive (.rar)")
    
    # rar extract
    p_rar_extract = subparsers.add_parser("rar-extract", help="Extract a RAR archive (read-only, basic support)")
    p_rar_extract.add_argument("archive", type=Path, help="Path to the RAR archive (.rar)")
    p_rar_extract.add_argument(
        "-d",
        "--output-dir",
        type=Path,
        default=Path("."),
        help="Directory to extract files to (default: current directory)",
    )
    p_rar_extract.add_argument(
        "--allow-absolute-paths",
        action="store_true",
        help="Allow absolute paths in entry names (default: False, for security)",
    )
    p_rar_extract.add_argument(
        "--max-path-length",
        type=int,
        default=None,
        help="Maximum allowed path length (default: no limit)",
    )
    p_rar_extract.add_argument(
        "--use-external-tool",
        action="store_true",
        help="Automatically use external tools (unrar, 7z, unar) when encountering compressed or encrypted entries",
    )
    p_rar_extract.add_argument(
        "--external-tool",
        type=str,
        choices=["unrar", "7z", "unar"],
        default=None,
        help="Specific external tool to use when --use-external-tool is enabled (default: auto-detect)",
    )
    p_rar_extract.add_argument(
        "--password",
        type=str,
        default=None,
        help="Password for encrypted archives (only used with --use-external-tool)",
    )
    
    # rar extractable
    p_rar_extractable = subparsers.add_parser("rar-extractable", help="List extractable entries in a RAR archive")
    p_rar_extractable.add_argument("archive", type=Path, help="Path to the RAR archive (.rar)")
    p_rar_extractable.add_argument(
        "--no-directories",
        action="store_true",
        help="Exclude directory entries from the output",
    )
    p_rar_extractable.add_argument(
        "--detailed",
        action="store_true",
        help="Show detailed information for each entry",
    )
    
    # rar extract-external
    p_rar_extract_external = subparsers.add_parser("rar-extract-external", help="Extract RAR archive using external tool (unrar, 7z, or unar)")
    p_rar_extract_external.add_argument("archive", type=Path, help="Path to the RAR archive (.rar)")
    p_rar_extract_external.add_argument(
        "-d",
        "--output-dir",
        type=Path,
        default=None,
        help="Directory to extract files to (default: archive directory)",
    )
    p_rar_extract_external.add_argument(
        "--tool",
        choices=["unrar", "7z", "unar"],
        default=None,
        help="Specific tool to use (default: auto-detect first available)",
    )
    p_rar_extract_external.add_argument(
        "--password",
        type=str,
        default=None,
        help="Password for encrypted archives",
    )
    
    # rar check-tools
    p_rar_check_tools = subparsers.add_parser("rar-check-tools", help="Check which external RAR extraction tools are available")
    
    # rar compatibility analysis
    p_rar_compat = subparsers.add_parser("rar-compat", help="Analyze RAR archive compatibility and extractability")
    p_rar_compat.add_argument("archive", type=Path, help="Path to the RAR archive to analyze")
    p_rar_compat.add_argument(
        "--no-tool-check",
        action="store_true",
        help="Skip checking for external RAR extraction tools",
    )
    
    # update
    p_update = subparsers.add_parser("update", help="Update an existing entry in an archive")
    p_update.add_argument("archive", type=Path, help="Path to the ZIP archive to update")
    p_update.add_argument("entry", type=str, help="Entry name to update")
    p_update.add_argument("source", type=Path, help="Source file to replace the entry with")
    p_update.add_argument(
        "-c",
        "--compression",
        choices=["stored", "deflate", "bzip2", "lzma", "ppmd"],
        default="deflate",
        help='Compression method to use (default: "deflate"). '
             'Note: PPMd compression is not yet implemented and will raise an error.',
    )
    p_update.add_argument(
        "--compression-level",
        type=int,
        default=6,
        metavar="LEVEL",
        help="Compression level (0-9 for DEFLATE/LZMA, 1-9 for BZIP2, default: 6)",
    )
    
    # delete
    p_delete = subparsers.add_parser("delete", help="Delete an entry from an archive")
    p_delete.add_argument("archive", type=Path, help="Path to the ZIP archive")
    p_delete.add_argument("entry", type=str, help="Entry name to delete")
    
    # rename
    p_rename = subparsers.add_parser("rename", help="Rename an entry in an archive")
    p_rename.add_argument("archive", type=Path, help="Path to the ZIP archive")
    p_rename.add_argument("old_name", type=str, help="Current entry name")
    p_rename.add_argument("new_name", type=str, help="New entry name")
    
    # merge
    p_merge = subparsers.add_parser("merge", help="Merge multiple archives into a single archive")
    p_merge.add_argument("output", type=Path, help="Path to the output archive file")
    p_merge.add_argument("archives", nargs="+", type=Path, help="Paths to source archives to merge")
    p_merge.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite output file if it already exists (default: False)",
    )
    p_merge.add_argument(
        "--conflict-resolution",
        choices=["skip", "overwrite", "rename", "error"],
        default="skip",
        help='Strategy for handling entry name conflicts: "skip" (default, keep first), '
             '"overwrite" (keep last), "rename" (append number), or "error" (raise error)',
    )
    
    # split
    p_split = subparsers.add_parser("split", help="Split an archive into multiple smaller archives")
    p_split.add_argument("archive", type=Path, help="Path to the source archive to split")
    p_split.add_argument("output", type=Path, help="Base path for output archives (e.g., 'output' creates output_001.zip, output_002.zip, etc.)")
    p_split.add_argument(
        "--max-size",
        type=str,
        metavar="SIZE",
        help="Maximum uncompressed size per output archive (e.g., '100MB', '1GB', '500KB'). "
             "Entries are distributed so each archive doesn't exceed this size.",
    )
    p_split.add_argument(
        "--max-entries",
        type=int,
        metavar="N",
        help="Maximum number of entries per output archive. "
             "Entries are distributed so each archive doesn't exceed this count.",
    )
    p_split.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite output files if they already exist (default: False)",
    )

    # convert
    # diff
    p_compare = subparsers.add_parser("compare", help="Compare two archives and show basic differences (simpler than diff)")
    p_compare.add_argument("archive1", type=Path, help="Path to first archive")
    p_compare.add_argument("archive2", type=Path, help="Path to second archive")
    p_compare.add_argument(
        "--format",
        choices=["zip", "tar", "7z", "rar"],
        help="Archive format (auto-detected if not specified)",
    )
    
    p_compare_formats = subparsers.add_parser("compare-formats", help="Compare two archives in potentially different formats")
    p_compare_formats.add_argument("archive1", type=Path, help="Path to first archive file")
    p_compare_formats.add_argument("archive2", type=Path, help="Path to second archive file")
    p_compare_formats.add_argument(
        "--format1",
        type=str,
        help="Format name for first archive (auto-detected if not specified)",
    )
    p_compare_formats.add_argument(
        "--format2",
        type=str,
        help="Format name for second archive (auto-detected if not specified)",
    )
    p_compare_formats.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Maximum time to wait for comparison in seconds (default: 300)",
    )
    
    p_format_statistics = subparsers.add_parser("format-statistics", help="Get comprehensive statistics for an archive in any format")
    p_format_statistics.add_argument("archive", type=Path, help="Path to the archive file")
    p_format_statistics.add_argument(
        "--format",
        type=str,
        help="Format name (auto-detected if not specified)",
    )
    p_format_statistics.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Maximum time to wait in seconds (default: 300)",
    )
    
    p_diff = subparsers.add_parser("diff", help="Compare two archives and show detailed differences")
    p_diff.add_argument("archive1", type=Path, help="Path to first archive")
    p_diff.add_argument("archive2", type=Path, help="Path to second archive")
    p_diff.add_argument(
        "--format",
        choices=["zip", "tar", "7z", "rar"],
        help="Archive format (auto-detected if not specified)",
    )
    p_diff.add_argument(
        "--summary-only",
        action="store_true",
        help="Only show summary statistics, not detailed differences",
    )
    
    # export
    p_export = subparsers.add_parser("export", help="Export archive metadata to JSON or CSV format")
    p_export.add_argument("archive", type=Path, help="Path to the archive file")
    p_export.add_argument("output", type=Path, help="Path to the output file (JSON or CSV)")
    p_export.add_argument(
        "--format",
        choices=["json", "csv"],
        default="json",
        help="Output format (default: json)",
    )
    p_export.add_argument(
        "--archive-format",
        choices=["zip", "tar", "7z", "rar"],
        help="Archive format (auto-detected if not specified)",
    )
    
    p_optimize = subparsers.add_parser("optimize", help="Optimize an archive by recompressing entries with different compression settings")
    p_optimize.add_argument("archive", type=Path, help="Path to the source archive to optimize")
    p_optimize.add_argument("output", type=Path, help="Path to the output optimized archive")
    p_optimize.add_argument(
        "--compression",
        choices=["deflate", "bzip2", "lzma", "stored"],
        help="Compression method to use (default: uses original compression method for each entry)",
    )
    p_optimize.add_argument(
        "--compression-level",
        type=int,
        choices=range(10),
        metavar="[0-9]",
        help="Compression level (0-9, default: 6)",
    )
    p_optimize.add_argument(
        "--password",
        type=str,
        help="Password for encrypted ZIP archives (source only)",
    )
    p_optimize.add_argument(
        "--password-file",
        type=Path,
        help="File containing password for encrypted ZIP archives (source only)",
    )
    p_optimize.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file timestamps and metadata",
    )
    
    p_repair = subparsers.add_parser("repair", help="Validate and repair an archive by extracting valid entries")
    p_repair.add_argument("archive", type=Path, help="Path to the archive to validate/repair")
    p_repair.add_argument(
        "output",
        type=Path,
        nargs="?",
        default=None,
        help="Path to the repaired archive (optional, only used with --repair flag)",
    )
    p_repair.add_argument(
        "--repair",
        action="store_true",
        help="Create a repaired archive with only valid entries (requires output path)",
    )
    p_repair.add_argument(
        "--crc-mode",
        choices=["strict", "warn", "skip"],
        default="strict",
        help="CRC verification mode: strict (fail on mismatch), warn (warn but continue), skip (skip verification)",
    )
    p_repair.add_argument(
        "--format",
        choices=["zip", "tar", "7z", "rar"],
        help="Archive format (auto-detected if not specified)",
    )
    
    p_recover = subparsers.add_parser("recover", help="Attempt to recover data from a corrupted archive by extracting readable entries")
    p_recover.add_argument("archive", type=Path, help="Path to the corrupted archive file")
    p_recover.add_argument("output_dir", type=Path, help="Directory where recovered files will be extracted")
    p_recover.add_argument(
        "--no-skip-crc",
        action="store_true",
        help="Do not skip CRC verification (may skip entries with CRC errors)",
    )
    p_recover.add_argument(
        "--no-partial-recovery",
        action="store_true",
        help="Do not attempt partial recovery of corrupted entries",
    )
    p_recover.add_argument(
        "--password",
        type=str,
        help="Password for encrypted archives",
    )
    p_recover.add_argument(
        "--password-file",
        type=Path,
        help="File containing password for encrypted archives",
    )
    p_recover.add_argument(
        "--format",
        choices=["zip", "tar", "gzip", "bzip2", "xz", "7z", "rar"],
        help="Archive format (auto-detected if not specified)",
    )
    p_recover.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_analyze = subparsers.add_parser("analyze", help="Analyze an archive and report on supported/unsupported features")
    p_analyze.add_argument("archive", type=Path, help="Path to the archive to analyze")
    p_analyze.add_argument(
        "--format",
        choices=["zip", "tar", "gzip", "bzip2", "xz", "7z", "rar"],
        help="Archive format (auto-detected if not specified)",
    )
    
    p_analyze_compression = subparsers.add_parser("analyze-compression", help="Analyze files and suggest optimal compression methods and levels")
    p_analyze_compression.add_argument("files", type=Path, nargs="+", help="File paths to analyze")
    p_analyze_compression.add_argument(
        "--sample-size",
        type=int,
        help="Maximum number of bytes to sample from each file for testing (useful for large files)",
    )
    p_analyze_compression.add_argument(
        "--test-methods",
        nargs="+",
        choices=["deflate", "bzip2", "lzma", "stored"],
        help="Compression methods to test (default: all methods)",
    )
    p_analyze_compression.add_argument(
        "--test-levels",
        type=int,
        nargs="+",
        metavar="LEVEL",
        help="Compression levels to test (0-9, default: [1, 3, 6, 9])",
    )
    p_analyze_compression.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_health_check = subparsers.add_parser("health-check", help="Perform a quick health check on an archive without full CRC validation")
    p_health_check.add_argument("archive", type=Path, help="Path to the archive to check")
    p_health_check.add_argument(
        "--format",
        choices=["zip", "tar", "gzip", "bzip2", "xz", "7z", "rar"],
        help="Archive format (auto-detected if not specified)",
    )
    
    # sync
    p_sync = subparsers.add_parser(
        "sync",
        help="Synchronize an archive with a directory (add new files, update changed files, optionally remove deleted files)"
    )
    p_sync.add_argument("archive", type=Path, help="Path to the archive file (will be created if it doesn't exist)")
    p_sync.add_argument("source_directory", type=Path, help="Path to the source directory to sync with")
    p_sync.add_argument(
        "--compression",
        choices=["stored", "deflate", "bzip2", "lzma", "ppmd"],
        default="deflate",
        help="Compression method (default: deflate)",
    )
    p_sync.add_argument(
        "--compression-level",
        type=int,
        default=6,
        help="Compression level (0-9 for DEFLATE/LZMA, 1-9 for BZIP2, default: 6)",
    )
    p_sync.add_argument(
        "--remove-deleted",
        action="store_true",
        help="Remove entries from archive that no longer exist in source directory",
    )
    p_sync.add_argument(
        "--compare-by",
        choices=["mtime", "size", "both"],
        default="mtime",
        help="How to detect changes: 'mtime' (modification time), 'size' (file size), or 'both' (default: mtime)",
    )
    p_sync.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Don't preserve file metadata (timestamps, permissions)",
    )
    p_sync.add_argument(
        "--password",
        type=str,
        help="Password for encryption (use --password-file for secure input)",
    )
    p_sync.add_argument(
        "--password-file",
        type=Path,
        help="Path to file containing password (more secure than --password)",
    )
    p_sync.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_deduplicate = subparsers.add_parser("deduplicate", help="Remove duplicate files from an archive based on content hash")
    p_deduplicate.add_argument("archive", type=Path, help="Path to the source archive file")
    p_deduplicate.add_argument("output", type=Path, help="Path to the output archive file (will be created/overwritten)")
    p_deduplicate.add_argument(
        "--hash-algorithm",
        choices=["crc32", "sha256"],
        default="crc32",
        help="Hash algorithm for duplicate detection (default: crc32, faster but less secure; sha256 provides cryptographic hash)",
    )
    p_deduplicate.add_argument(
        "--keep-last",
        action="store_true",
        help="Keep the last occurrence of duplicates instead of the first (default: keep first)",
    )
    p_deduplicate.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, attributes) from original archive",
    )
    p_deduplicate.add_argument(
        "--compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        help="Compression method for output archive (default: preserve original compression for each entry)",
    )
    p_deduplicate.add_argument(
        "--compression-level",
        type=int,
        choices=range(10),
        metavar="[0-9]",
        help="Compression level (0-9, default: preserve original compression level)",
    )
    p_deduplicate.add_argument(
        "--password",
        type=str,
        help="Password for encrypted source archives (ZIP only)",
    )
    p_deduplicate.add_argument(
        "--password-file",
        type=Path,
        help="File containing password for encrypted source archives (ZIP only)",
    )
    p_deduplicate.add_argument(
        "--format",
        choices=["zip", "tar", "7z"],
        help="Archive format (auto-detected if not specified)",
    )
    
    p_find_duplicates = subparsers.add_parser("find-duplicates", help="Find duplicate files across multiple archives based on content hash")
    p_find_duplicates.add_argument("archives", type=Path, nargs="+", help="Paths to archive files to analyze")
    p_find_duplicates.add_argument(
        "--hash-algorithm",
        choices=["crc32", "sha256"],
        default="crc32",
        help="Hash algorithm for duplicate detection (default: crc32, faster but less secure; sha256 provides cryptographic hash)",
    )
    p_find_duplicates.add_argument(
        "--password",
        type=str,
        help="Password for encrypted archives (applied to all archives)",
    )
    p_find_duplicates.add_argument(
        "--password-file",
        type=Path,
        help="File containing password for encrypted archives",
    )
    p_find_duplicates.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_smart = subparsers.add_parser("create-smart", help="Create an archive with automatic optimal compression selection for each file")
    p_create_smart.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_smart.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_smart.add_argument(
        "--strategy",
        choices=["best_compression", "balanced", "fastest", "fastest_decompression"],
        default="best_compression",
        help="Compression strategy: best_compression (best ratio), balanced (good balance), fastest (fastest compression), fastest_decompression (fastest decompression). Default: best_compression",
    )
    p_create_smart.add_argument(
        "--sample-size",
        type=int,
        help="Maximum number of bytes to sample from each file for analysis (useful for large files, None = analyze entire file)",
    )
    p_create_smart.add_argument(
        "--test-methods",
        nargs="+",
        choices=["deflate", "bzip2", "lzma", "stored"],
        help="Compression methods to test (default: all methods)",
    )
    p_create_smart.add_argument(
        "--test-levels",
        type=int,
        nargs="+",
        metavar="LEVEL",
        help="Compression levels to test (0-9, default: [1, 3, 6, 9])",
    )
    p_create_smart.add_argument(
        "--password",
        type=str,
        help="Password for encryption (ZIP only)",
    )
    p_create_smart.add_argument(
        "--password-file",
        type=Path,
        help="File containing password for encryption (ZIP only)",
    )
    p_create_smart.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_smart.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_preset = subparsers.add_parser("create-preset", help="Create an archive with file-type-based compression presets")
    p_create_preset.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_preset.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_preset.add_argument(
        "--preset",
        choices=["balanced", "maximum", "fast"],
        default="balanced",
        help="Compression preset: balanced (good balance, default), maximum (best compression, slower), fast (fast compression, less compression)",
    )
    p_create_preset.add_argument(
        "--password",
        type=str,
        help="Password for encryption (ZIP only)",
    )
    p_create_preset.add_argument(
        "--password-file",
        type=Path,
        help="File containing password for encryption (ZIP only)",
    )
    p_create_preset.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_preset.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_clean = subparsers.add_parser("create-clean", help="Create an archive with automatic exclusion of common temporary/cache files and preset compression")
    p_create_clean.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_clean.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_clean.add_argument(
        "--preset",
        choices=["balanced", "maximum", "fast"],
        default="balanced",
        help="Compression preset: balanced (good balance, default), maximum (best compression, slower), fast (fast compression, less compression)",
    )
    p_create_clean.add_argument(
        "--no-exclude-temp",
        action="store_true",
        help="Do not exclude common temporary/cache files",
    )
    p_create_clean.add_argument(
        "--exclude-patterns",
        nargs="+",
        help="Additional glob patterns to exclude (e.g., '*.test', 'test_*')",
    )
    p_create_clean.add_argument(
        "--exclude-extensions",
        nargs="+",
        help="Additional file extensions to exclude (e.g., '.test', '.tmp')",
    )
    p_create_clean.add_argument(
        "--password",
        type=str,
        help="Password for encryption (ZIP only)",
    )
    p_create_clean.add_argument(
        "--password-file",
        type=Path,
        help="File containing password for encryption (ZIP only)",
    )
    p_create_clean.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_clean.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_dedup = subparsers.add_parser("create-dedup", help="Create an archive with automatic deduplication during creation")
    p_create_dedup.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_dedup.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_dedup.add_argument(
        "--hash-algorithm",
        choices=["crc32", "sha256"],
        default="crc32",
        help="Hash algorithm for duplicate detection (default: crc32, faster; sha256 provides cryptographic hash)",
    )
    p_create_dedup.add_argument(
        "--keep-last",
        action="store_true",
        help="Keep the last occurrence of duplicates instead of the first (default: keep first)",
    )
    p_create_dedup.add_argument(
        "--compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        help="Uniform compression method for all files (cannot be used with --preset)",
    )
    p_create_dedup.add_argument(
        "--compression-level",
        type=int,
        choices=range(10),
        metavar="[0-9]",
        help="Compression level (0-9, default: 6)",
    )
    p_create_dedup.add_argument(
        "--preset",
        choices=["balanced", "maximum", "fast"],
        help="Compression preset: balanced (good balance), maximum (best compression), fast (fast compression). Cannot be used with --compression",
    )
    p_create_dedup.add_argument(
        "--password",
        type=str,
        help="Password for encryption (ZIP only)",
    )
    p_create_dedup.add_argument(
        "--password-file",
        type=Path,
        help="File containing password for encryption (ZIP only)",
    )
    p_create_dedup.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_dedup.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_size_based = subparsers.add_parser("create-size-based", help="Create an archive with automatic compression level selection based on file size")
    p_create_size_based.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_size_based.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_size_based.add_argument(
        "--compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Compression method (default: deflate)",
    )
    p_create_size_based.add_argument(
        "--small-threshold",
        type=int,
        default=1024 * 1024,
        help="Size threshold in bytes for small files (default: 1 MB)",
    )
    p_create_size_based.add_argument(
        "--medium-threshold",
        type=int,
        default=10 * 1024 * 1024,
        help="Size threshold in bytes for medium files (default: 10 MB)",
    )
    p_create_size_based.add_argument(
        "--small-level",
        type=int,
        choices=range(10),
        default=3,
        metavar="[0-9]",
        help="Compression level for small files (default: 3)",
    )
    p_create_size_based.add_argument(
        "--medium-level",
        type=int,
        choices=range(10),
        default=6,
        metavar="[0-9]",
        help="Compression level for medium files (default: 6)",
    )
    p_create_size_based.add_argument(
        "--large-level",
        type=int,
        choices=range(10),
        default=9,
        metavar="[0-9]",
        help="Compression level for large files (default: 9)",
    )
    p_create_size_based.add_argument(
        "--password",
        type=str,
        help="Password for encryption (ZIP only)",
    )
    p_create_size_based.add_argument(
        "--password-file",
        type=Path,
        help="File containing password for encryption (ZIP only)",
    )
    p_create_size_based.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_size_based.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_backup = subparsers.add_parser("backup", help="Create a timestamped backup archive with automatic versioning")
    p_backup.add_argument("base_archive", type=Path, help="Base path for the archive (timestamp will be inserted before extension, e.g., backup.zip -> backup_2025-12-03_14-12-21.zip)")
    p_backup.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_backup.add_argument(
        "--timestamp-format",
        type=str,
        default="%Y-%m-%d_%H-%M-%S",
        help="strftime format string for timestamp (default: %%Y-%%m-%%d_%%H-%%M-%%S)",
    )
    p_backup.add_argument(
        "--include-timezone",
        action="store_true",
        help="Include timezone info in timestamp",
    )
    p_backup.add_argument(
        "--max-backups",
        type=int,
        help="Maximum number of backups to keep (removes oldest if exceeded)",
    )
    p_backup.add_argument(
        "--compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Compression method (default: deflate)",
    )
    p_backup.add_argument(
        "--compression-level",
        type=int,
        choices=range(10),
        default=6,
        metavar="[0-9]",
        help="Compression level (0-9, default: 6)",
    )
    p_backup.add_argument(
        "--password",
        type=str,
        help="Password for encryption (ZIP only)",
    )
    p_backup.add_argument(
        "--password-file",
        type=Path,
        help="File containing password for encryption (ZIP only)",
    )
    p_backup.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_backup.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_content_based = subparsers.add_parser("create-content-based", help="Create an archive with content-based file type detection and preset compression")
    p_create_content_based.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_content_based.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_content_based.add_argument(
        "--preset",
        choices=["balanced", "maximum", "fast"],
        default="balanced",
        help="Compression preset: balanced (good balance, default), maximum (best compression, slower), fast (fast compression, less compression)",
    )
    p_create_content_based.add_argument(
        "--password",
        type=str,
        help="Password for encryption (ZIP only)",
    )
    p_create_content_based.add_argument(
        "--password-file",
        type=Path,
        help="File containing password for encryption (ZIP only)",
    )
    p_create_content_based.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_content_based.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_incremental = subparsers.add_parser("create-incremental", help="Create an incremental archive containing only files changed since a reference archive")
    p_create_incremental.add_argument("archive", type=Path, help="Path where the incremental archive will be created")
    p_create_incremental.add_argument("files", type=Path, nargs="+", help="File or directory paths to check for changes")
    p_create_incremental.add_argument("reference", type=Path, help="Path to the reference archive to compare against")
    p_create_incremental.add_argument(
        "--compare-by",
        choices=["mtime", "size", "both", "hash"],
        default="mtime",
        help="How to detect changes: mtime (modification time, default), size (file size), both (mtime and size), hash (content hash)",
    )
    p_create_incremental.add_argument(
        "--compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Compression method (default: deflate)",
    )
    p_create_incremental.add_argument(
        "--compression-level",
        type=int,
        choices=range(10),
        default=6,
        metavar="[0-9]",
        help="Compression level (0-9, default: 6)",
    )
    p_create_incremental.add_argument(
        "--password",
        type=str,
        help="Password for encryption (ZIP only)",
    )
    p_create_incremental.add_argument(
        "--password-file",
        type=Path,
        help="File containing password for encryption (ZIP only)",
    )
    p_create_incremental.add_argument(
        "--reference-password",
        type=str,
        help="Password for reference archive (ZIP only)",
    )
    p_create_incremental.add_argument(
        "--reference-password-file",
        type=Path,
        help="File containing password for reference archive (ZIP only)",
    )
    p_create_incremental.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_incremental.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_recent = subparsers.add_parser("create-recent", help="Create an archive containing only files modified within a specified time period")
    p_create_recent.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_recent.add_argument("files", type=Path, nargs="+", help="File or directory paths to check")
    p_create_recent.add_argument(
        "--hours",
        type=int,
        help="Include files modified within the last N hours (cannot be used with --days)",
    )
    p_create_recent.add_argument(
        "--days",
        type=int,
        help="Include files modified within the last N days (cannot be used with --hours)",
    )
    p_create_recent.add_argument(
        "--compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Compression method (default: deflate)",
    )
    p_create_recent.add_argument(
        "--compression-level",
        type=int,
        choices=range(10),
        default=6,
        metavar="[0-9]",
        help="Compression level (0-9, default: 6)",
    )
    p_create_recent.add_argument(
        "--password",
        type=str,
        help="Password for encryption (ZIP only)",
    )
    p_create_recent.add_argument(
        "--password-file",
        type=Path,
        help="File containing password for encryption (ZIP only)",
    )
    p_create_recent.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_recent.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_organize = subparsers.add_parser("create-organize", help="Create an archive with automatic file organization into subdirectories")
    p_create_organize.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_organize.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_organize.add_argument(
        "--organize-by",
        choices=["type", "date", "size", "type_date", "type_size"],
        default="type",
        help="Organization method: type (by file type), date (by modification date), size (by file size), type_date (by type and date), type_size (by type and size)",
    )
    p_create_organize.add_argument(
        "--preserve-original-structure",
        action="store_true",
        help="Preserve original directory structure in addition to organization",
    )
    p_create_organize.add_argument(
        "--compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Compression method (default: deflate)",
    )
    p_create_organize.add_argument(
        "--compression-level",
        type=int,
        choices=range(10),
        default=6,
        metavar="[0-9]",
        help="Compression level (0-9, default: 6)",
    )
    p_create_organize.add_argument(
        "--password",
        type=str,
        help="Password for encryption (ZIP only)",
    )
    p_create_organize.add_argument(
        "--password-file",
        type=Path,
        help="File containing password for encryption (ZIP only)",
    )
    p_create_organize.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_organize.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_analyze_files = subparsers.add_parser("analyze-files", help="Analyze files before archiving and provide a detailed report")
    p_analyze_files.add_argument("files", type=Path, nargs="+", help="File or directory paths to analyze")
    p_analyze_files.add_argument(
        "--sample-size",
        type=int,
        help="Maximum number of bytes to sample from each file for content analysis (useful for large files)",
    )
    p_analyze_files.add_argument(
        "--no-content-analysis",
        action="store_true",
        help="Skip content-based file type detection (faster, less accurate)",
    )
    p_analyze_files.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress detailed output (show summary only)",
    )
    
    p_create_embedded_metadata = subparsers.add_parser("create-embedded-metadata", help="Create an archive with embedded metadata files (manifest, checksums, creation info)")
    p_create_embedded_metadata.add_argument("archive", type=Path, help="Path to the archive file to create")
    p_create_embedded_metadata.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_embedded_metadata.add_argument(
        "--metadata-format",
        choices=["json", "csv", "text"],
        default="json",
        help="Format for metadata files (default: json)",
    )
    p_create_embedded_metadata.add_argument(
        "--no-manifest",
        action="store_true",
        help="Do not include manifest file",
    )
    p_create_embedded_metadata.add_argument(
        "--no-checksums",
        action="store_true",
        help="Do not include checksums file",
    )
    p_create_embedded_metadata.add_argument(
        "--no-creation-info",
        action="store_true",
        help="Do not include creation info file",
    )
    p_create_embedded_metadata.add_argument(
        "--metadata-prefix",
        type=str,
        default=".dnzip",
        help="Prefix directory for metadata files (default: .dnzip)",
    )
    p_create_embedded_metadata.add_argument(
        "--compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        help="Uniform compression method for all files",
    )
    p_create_embedded_metadata.add_argument(
        "--compression-level",
        type=int,
        choices=range(0, 10),
        metavar="0-9",
        help="Compression level (0-9)",
    )
    p_create_embedded_metadata.add_argument(
        "--preset",
        choices=["balanced", "maximum", "fast"],
        help="Compression preset (overrides --compression)",
    )
    p_create_embedded_metadata.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_embedded_metadata.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_embedded_metadata.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_embedded_metadata.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_embedded_metadata.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_filter = subparsers.add_parser("create-filter", help="Create an archive with advanced filtering criteria applied during creation")
    p_create_filter.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_filter.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_filter.add_argument(
        "--include-patterns",
        nargs="+",
        help="Glob patterns to include (e.g., '*.txt', 'data/*')",
    )
    p_create_filter.add_argument(
        "--exclude-patterns",
        nargs="+",
        help="Glob patterns to exclude (e.g., '*.tmp', 'temp/*')",
    )
    p_create_filter.add_argument(
        "--include-regex",
        nargs="+",
        help="Regular expression patterns to include (e.g., '^test.*\\.py$')",
    )
    p_create_filter.add_argument(
        "--exclude-regex",
        nargs="+",
        help="Regular expression patterns to exclude (e.g., '.*\\.tmp$')",
    )
    p_create_filter.add_argument(
        "--include-extensions",
        nargs="+",
        help="File extensions to include (e.g., '.txt', '.py')",
    )
    p_create_filter.add_argument(
        "--exclude-extensions",
        nargs="+",
        help="File extensions to exclude (e.g., '.tmp', '.bak')",
    )
    p_create_filter.add_argument(
        "--min-size",
        type=int,
        help="Minimum file size in bytes (inclusive)",
    )
    p_create_filter.add_argument(
        "--max-size",
        type=int,
        help="Maximum file size in bytes (inclusive)",
    )
    p_create_filter.add_argument(
        "--start-date",
        type=str,
        help="Start date for modification time filter (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)",
    )
    p_create_filter.add_argument(
        "--end-date",
        type=str,
        help="End date for modification time filter (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)",
    )
    p_create_filter.add_argument(
        "--case-sensitive",
        action="store_true",
        help="Case-sensitive pattern matching",
    )
    p_create_filter.add_argument(
        "--compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        help="Uniform compression method for all files",
    )
    p_create_filter.add_argument(
        "--compression-level",
        type=int,
        choices=range(0, 10),
        metavar="0-9",
        help="Compression level (0-9)",
    )
    p_create_filter.add_argument(
        "--preset",
        choices=["balanced", "maximum", "fast"],
        help="Compression preset (overrides --compression)",
    )
    p_create_filter.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_filter.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_filter.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_filter.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_filter.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_verify = subparsers.add_parser("create-verify", help="Create an archive with automatic integrity verification during creation")
    p_create_verify.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_verify.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_verify.add_argument(
        "--no-verify-crc",
        action="store_true",
        help="Do not verify CRC32 checksum",
    )
    p_create_verify.add_argument(
        "--no-verify-size",
        action="store_true",
        help="Do not verify file size",
    )
    p_create_verify.add_argument(
        "--no-verify-decompression",
        action="store_true",
        help="Do not verify decompression (data comparison)",
    )
    p_create_verify.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop on first verification failure",
    )
    p_create_verify.add_argument(
        "--compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        help="Uniform compression method for all files",
    )
    p_create_verify.add_argument(
        "--compression-level",
        type=int,
        choices=range(0, 10),
        metavar="0-9",
        help="Compression level (0-9)",
    )
    p_create_verify.add_argument(
        "--preset",
        choices=["balanced", "maximum", "fast"],
        help="Compression preset (overrides --compression)",
    )
    p_create_verify.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_verify.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_verify.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_verify.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_verify.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_optimize = subparsers.add_parser("create-optimize", help="Create an archive with automatic compression optimization through iterative refinement")
    p_create_optimize.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_optimize.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_optimize.add_argument(
        "--optimization-mode",
        choices=["best_compression", "target_ratio", "balanced", "size_reduction"],
        default="best_compression",
        help="Optimization mode (default: best_compression)",
    )
    p_create_optimize.add_argument(
        "--target-ratio",
        type=float,
        help="Target compression ratio (0.0-1.0, required for target_ratio mode)",
    )
    p_create_optimize.add_argument(
        "--max-iterations",
        type=int,
        default=3,
        help="Maximum number of optimization iterations (default: 3)",
    )
    p_create_optimize.add_argument(
        "--test-methods",
        nargs="+",
        choices=["stored", "deflate", "bzip2", "lzma"],
        help="Compression methods to test during optimization (default: all methods)",
    )
    p_create_optimize.add_argument(
        "--test-levels",
        type=int,
        nargs="+",
        choices=range(10),
        metavar="0-9",
        help="Compression levels to test (default: [1, 3, 6, 9])",
    )
    p_create_optimize.add_argument(
        "--compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        help="Initial compression method for all files",
    )
    p_create_optimize.add_argument(
        "--compression-level",
        type=int,
        choices=range(0, 10),
        metavar="0-9",
        help="Initial compression level (0-9)",
    )
    p_create_optimize.add_argument(
        "--preset",
        choices=["balanced", "maximum", "fast"],
        help="Initial compression preset (overrides --compression)",
    )
    p_create_optimize.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_optimize.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_optimize.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_optimize.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_optimize.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_parallel = subparsers.add_parser("create-parallel", help="Create an archive with automatic parallel compression optimization")
    p_create_parallel.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_parallel.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_parallel.add_argument(
        "--no-auto-threads",
        action="store_true",
        help="Disable automatic thread count optimization (use --max-threads instead)",
    )
    p_create_parallel.add_argument(
        "--max-threads",
        type=int,
        help="Maximum number of threads to use (default: number of CPU cores). If --no-auto-threads, this is the exact thread count.",
    )
    p_create_parallel.add_argument(
        "--min-files-for-parallel",
        type=int,
        default=4,
        help="Minimum number of files required to use parallel compression (default: 4)",
    )
    p_create_parallel.add_argument(
        "--compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        help="Compression method for all files",
    )
    p_create_parallel.add_argument(
        "--compression-level",
        type=int,
        choices=range(0, 10),
        metavar="0-9",
        help="Compression level (0-9)",
    )
    p_create_parallel.add_argument(
        "--preset",
        choices=["balanced", "maximum", "fast"],
        help="Compression preset (overrides --compression)",
    )
    p_create_parallel.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_parallel.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_parallel.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_parallel.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_parallel.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_redundant = subparsers.add_parser("create-redundant", help="Create an archive with automatic redundancy for data protection")
    p_create_redundant.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_redundant.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_redundant.add_argument(
        "--redundancy-mode",
        choices=["copies", "checksums", "both"],
        default="copies",
        help="Redundancy mode: copies (multiple copies), checksums (embedded checksums), both (copies with checksums, default: copies)",
    )
    p_create_redundant.add_argument(
        "--num-copies",
        type=int,
        default=2,
        help="Number of copies to create when redundancy_mode is 'copies' or 'both' (default: 2)",
    )
    p_create_redundant.add_argument(
        "--redundancy-location",
        type=Path,
        help="Directory for storing redundant copies (default: same directory as archive)",
    )
    p_create_redundant.add_argument(
        "--no-checksums",
        action="store_true",
        help="Do not include embedded checksums when mode is 'checksums' or 'both'",
    )
    p_create_redundant.add_argument(
        "--checksum-algorithm",
        choices=["md5", "sha1", "sha256", "sha512"],
        default="sha256",
        help="Checksum algorithm for embedded checksums (default: sha256)",
    )
    p_create_redundant.add_argument(
        "--compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        help="Compression method for all files",
    )
    p_create_redundant.add_argument(
        "--compression-level",
        type=int,
        choices=range(0, 10),
        metavar="0-9",
        help="Compression level (0-9)",
    )
    p_create_redundant.add_argument(
        "--preset",
        choices=["balanced", "maximum", "fast"],
        help="Compression preset (overrides --compression)",
    )
    p_create_redundant.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_redundant.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_redundant.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_redundant.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_redundant.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_retry = subparsers.add_parser("create-retry", help="Create an archive with automatic retry and resume capabilities for interrupted operations")
    p_create_retry.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_retry.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_retry.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Maximum number of retry attempts for failed operations (default: 3)",
    )
    p_create_retry.add_argument(
        "--retry-delay",
        type=float,
        default=1.0,
        help="Delay in seconds between retry attempts (default: 1.0)",
    )
    p_create_retry.add_argument(
        "--no-resume",
        action="store_true",
        help="Disable resume capability (start fresh even if archive exists)",
    )
    p_create_retry.add_argument(
        "--compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        help="Compression method for all files",
    )
    p_create_retry.add_argument(
        "--compression-level",
        type=int,
        choices=range(0, 10),
        metavar="0-9",
        help="Compression level (0-9)",
    )
    p_create_retry.add_argument(
        "--preset",
        choices=["balanced", "maximum", "fast"],
        help="Compression preset (overrides --compression)",
    )
    p_create_retry.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_retry.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_retry.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_retry.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_retry.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_auto_format = subparsers.add_parser("create-auto-format", help="Create an archive with automatic format selection based on file characteristics")
    p_create_auto_format.add_argument("archive", type=Path, help="Path where the archive will be created (extension may be changed to match selected format)")
    p_create_auto_format.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_auto_format.add_argument(
        "--format-selection-strategy",
        choices=["best_compression", "compatibility", "metadata", "speed"],
        default="best_compression",
        help="Strategy for format selection: best_compression (best compression, default), compatibility (best compatibility), metadata (best metadata preservation), speed (fastest creation)",
    )
    p_create_auto_format.add_argument(
        "--no-prefer-zip",
        action="store_true",
        help="Do not prefer ZIP format when multiple formats are suitable",
    )
    p_create_auto_format.add_argument(
        "--prefer-tar",
        action="store_true",
        help="Prefer TAR format when multiple formats are suitable",
    )
    p_create_auto_format.add_argument(
        "--prefer-7z",
        action="store_true",
        help="Prefer 7Z format when multiple formats are suitable",
    )
    p_create_auto_format.add_argument(
        "--compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        help="Compression method for all files (format-specific)",
    )
    p_create_auto_format.add_argument(
        "--compression-level",
        type=int,
        choices=range(0, 10),
        metavar="0-9",
        help="Compression level (0-9)",
    )
    p_create_auto_format.add_argument(
        "--preset",
        choices=["balanced", "maximum", "fast"],
        help="Compression preset (overrides --compression, ZIP only)",
    )
    p_create_auto_format.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_auto_format.add_argument(
        "--password",
        type=str,
        help="Password for encryption (ZIP only)",
    )
    p_create_auto_format.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1, ZIP only)",
    )
    p_create_auto_format.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_auto_format.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_entropy = subparsers.add_parser("create-entropy", help="Create an archive with automatic compression selection based on file entropy analysis")
    p_create_entropy.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_entropy.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_entropy.add_argument(
        "--entropy-threshold",
        type=float,
        default=7.5,
        help="Entropy threshold for compression decision (0.0-8.0, default: 7.5). Files with entropy >= threshold are stored uncompressed",
    )
    p_create_entropy.add_argument(
        "--sample-size",
        type=int,
        default=8192,
        help="Maximum bytes to sample from each file for entropy analysis (default: 8192). Use 0 to analyze entire file",
    )
    p_create_entropy.add_argument(
        "--compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Compression method for compressible files (default: deflate)",
    )
    p_create_entropy.add_argument(
        "--compression-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Compression level for compressible files (0-9, default: 6)",
    )
    p_create_entropy.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_entropy.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_entropy.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_entropy.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_entropy.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_pattern = subparsers.add_parser("create-pattern", help="Create an archive with automatic compression selection based on file data patterns")
    p_create_pattern.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_pattern.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_pattern.add_argument(
        "--pattern-analysis-size",
        type=int,
        default=16384,
        help="Maximum bytes to analyze from each file for pattern detection (default: 16384). Use 0 to analyze entire file",
    )
    p_create_pattern.add_argument(
        "--compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Default compression method for files without strong pattern match (default: deflate)",
    )
    p_create_pattern.add_argument(
        "--compression-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Compression level (0-9, default: 6)",
    )
    p_create_pattern.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_pattern.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_pattern.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_pattern.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_pattern.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_time_based = subparsers.add_parser("create-time-based", help="Create an archive with automatic compression selection based on file modification time")
    p_create_time_based.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_time_based.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_time_based.add_argument(
        "--recent-threshold-days",
        type=int,
        default=7,
        help="Number of days to consider a file 'recent' (default: 7). Files modified within this period use recent compression",
    )
    p_create_time_based.add_argument(
        "--old-threshold-days",
        type=int,
        default=90,
        help="Number of days to consider a file 'old' (default: 90). Files modified before this period use old compression",
    )
    p_create_time_based.add_argument(
        "--recent-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Compression method for recent files (default: deflate)",
    )
    p_create_time_based.add_argument(
        "--recent-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Compression level for recent files (0-9, default: 6)",
    )
    p_create_time_based.add_argument(
        "--old-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="lzma",
        help="Compression method for old files (default: lzma)",
    )
    p_create_time_based.add_argument(
        "--old-level",
        type=int,
        choices=range(0, 10),
        default=9,
        metavar="0-9",
        help="Compression level for old files (0-9, default: 9)",
    )
    p_create_time_based.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_time_based.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_time_based.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_time_based.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_time_based.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_creation_based = subparsers.add_parser("create-creation-based", help="Create an archive with automatic compression selection based on file creation time")
    p_create_creation_based.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_creation_based.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_creation_based.add_argument(
        "--recent-threshold-days",
        type=int,
        default=7,
        help="Number of days to consider a file 'recently created' (default: 7). Files created within this period use recent compression",
    )
    p_create_creation_based.add_argument(
        "--old-threshold-days",
        type=int,
        default=90,
        help="Number of days to consider a file 'old' (default: 90). Files created before this period use old compression",
    )
    p_create_creation_based.add_argument(
        "--recent-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Compression method for recently created files (default: deflate)",
    )
    p_create_creation_based.add_argument(
        "--recent-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Compression level for recently created files (0-9, default: 6)",
    )
    p_create_creation_based.add_argument(
        "--old-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="lzma",
        help="Compression method for old files (default: lzma)",
    )
    p_create_creation_based.add_argument(
        "--old-level",
        type=int,
        choices=range(0, 10),
        default=9,
        metavar="0-9",
        help="Compression level for old files (0-9, default: 9)",
    )
    p_create_creation_based.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_creation_based.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_creation_based.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_creation_based.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_creation_based.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_permission_based = subparsers.add_parser("create-permission-based", help="Create an archive with automatic compression selection based on file permissions")
    p_create_permission_based.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_permission_based.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_permission_based.add_argument(
        "--permission-rules",
        type=str,
        help="JSON string mapping permission patterns to compression settings. Example: '{\"executable\":{\"compression\":\"stored\",\"level\":0},\"readonly\":{\"compression\":\"lzma\",\"level\":9}}'",
    )
    p_create_permission_based.add_argument(
        "--default-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Default compression method for files not matching any rule (default: deflate)",
    )
    p_create_permission_based.add_argument(
        "--default-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Default compression level for files not matching any rule (0-9, default: 6)",
    )
    p_create_permission_based.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_permission_based.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_permission_based.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_permission_based.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_permission_based.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_owner_based = subparsers.add_parser("create-owner-based", help="Create an archive with automatic compression selection based on file owner and group")
    p_create_owner_based.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_owner_based.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_owner_based.add_argument(
        "--owner-rules",
        type=str,
        help="JSON string mapping owner identifiers (username or UID) to compression settings. Example: '{\"root\":{\"compression\":\"lzma\",\"level\":9},\"0\":{\"compression\":\"lzma\",\"level\":9}}'",
    )
    p_create_owner_based.add_argument(
        "--group-rules",
        type=str,
        help="JSON string mapping group identifiers (groupname or GID) to compression settings. Example: '{\"admin\":{\"compression\":\"lzma\",\"level\":9},\"users\":{\"compression\":\"deflate\",\"level\":6}}'",
    )
    p_create_owner_based.add_argument(
        "--default-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Default compression method for files not matching any rule (default: deflate)",
    )
    p_create_owner_based.add_argument(
        "--default-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Default compression level for files not matching any rule (0-9, default: 6)",
    )
    p_create_owner_based.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_owner_based.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_owner_based.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_owner_based.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_owner_based.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_path_based = subparsers.add_parser("create-path-based", help="Create an archive with automatic compression selection based on file path patterns")
    p_create_path_based.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_path_based.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_path_based.add_argument(
        "--path-patterns",
        type=str,
        help="JSON string mapping path patterns (glob-style) to compression settings. Example: '{\"*.log\":{\"compression\":\"stored\",\"level\":0},\"**/data/**\":{\"compression\":\"lzma\",\"level\":9}}'",
    )
    p_create_path_based.add_argument(
        "--default-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Default compression method for files not matching any pattern (default: deflate)",
    )
    p_create_path_based.add_argument(
        "--default-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Default compression level for files not matching any pattern (0-9, default: 6)",
    )
    p_create_path_based.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_path_based.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_path_based.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_path_based.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_path_based.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_extension_based = subparsers.add_parser("create-extension-based", help="Create an archive with automatic compression selection based on file extensions")
    p_create_extension_based.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_extension_based.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_extension_based.add_argument(
        "--extension-rules",
        type=str,
        help="JSON string mapping file extensions to compression settings. Example: '{\".txt\":{\"compression\":\"deflate\",\"level\":9},\".jpg\":{\"compression\":\"stored\",\"level\":0}}'",
    )
    p_create_extension_based.add_argument(
        "--default-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Default compression method for files not matching any rule (default: deflate)",
    )
    p_create_extension_based.add_argument(
        "--default-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Default compression level for files not matching any rule (0-9, default: 6)",
    )
    p_create_extension_based.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_extension_based.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_extension_based.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_extension_based.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_extension_based.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_mime_based = subparsers.add_parser("create-mime-based", help="Create an archive with automatic compression selection based on MIME type detection")
    p_create_mime_based.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_mime_based.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_mime_based.add_argument(
        "--mime-rules",
        type=str,
        help="JSON string mapping MIME types/patterns to compression settings. Example: '{\"text/*\":{\"compression\":\"deflate\",\"level\":9},\"image/*\":{\"compression\":\"stored\",\"level\":0}}'",
    )
    p_create_mime_based.add_argument(
        "--default-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Default compression method for files not matching any rule (default: deflate)",
    )
    p_create_mime_based.add_argument(
        "--default-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Default compression level for files not matching any rule (0-9, default: 6)",
    )
    p_create_mime_based.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_mime_based.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_mime_based.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_mime_based.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_mime_based.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_hybrid = subparsers.add_parser("create-hybrid", help="Create an archive with automatic compression selection using hybrid multi-strategy approach")
    p_create_hybrid.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_hybrid.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_hybrid.add_argument(
        "--strategies",
        type=str,
        help="Comma-separated list of strategy names to use (default: 'size,type,entropy'). Available: size, type, entropy, age, access, path, extension",
    )
    p_create_hybrid.add_argument(
        "--strategy-weights",
        type=str,
        help="JSON string mapping strategy names to weights (0.0-1.0). Example: '{\"size\":0.3,\"type\":0.4,\"entropy\":0.2,\"age\":0.1}'",
    )
    p_create_hybrid.add_argument(
        "--default-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Default compression method when strategies don't agree (default: deflate)",
    )
    p_create_hybrid.add_argument(
        "--default-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Default compression level when strategies don't agree (0-9, default: 6)",
    )
    p_create_hybrid.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_hybrid.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_hybrid.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_hybrid.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_hybrid.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_metadata_combined = subparsers.add_parser("create-metadata-combined", help="Create an archive with automatic compression selection based on combined file metadata")
    p_create_metadata_combined.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_metadata_combined.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_metadata_combined.add_argument(
        "--metadata-rules",
        type=str,
        help="JSON string with list of rule dictionaries. Each rule should contain metadata conditions and compression settings. Example: '[{\"size\":\"large\",\"type\":\"text\",\"compression\":\"lzma\",\"level\":9},{\"age\":\"old\",\"permission\":\"readonly\",\"compression\":\"lzma\",\"level\":9}]'",
    )
    p_create_metadata_combined.add_argument(
        "--default-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Default compression method for files not matching any rule (default: deflate)",
    )
    p_create_metadata_combined.add_argument(
        "--default-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Default compression level for files not matching any rule (0-9, default: 6)",
    )
    p_create_metadata_combined.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_metadata_combined.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_metadata_combined.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_metadata_combined.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_metadata_combined.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_relationship_based = subparsers.add_parser("create-relationship-based", help="Create an archive with automatic compression selection based on file relationships")
    p_create_relationship_based.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_relationship_based.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_relationship_based.add_argument(
        "--relationship-detection",
        choices=["path", "naming", "extension", "directory", "hybrid"],
        default="path",
        help="Method for detecting file relationships (default: path)",
    )
    p_create_relationship_based.add_argument(
        "--relationship-threshold",
        type=float,
        default=0.7,
        metavar="0.0-1.0",
        help="Similarity threshold for relationship detection (0.0-1.0 for 'path'/'naming', 1-3 for 'hybrid', default: 0.7)",
    )
    p_create_relationship_based.add_argument(
        "--group-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Compression method for related file groups (default: deflate)",
    )
    p_create_relationship_based.add_argument(
        "--group-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Compression level for related file groups (0-9, default: 6)",
    )
    p_create_relationship_based.add_argument(
        "--default-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Default compression method for files not in any group (default: deflate)",
    )
    p_create_relationship_based.add_argument(
        "--default-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Default compression level for files not in any group (0-9, default: 6)",
    )
    p_create_relationship_based.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_relationship_based.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_relationship_based.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_relationship_based.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_relationship_based.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_stability_based = subparsers.add_parser("create-stability-based", help="Create an archive with automatic compression selection based on file stability (modification frequency)")
    p_create_stability_based.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_stability_based.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_stability_based.add_argument(
        "--stable-threshold-ratio",
        type=float,
        default=0.8,
        metavar="0.0-1.0",
        help="Stability ratio threshold for stable files (0.0-1.0, default: 0.8). Files with ratio >= this value are considered stable",
    )
    p_create_stability_based.add_argument(
        "--unstable-threshold-ratio",
        type=float,
        default=0.2,
        metavar="0.0-1.0",
        help="Stability ratio threshold for unstable files (0.0-1.0, default: 0.2). Files with ratio <= this value are considered unstable",
    )
    p_create_stability_based.add_argument(
        "--stable-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="lzma",
        help="Compression method for stable files (default: lzma)",
    )
    p_create_stability_based.add_argument(
        "--stable-level",
        type=int,
        choices=range(0, 10),
        default=9,
        metavar="0-9",
        help="Compression level for stable files (0-9, default: 9)",
    )
    p_create_stability_based.add_argument(
        "--unstable-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Compression method for unstable files (default: deflate)",
    )
    p_create_stability_based.add_argument(
        "--unstable-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Compression level for unstable files (0-9, default: 6)",
    )
    p_create_stability_based.add_argument(
        "--default-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Default compression method for moderately stable files (default: deflate)",
    )
    p_create_stability_based.add_argument(
        "--default-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Default compression level for moderately stable files (0-9, default: 6)",
    )
    p_create_stability_based.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_stability_based.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_stability_based.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_stability_based.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_stability_based.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_priority_based = subparsers.add_parser("create-priority-based", help="Create an archive with automatic compression selection based on file priority or importance levels")
    p_create_priority_based.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_priority_based.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_priority_based.add_argument(
        "--priority-rules",
        type=str,
        help="JSON string mapping priority levels to rules with matching criteria and compression settings. Example: '{\"critical\":{\"pattern\":\"*.config\",\"compression\":\"deflate\",\"level\":9},\"low\":{\"pattern\":\"*.log\",\"compression\":\"lzma\",\"level\":9}}'",
    )
    p_create_priority_based.add_argument(
        "--default-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Default compression method for files not matching any rule (default: deflate)",
    )
    p_create_priority_based.add_argument(
        "--default-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Default compression level for files not matching any rule (0-9, default: 6)",
    )
    p_create_priority_based.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_priority_based.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_priority_based.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_priority_based.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_priority_based.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_count_based = subparsers.add_parser("create-count-based", help="Create an archive with automatic compression selection based on file count")
    p_create_count_based.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_count_based.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_count_based.add_argument(
        "--few-files-threshold",
        type=int,
        default=10,
        help="File count threshold for 'few files' category (default: 10). Archives with fewer files use few-files compression",
    )
    p_create_count_based.add_argument(
        "--many-files-threshold",
        type=int,
        default=100,
        help="File count threshold for 'many files' category (default: 100). Archives with more files use many-files compression",
    )
    p_create_count_based.add_argument(
        "--few-files-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Compression method for archives with few files (default: deflate)",
    )
    p_create_count_based.add_argument(
        "--few-files-level",
        type=int,
        choices=range(0, 10),
        default=9,
        metavar="0-9",
        help="Compression level for archives with few files (0-9, default: 9)",
    )
    p_create_count_based.add_argument(
        "--many-files-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Compression method for archives with many files (default: deflate)",
    )
    p_create_count_based.add_argument(
        "--many-files-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Compression level for archives with many files (0-9, default: 6)",
    )
    p_create_count_based.add_argument(
        "--default-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Default compression method for medium file count archives (default: deflate)",
    )
    p_create_count_based.add_argument(
        "--default-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Default compression level for medium file count archives (0-9, default: 6)",
    )
    p_create_count_based.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_count_based.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_count_based.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_count_based.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_count_based.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_total_size_based = subparsers.add_parser("create-total-size-based", help="Create an archive with automatic compression selection based on total archive size")
    p_create_total_size_based.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_total_size_based.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_total_size_based.add_argument(
        "--small-archive-threshold",
        type=int,
        default=100 * 1024 * 1024,
        help="Total size threshold in bytes for 'small archive' category (default: 100 MB). Archives with smaller total size use small-archive compression",
    )
    p_create_total_size_based.add_argument(
        "--large-archive-threshold",
        type=int,
        default=1024 * 1024 * 1024,
        help="Total size threshold in bytes for 'large archive' category (default: 1 GB). Archives with larger total size use large-archive compression",
    )
    p_create_total_size_based.add_argument(
        "--small-archive-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Compression method for small archives (default: deflate)",
    )
    p_create_total_size_based.add_argument(
        "--small-archive-level",
        type=int,
        choices=range(0, 10),
        default=9,
        metavar="0-9",
        help="Compression level for small archives (0-9, default: 9)",
    )
    p_create_total_size_based.add_argument(
        "--large-archive-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Compression method for large archives (default: deflate)",
    )
    p_create_total_size_based.add_argument(
        "--large-archive-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Compression level for large archives (0-9, default: 6)",
    )
    p_create_total_size_based.add_argument(
        "--default-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Default compression method for medium archives (default: deflate)",
    )
    p_create_total_size_based.add_argument(
        "--default-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Default compression level for medium archives (0-9, default: 6)",
    )
    p_create_total_size_based.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_total_size_based.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_total_size_based.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_total_size_based.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_total_size_based.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_efficiency_based = subparsers.add_parser("create-efficiency-based", help="Create an archive with automatic compression selection based on efficiency prediction")
    p_create_efficiency_based.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_efficiency_based.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_efficiency_based.add_argument(
        "--sample-size",
        type=int,
        default=10,
        help="Number of files to sample for testing (default: 10). Used if --sample-percent is not specified",
    )
    p_create_efficiency_based.add_argument(
        "--sample-percent",
        type=float,
        default=None,
        metavar="0.0-1.0",
        help="Percentage of files to sample for testing (0.0-1.0, default: None). Overrides --sample-size if specified",
    )
    p_create_efficiency_based.add_argument(
        "--test-methods",
        type=str,
        default=None,
        help="Comma-separated list of compression methods to test (default: deflate,bzip2,lzma). Example: 'deflate,bzip2,lzma'",
    )
    p_create_efficiency_based.add_argument(
        "--test-levels",
        type=str,
        default=None,
        help="Comma-separated list of compression levels to test (default: 1,6,9). Example: '1,6,9'",
    )
    p_create_efficiency_based.add_argument(
        "--min-sample-files",
        type=int,
        default=3,
        help="Minimum number of files to test (default: 3). Ensures meaningful prediction even with few files",
    )
    p_create_efficiency_based.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_efficiency_based.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_efficiency_based.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_efficiency_based.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_efficiency_based.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_type_distribution_based = subparsers.add_parser("create-type-distribution-based", help="Create an archive with automatic compression selection based on file type distribution")
    p_create_type_distribution_based.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_type_distribution_based.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_type_distribution_based.add_argument(
        "--dominant-threshold",
        type=float,
        default=0.5,
        metavar="0.0-1.0",
        help="Threshold percentage (0.0-1.0) for considering a type 'dominant' (default: 0.5 = 50%%). Types with percentage >= threshold are considered dominant",
    )
    p_create_type_distribution_based.add_argument(
        "--type-compression-map",
        type=str,
        default=None,
        help="JSON string mapping file types to compression settings. Example: '{\"text\":{\"compression\":\"deflate\",\"level\":9},\"image\":{\"compression\":\"stored\",\"level\":0}}'",
    )
    p_create_type_distribution_based.add_argument(
        "--default-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Default compression method for files not matching any type rule (default: deflate)",
    )
    p_create_type_distribution_based.add_argument(
        "--default-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Default compression level for files not matching any type rule (0-9, default: 6)",
    )
    p_create_type_distribution_based.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_type_distribution_based.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_type_distribution_based.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_type_distribution_based.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_type_distribution_based.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_adaptive = subparsers.add_parser("create-adaptive", help="Create an archive with adaptive compression that adjusts based on actual compression ratios")
    p_create_adaptive.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_adaptive.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_adaptive.add_argument(
        "--initial-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Initial compression method to try (default: deflate)",
    )
    p_create_adaptive.add_argument(
        "--initial-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Initial compression level to try (0-9, default: 6)",
    )
    p_create_adaptive.add_argument(
        "--adaptation-threshold",
        type=float,
        default=0.95,
        metavar="0.0-1.0",
        help="Compression ratio threshold for triggering adaptation (0.0-1.0, default: 0.95). Files with compression ratio > threshold trigger alternative testing",
    )
    p_create_adaptive.add_argument(
        "--test-methods",
        type=str,
        default=None,
        help="Comma-separated list of compression methods to test when adaptation is triggered (default: deflate,bzip2,lzma). Example: 'deflate,bzip2,lzma'",
    )
    p_create_adaptive.add_argument(
        "--test-levels",
        type=str,
        default=None,
        help="Comma-separated list of compression levels to test when adaptation is triggered (default: 6,9). Example: '6,9'",
    )
    p_create_adaptive.add_argument(
        "--adaptation-window",
        type=int,
        default=10,
        help="Number of recent files to consider for adaptation decisions (default: 10). Larger windows provide more stable adaptation but slower response",
    )
    p_create_adaptive.add_argument(
        "--min-improvement",
        type=float,
        default=0.05,
        metavar="0.0-1.0",
        help="Minimum improvement ratio required to switch compression (0.0-1.0, default: 0.05 = 5%%). Alternative compression must improve by at least this amount to be used",
    )
    p_create_adaptive.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_adaptive.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_adaptive.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_adaptive.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_adaptive.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_target_based = subparsers.add_parser("create-target-based", help="Create an archive with compression adjusted to meet target compression ratio or space savings goals")
    p_create_target_based.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_target_based.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_target_based.add_argument(
        "--target-ratio",
        type=float,
        default=None,
        metavar="0.0-1.0",
        help="Target compression ratio (0.0-1.0). Lower values = better compression. Example: 0.5 = 50%% of original size",
    )
    p_create_target_based.add_argument(
        "--target-space-saved",
        type=int,
        default=None,
        help="Target space saved in bytes. Compression adjusts to save at least this amount of space",
    )
    p_create_target_based.add_argument(
        "--target-space-saved-percent",
        type=float,
        default=None,
        metavar="0.0-100.0",
        help="Target space saved percentage (0.0-100.0). Compression adjusts to save at least this percentage of space",
    )
    p_create_target_based.add_argument(
        "--initial-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Initial compression method to try (default: deflate)",
    )
    p_create_target_based.add_argument(
        "--initial-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Initial compression level to try (0-9, default: 6)",
    )
    p_create_target_based.add_argument(
        "--test-methods",
        type=str,
        default=None,
        help="Comma-separated list of compression methods to test when target not met (default: deflate,bzip2,lzma). Example: 'deflate,bzip2,lzma'",
    )
    p_create_target_based.add_argument(
        "--test-levels",
        type=str,
        default=None,
        help="Comma-separated list of compression levels to test when target not met (default: 6,9). Example: '6,9'",
    )
    p_create_target_based.add_argument(
        "--max-iterations",
        type=int,
        default=3,
        help="Maximum number of iterations to try (default: 3). Each iteration tests better compression if target not met",
    )
    p_create_target_based.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_target_based.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_target_based.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_target_based.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_target_based.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_speed_based = subparsers.add_parser("create-speed-based", help="Create an archive with compression optimized for speed rather than compression ratio")
    p_create_speed_based.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_speed_based.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_speed_based.add_argument(
        "--speed-mode",
        choices=["fast", "balanced", "time_budget"],
        default="balanced",
        help="Speed optimization mode (default: balanced). 'fast' = maximum speed, 'balanced' = balanced speed/compression, 'time_budget' = optimize for time budget",
    )
    p_create_speed_based.add_argument(
        "--max-time-seconds",
        type=float,
        default=None,
        help="Maximum time in seconds for compression (only used with --speed-mode=time_budget). Compression adjusts to complete within this time",
    )
    p_create_speed_based.add_argument(
        "--fast-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Compression method for fast mode (default: deflate)",
    )
    p_create_speed_based.add_argument(
        "--fast-level",
        type=int,
        choices=range(0, 10),
        default=3,
        metavar="0-9",
        help="Compression level for fast mode (0-9, default: 3)",
    )
    p_create_speed_based.add_argument(
        "--balanced-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Compression method for balanced mode (default: deflate)",
    )
    p_create_speed_based.add_argument(
        "--balanced-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Compression level for balanced mode (0-9, default: 6)",
    )
    p_create_speed_based.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_speed_based.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_speed_based.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_speed_based.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_speed_based.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_quality_based = subparsers.add_parser("create-quality-based", help="Create an archive with compression optimized for quality and efficiency metrics")
    p_create_quality_based.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_quality_based.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_quality_based.add_argument(
        "--quality-mode",
        choices=["balanced", "high_quality", "efficient", "minimum_ratio"],
        default="balanced",
        help="Quality optimization mode (default: balanced). 'balanced' = balanced quality, 'high_quality' = maximum quality, 'efficient' = maximum efficiency, 'minimum_ratio' = ensure minimum ratio",
    )
    p_create_quality_based.add_argument(
        "--min-compression-ratio",
        type=float,
        default=None,
        metavar="0.0-1.0",
        help="Minimum compression ratio (0.0-1.0, only used with --quality-mode=minimum_ratio). Compression adjusts to achieve at least this ratio",
    )
    p_create_quality_based.add_argument(
        "--quality-threshold",
        type=float,
        default=0.7,
        metavar="0.0-1.0",
        help="Quality score threshold for triggering better compression (0.0-1.0, default: 0.7). Files with quality score < threshold get better compression",
    )
    p_create_quality_based.add_argument(
        "--test-methods",
        type=str,
        default=None,
        help="Comma-separated list of compression methods to test for quality optimization (default: deflate,bzip2,lzma). Example: 'deflate,bzip2,lzma'",
    )
    p_create_quality_based.add_argument(
        "--test-levels",
        type=str,
        default=None,
        help="Comma-separated list of compression levels to test (default: 6,9). Example: '6,9'",
    )
    p_create_quality_based.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_quality_based.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_quality_based.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_quality_based.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_quality_based.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_age_based = subparsers.add_parser("create-age-based", help="Create an archive with compression optimized based on file age (time since last modification)")
    p_create_age_based.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_age_based.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_age_based.add_argument(
        "--recent-age-days",
        type=int,
        default=7,
        help="Age threshold in days for 'recent files' category (default: 7). Files modified within this many days use recent compression",
    )
    p_create_age_based.add_argument(
        "--old-age-days",
        type=int,
        default=90,
        help="Age threshold in days for 'old files' category (default: 90). Files modified more than this many days ago use old compression",
    )
    p_create_age_based.add_argument(
        "--recent-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Compression method for recently modified files (default: deflate)",
    )
    p_create_age_based.add_argument(
        "--recent-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Compression level for recently modified files (0-9, default: 6)",
    )
    p_create_age_based.add_argument(
        "--old-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Compression method for old files (default: deflate)",
    )
    p_create_age_based.add_argument(
        "--old-level",
        type=int,
        choices=range(0, 10),
        default=9,
        metavar="0-9",
        help="Compression level for old files (0-9, default: 9)",
    )
    p_create_age_based.add_argument(
        "--default-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Default compression method for medium age files (default: deflate)",
    )
    p_create_age_based.add_argument(
        "--default-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Default compression level for medium age files (0-9, default: 6)",
    )
    p_create_age_based.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_age_based.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_age_based.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_age_based.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_age_based.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_size_distribution_based = subparsers.add_parser("create-size-distribution-based", help="Create an archive with compression optimized based on file size distribution")
    p_create_size_distribution_based.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_size_distribution_based.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_size_distribution_based.add_argument(
        "--small-file-threshold",
        type=int,
        default=1024 * 1024,
        help="Size threshold in bytes for small files (default: 1048576 = 1 MB). Files smaller than this are considered small",
    )
    p_create_size_distribution_based.add_argument(
        "--large-file-threshold",
        type=int,
        default=10 * 1024 * 1024,
        help="Size threshold in bytes for large files (default: 10485760 = 10 MB). Files larger than this are considered large",
    )
    p_create_size_distribution_based.add_argument(
        "--mostly-small-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Compression method for archives with mostly small files (default: deflate)",
    )
    p_create_size_distribution_based.add_argument(
        "--mostly-small-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Compression level for archives with mostly small files (0-9, default: 6)",
    )
    p_create_size_distribution_based.add_argument(
        "--mostly-large-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="lzma",
        help="Compression method for archives with mostly large files (default: lzma)",
    )
    p_create_size_distribution_based.add_argument(
        "--mostly-large-level",
        type=int,
        choices=range(0, 10),
        default=9,
        metavar="0-9",
        help="Compression level for archives with mostly large files (0-9, default: 9)",
    )
    p_create_size_distribution_based.add_argument(
        "--mixed-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Compression method for mixed size archives (default: deflate)",
    )
    p_create_size_distribution_based.add_argument(
        "--mixed-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Compression level for mixed size archives (0-9, default: 6)",
    )
    p_create_size_distribution_based.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_size_distribution_based.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_size_distribution_based.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_size_distribution_based.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_size_distribution_based.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_effectiveness_scoring_based = subparsers.add_parser("create-effectiveness-scoring-based", help="Create an archive with compression selected based on file compression effectiveness scoring")
    p_create_effectiveness_scoring_based.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_effectiveness_scoring_based.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_effectiveness_scoring_based.add_argument(
        "--high-effectiveness-threshold",
        type=float,
        default=0.6,
        metavar="0.0-1.0",
        help="Effectiveness score threshold for high effectiveness files (0.0-1.0, default: 0.6). Files with score > threshold are highly effective",
    )
    p_create_effectiveness_scoring_based.add_argument(
        "--low-effectiveness-threshold",
        type=float,
        default=0.3,
        metavar="0.0-1.0",
        help="Effectiveness score threshold for low effectiveness files (0.0-1.0, default: 0.3). Files with score < threshold are low effectiveness",
    )
    p_create_effectiveness_scoring_based.add_argument(
        "--high-effectiveness-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="lzma",
        help="Compression method for high effectiveness files (default: lzma)",
    )
    p_create_effectiveness_scoring_based.add_argument(
        "--high-effectiveness-level",
        type=int,
        choices=range(0, 10),
        default=9,
        metavar="0-9",
        help="Compression level for high effectiveness files (0-9, default: 9)",
    )
    p_create_effectiveness_scoring_based.add_argument(
        "--low-effectiveness-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="stored",
        help="Compression method for low effectiveness files (default: stored)",
    )
    p_create_effectiveness_scoring_based.add_argument(
        "--low-effectiveness-level",
        type=int,
        choices=range(0, 10),
        default=0,
        metavar="0-9",
        help="Compression level for low effectiveness files (0-9, default: 0)",
    )
    p_create_effectiveness_scoring_based.add_argument(
        "--default-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Default compression method for medium effectiveness files (default: deflate)",
    )
    p_create_effectiveness_scoring_based.add_argument(
        "--default-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Default compression level for medium effectiveness files (0-9, default: 6)",
    )
    p_create_effectiveness_scoring_based.add_argument(
        "--test-methods",
        type=str,
        default=None,
        help="Comma-separated list of compression methods to test (default: deflate,bzip2,lzma). Example: 'deflate,bzip2,lzma'",
    )
    p_create_effectiveness_scoring_based.add_argument(
        "--test-levels",
        type=str,
        default=None,
        help="Comma-separated list of compression levels to test (default: 6,9). Example: '6,9'",
    )
    p_create_effectiveness_scoring_based.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_effectiveness_scoring_based.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_effectiveness_scoring_based.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_effectiveness_scoring_based.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_effectiveness_scoring_based.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_activity_based = subparsers.add_parser("create-activity-based", help="Create an archive with compression optimized based on file activity level")
    p_create_activity_based.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_activity_based.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_activity_based.add_argument(
        "--high-activity-threshold-hours",
        type=int,
        default=24,
        help="Time threshold in hours for 'high activity' category (default: 24). Files modified within this many hours use high activity compression",
    )
    p_create_activity_based.add_argument(
        "--low-activity-threshold-days",
        type=int,
        default=30,
        help="Time threshold in days for 'low activity' category (default: 30). Files not modified for this many days use low activity compression",
    )
    p_create_activity_based.add_argument(
        "--high-activity-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Compression method for high activity files (default: deflate)",
    )
    p_create_activity_based.add_argument(
        "--high-activity-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Compression level for high activity files (0-9, default: 6)",
    )
    p_create_activity_based.add_argument(
        "--low-activity-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="lzma",
        help="Compression method for low activity files (default: lzma)",
    )
    p_create_activity_based.add_argument(
        "--low-activity-level",
        type=int,
        choices=range(0, 10),
        default=9,
        metavar="0-9",
        help="Compression level for low activity files (0-9, default: 9)",
    )
    p_create_activity_based.add_argument(
        "--medium-activity-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Compression method for medium activity files (default: deflate)",
    )
    p_create_activity_based.add_argument(
        "--medium-activity-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Compression level for medium activity files (0-9, default: 6)",
    )
    p_create_activity_based.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_activity_based.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_activity_based.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_activity_based.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_activity_based.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_performance_requirements_based = subparsers.add_parser("create-performance-requirements-based", help="Create an archive with compression selected to meet performance requirements and constraints")
    p_create_performance_requirements_based.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_performance_requirements_based.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_performance_requirements_based.add_argument(
        "--max-time-seconds",
        type=float,
        default=None,
        help="Maximum total time allowed for compression in seconds. Compression is selected to complete within this time",
    )
    p_create_performance_requirements_based.add_argument(
        "--min-compression-ratio",
        type=float,
        default=None,
        metavar="0.0-1.0",
        help="Minimum compression ratio required (0.0-1.0). Compression is selected to achieve at least this ratio",
    )
    p_create_performance_requirements_based.add_argument(
        "--max-compression-time-per-file",
        type=float,
        default=None,
        help="Maximum compression time per file in seconds. Limits compression time for individual files",
    )
    p_create_performance_requirements_based.add_argument(
        "--test-methods",
        type=str,
        default=None,
        help="Comma-separated list of compression methods to test (default: deflate,bzip2,lzma). Example: 'deflate,bzip2,lzma'",
    )
    p_create_performance_requirements_based.add_argument(
        "--test-levels",
        type=str,
        default=None,
        help="Comma-separated list of compression levels to test (default: 1,3,6,9). Example: '1,3,6,9'",
    )
    p_create_performance_requirements_based.add_argument(
        "--default-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Default compression method if no requirements specified (default: deflate)",
    )
    p_create_performance_requirements_based.add_argument(
        "--default-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Default compression level if no requirements specified (0-9, default: 6)",
    )
    p_create_performance_requirements_based.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_performance_requirements_based.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_performance_requirements_based.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_performance_requirements_based.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_performance_requirements_based.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_compressibility_based = subparsers.add_parser("create-compressibility-based", help="Create an archive with compression selected based on file compressibility")
    p_create_compressibility_based.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_compressibility_based.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_compressibility_based.add_argument(
        "--highly-compressible-threshold",
        type=float,
        default=0.5,
        metavar="0.0-1.0",
        help="Compression ratio threshold for highly compressible files (0.0-1.0, default: 0.5). Files with ratio < threshold are highly compressible",
    )
    p_create_compressibility_based.add_argument(
        "--poorly-compressible-threshold",
        type=float,
        default=0.9,
        metavar="0.0-1.0",
        help="Compression ratio threshold for poorly compressible files (0.0-1.0, default: 0.9). Files with ratio > threshold are poorly compressible",
    )
    p_create_compressibility_based.add_argument(
        "--highly-compressible-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="lzma",
        help="Compression method for highly compressible files (default: lzma)",
    )
    p_create_compressibility_based.add_argument(
        "--highly-compressible-level",
        type=int,
        choices=range(0, 10),
        default=9,
        metavar="0-9",
        help="Compression level for highly compressible files (0-9, default: 9)",
    )
    p_create_compressibility_based.add_argument(
        "--poorly-compressible-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="stored",
        help="Compression method for poorly compressible files (default: stored)",
    )
    p_create_compressibility_based.add_argument(
        "--poorly-compressible-level",
        type=int,
        choices=range(0, 10),
        default=0,
        metavar="0-9",
        help="Compression level for poorly compressible files (0-9, default: 0)",
    )
    p_create_compressibility_based.add_argument(
        "--default-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Default compression method for moderately compressible files (default: deflate)",
    )
    p_create_compressibility_based.add_argument(
        "--default-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Default compression level for moderately compressible files (0-9, default: 6)",
    )
    p_create_compressibility_based.add_argument(
        "--test-sample-size",
        type=int,
        default=5,
        help="Number of files to test for compressibility analysis (default: 5). Larger samples provide more accurate categorization",
    )
    p_create_compressibility_based.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_compressibility_based.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_compressibility_based.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_compressibility_based.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_compressibility_based.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_pattern_based = subparsers.add_parser("create-pattern-based", help="Create an archive with automatic compression method selection based on file data patterns")
    p_create_pattern_based.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_pattern_based.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_pattern_based.add_argument(
        "--pattern-analysis-size",
        type=int,
        default=16384,
        help="Maximum number of bytes to analyze from each file for pattern detection (default: 16384). Use None to analyze entire file",
    )
    p_create_pattern_based.add_argument(
        "--compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Default compression method to use when pattern doesn't strongly suggest a specific method (default: deflate)",
    )
    p_create_pattern_based.add_argument(
        "--compression-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Compression level to use (0-9, default: 6)",
    )
    p_create_pattern_based.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_pattern_based.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_pattern_based.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_pattern_based.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_pattern_based.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_similarity_based = subparsers.add_parser("create-similarity-based", help="Create an archive with automatic compression selection based on file content similarity")
    p_create_similarity_based.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_similarity_based.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_similarity_based.add_argument(
        "--similarity-threshold",
        type=float,
        default=0.8,
        metavar="0.0-1.0",
        help="Similarity threshold for grouping files (0.0-1.0, default: 0.8). Files with similarity >= threshold are grouped together",
    )
    p_create_similarity_based.add_argument(
        "--sample-size",
        type=int,
        default=4096,
        help="Maximum number of bytes to sample from each file for similarity comparison (default: 4096). Use 0 to compare entire files",
    )
    p_create_similarity_based.add_argument(
        "--group-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Compression method for similar file groups (default: deflate)",
    )
    p_create_similarity_based.add_argument(
        "--group-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Compression level for similar file groups (0-9, default: 6)",
    )
    p_create_similarity_based.add_argument(
        "--default-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Default compression method for unique files (default: deflate)",
    )
    p_create_similarity_based.add_argument(
        "--default-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Default compression level for unique files (0-9, default: 6)",
    )
    p_create_similarity_based.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_similarity_based.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_similarity_based.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_similarity_based.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_similarity_based.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_naming_based = subparsers.add_parser("create-naming-based", help="Create an archive with automatic compression selection based on file naming patterns")
    p_create_naming_based.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_naming_based.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_naming_based.add_argument(
        "--naming-patterns",
        type=str,
        help="JSON string mapping filename patterns (glob-style) to compression settings. Example: '{\"backup_*\":{\"compression\":\"stored\",\"level\":0},\"*.log\":{\"compression\":\"deflate\",\"level\":9}}'",
    )
    p_create_naming_based.add_argument(
        "--default-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Default compression method for files not matching any pattern (default: deflate)",
    )
    p_create_naming_based.add_argument(
        "--default-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Default compression level for files not matching any pattern (0-9, default: 6)",
    )
    p_create_naming_based.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_naming_based.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_naming_based.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_naming_based.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_naming_based.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_depth_based = subparsers.add_parser("create-depth-based", help="Create an archive with automatic compression selection based on directory depth")
    p_create_depth_based.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_depth_based.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_depth_based.add_argument(
        "--depth-thresholds",
        type=str,
        help="JSON array of depth thresholds for custom depth ranges. Example: '[0, 2, 5]' means depth 0, depth 1-2, depth 3-5, depth 6+",
    )
    p_create_depth_based.add_argument(
        "--depth-compressions",
        type=str,
        help="JSON array of compression settings for each depth range. Example: '[{\"compression\":\"deflate\",\"level\":3},{\"compression\":\"deflate\",\"level\":6},{\"compression\":\"lzma\",\"level\":9}]'",
    )
    p_create_depth_based.add_argument(
        "--default-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Default compression method for files beyond configured depths (default: deflate)",
    )
    p_create_depth_based.add_argument(
        "--default-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Default compression level for files beyond configured depths (0-9, default: 6)",
    )
    p_create_depth_based.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_depth_based.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_depth_based.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_depth_based.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_depth_based.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_create_access_based = subparsers.add_parser("create-access-based", help="Create an archive with automatic compression selection based on file access time")
    p_create_access_based.add_argument("archive", type=Path, help="Path where the archive will be created")
    p_create_access_based.add_argument("files", type=Path, nargs="+", help="File or directory paths to add to archive")
    p_create_access_based.add_argument(
        "--frequent-threshold-days",
        type=int,
        default=7,
        help="Number of days to consider a file 'frequently accessed' (default: 7). Files accessed within this period use frequent compression",
    )
    p_create_access_based.add_argument(
        "--rare-threshold-days",
        type=int,
        default=90,
        help="Number of days to consider a file 'rarely accessed' (default: 90). Files accessed before this period use rare compression",
    )
    p_create_access_based.add_argument(
        "--frequent-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="Compression method for frequently accessed files (default: deflate)",
    )
    p_create_access_based.add_argument(
        "--frequent-level",
        type=int,
        choices=range(0, 10),
        default=6,
        metavar="0-9",
        help="Compression level for frequently accessed files (0-9, default: 6)",
    )
    p_create_access_based.add_argument(
        "--rare-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="lzma",
        help="Compression method for rarely accessed files (default: lzma)",
    )
    p_create_access_based.add_argument(
        "--rare-level",
        type=int,
        choices=range(0, 10),
        default=9,
        metavar="0-9",
        help="Compression level for rarely accessed files (0-9, default: 9)",
    )
    p_create_access_based.add_argument(
        "--comment",
        type=str,
        help="Archive comment",
    )
    p_create_access_based.add_argument(
        "--password",
        type=str,
        help="Password for encryption",
    )
    p_create_access_based.add_argument(
        "--aes-version",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="AES version for encryption (1=AES-128, 2=AES-192, 3=AES-256, default: 1)",
    )
    p_create_access_based.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_create_access_based.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    p_batch_convert_smart = subparsers.add_parser("batch-convert-smart", help="Batch convert multiple archives with automatic optimal compression selection")
    p_batch_convert_smart.add_argument("archives", type=Path, nargs="+", help="Paths to source archive files to convert")
    p_batch_convert_smart.add_argument("output_dir", type=Path, help="Directory where converted archives will be created")
    p_batch_convert_smart.add_argument(
        "--target-format",
        choices=["zip", "tar", "7z"],
        default="zip",
        help="Target archive format (default: zip)",
    )
    p_batch_convert_smart.add_argument(
        "--strategy",
        choices=["best_compression", "balanced", "fastest", "fastest_decompression"],
        default="best_compression",
        help="Compression strategy (default: best_compression)",
    )
    p_batch_convert_smart.add_argument(
        "--sample-size",
        type=int,
        help="Maximum number of bytes to sample from each file for analysis (useful for large files)",
    )
    p_batch_convert_smart.add_argument(
        "--test-methods",
        nargs="+",
        choices=["deflate", "bzip2", "lzma", "stored"],
        help="Compression methods to test (default: all methods)",
    )
    p_batch_convert_smart.add_argument(
        "--test-levels",
        type=int,
        nargs="+",
        metavar="LEVEL",
        help="Compression levels to test (0-9, default: [1, 3, 6, 9])",
    )
    p_batch_convert_smart.add_argument(
        "--source-format",
        choices=["zip", "tar", "gzip", "bzip2", "xz", "7z", "rar"],
        help="Source format name (auto-detected if not specified)",
    )
    p_batch_convert_smart.add_argument(
        "--password",
        type=str,
        help="Password for encrypted source archives",
    )
    p_batch_convert_smart.add_argument(
        "--password-file",
        type=Path,
        help="File containing password for encrypted source archives",
    )
    p_batch_convert_smart.add_argument(
        "--use-external-tool-for-rar",
        action="store_true",
        help="Use external tools to extract compressed RAR entries",
    )
    p_batch_convert_smart.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_batch_convert_smart.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Stop processing on first error",
    )
    p_batch_convert_smart.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    # extract-extractable (extract only extractable entries)
    p_extract_extractable = subparsers.add_parser("extract-extractable", help="Create a new archive containing only extractable entries from the source archive")
    p_extract_extractable.add_argument("source", type=Path, help="Path to the source archive file")
    p_extract_extractable.add_argument("target", type=Path, help="Path to the target archive file to create")
    p_extract_extractable.add_argument(
        "--source-format",
        choices=["zip", "tar", "gzip", "bzip2", "xz", "7z", "rar"],
        help="Source archive format (auto-detected if not specified)",
    )
    p_extract_extractable.add_argument(
        "--target-format",
        choices=["zip", "tar", "gzip", "bzip2", "xz", "7z"],
        help="Target archive format (auto-detected from extension if not specified)",
    )
    p_extract_extractable.add_argument(
        "--compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        help="Compression method for target archive (for formats that support it)",
    )
    p_extract_extractable.add_argument(
        "--compression-level",
        type=int,
        choices=range(10),
        metavar="[0-9]",
        help="Compression level (0-9, format-dependent default)",
    )
    p_extract_extractable.add_argument(
        "--password",
        type=str,
        help="Password for encrypted source archives (ZIP only)",
    )
    p_extract_extractable.add_argument(
        "--password-file",
        type=Path,
        help="File containing password for encrypted source archives (ZIP only)",
    )
    p_extract_extractable.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, attributes) from original archive",
    )
    
    # normalize (normalize archive)
    p_normalize = subparsers.add_parser("normalize", help="Normalize an archive by standardizing paths, compression, and metadata")
    p_normalize.add_argument("archive", type=Path, help="Path to the source archive file")
    p_normalize.add_argument("output", type=Path, help="Path to the output normalized archive file (will be created/overwritten)")
    p_normalize.add_argument(
        "--no-normalize-paths",
        action="store_true",
        help="Do not normalize path separators (default: normalize to forward slashes)",
    )
    p_normalize.add_argument(
        "--keep-empty-dirs",
        action="store_true",
        help="Keep empty directory entries (default: remove empty directories)",
    )
    p_normalize.add_argument(
        "--standardize-compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        help="Compression method to standardize all entries to (default: preserve original compression for each entry)",
    )
    p_normalize.add_argument(
        "--compression-level",
        type=int,
        choices=range(10),
        metavar="[0-9]",
        help="Compression level (0-9) when --standardize-compression is set (default: format-specific)",
    )
    p_normalize.add_argument(
        "--no-sort",
        action="store_true",
        help="Do not sort entries alphabetically (default: sort entries)",
    )
    p_normalize.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, attributes) from original archive",
    )
    p_normalize.add_argument(
        "--password",
        type=str,
        help="Password for encrypted source archives (ZIP only)",
    )
    p_normalize.add_argument(
        "--password-file",
        type=Path,
        help="File containing password for encrypted source archives (ZIP only)",
    )
    p_normalize.add_argument(
        "--format",
        choices=["zip", "tar", "7z"],
        help="Archive format (auto-detected if not specified)",
    )
    p_normalize.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    # filter (create filtered archive)
    p_filter = subparsers.add_parser("filter", help="Create a new archive containing only entries matching filter criteria")
    p_filter.add_argument("archive", type=Path, help="Path to the source archive file")
    p_filter.add_argument("output", type=Path, help="Path to the output archive file (will be created/overwritten)")
    p_filter.add_argument(
        "--include-patterns",
        nargs="+",
        metavar="PATTERN",
        help="Glob patterns to include (e.g., '*.txt' 'data/*')",
    )
    p_filter.add_argument(
        "--exclude-patterns",
        nargs="+",
        metavar="PATTERN",
        help="Glob patterns to exclude (e.g., '*.tmp' 'temp/*')",
    )
    p_filter.add_argument(
        "--include-extensions",
        nargs="+",
        metavar="EXT",
        help="File extensions to include (with dot, e.g., '.txt' '.py')",
    )
    p_filter.add_argument(
        "--exclude-extensions",
        nargs="+",
        metavar="EXT",
        help="File extensions to exclude (with dot, e.g., '.tmp' '.bak')",
    )
    p_filter.add_argument(
        "--min-size",
        type=int,
        metavar="BYTES",
        help="Minimum file size in bytes (inclusive)",
    )
    p_filter.add_argument(
        "--max-size",
        type=int,
        metavar="BYTES",
        help="Maximum file size in bytes (inclusive)",
    )
    p_filter.add_argument(
        "--start-date",
        type=str,
        metavar="DATE",
        help="Start date for modification time filter (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)",
    )
    p_filter.add_argument(
        "--end-date",
        type=str,
        metavar="DATE",
        help="End date for modification time filter (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)",
    )
    p_filter.add_argument(
        "--case-sensitive",
        action="store_true",
        help="Use case-sensitive pattern matching",
    )
    p_filter.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, attributes) from original archive",
    )
    p_filter.add_argument(
        "--compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        help="Compression method for output archive (default: preserve original compression for each entry)",
    )
    p_filter.add_argument(
        "--compression-level",
        type=int,
        choices=range(10),
        metavar="[0-9]",
        help="Compression level (0-9, default: preserve original compression level)",
    )
    p_filter.add_argument(
        "--password",
        type=str,
        help="Password for encrypted source archives (ZIP only)",
    )
    p_filter.add_argument(
        "--password-file",
        type=Path,
        help="File containing password for encrypted source archives (ZIP only)",
    )
    p_filter.add_argument(
        "--format",
        choices=["zip", "tar", "7z"],
        help="Archive format (auto-detected if not specified)",
    )
    p_filter.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    # create-index
    p_create_index = subparsers.add_parser("create-index", help="Create a searchable index file for an archive")
    p_create_index.add_argument("archive", type=Path, help="Path to the archive to index")
    p_create_index.add_argument(
        "--index",
        type=Path,
        help="Path to the index file (default: archive.index.json)",
    )
    p_create_index.add_argument(
        "--include-content-hash",
        action="store_true",
        help="Calculate content hash for each entry (slower but enables duplicate detection)",
    )
    p_create_index.add_argument(
        "--no-metadata",
        action="store_true",
        help="Do not include full metadata (timestamps, compression info, etc.)",
    )
    p_create_index.add_argument(
        "--format",
        choices=["zip", "tar", "7z", "rar"],
        help="Archive format (auto-detected if not specified)",
    )
    p_create_index.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    # search-index
    p_search_index = subparsers.add_parser("search-index", help="Search an archive index without opening the archive")
    p_search_index.add_argument("index", type=Path, help="Path to the index file")
    p_search_index.add_argument("pattern", type=str, help="Pattern to search for (glob pattern by default, regex if --regex)")
    p_search_index.add_argument(
        "--regex",
        action="store_true",
        help="Treat pattern as a regular expression",
    )
    p_search_index.add_argument(
        "--case-sensitive",
        action="store_true",
        help="Use case-sensitive pattern matching",
    )
    p_search_index.add_argument(
        "--search-metadata",
        action="store_true",
        help="Also search in metadata fields (mtime, compression_method, etc.)",
    )
    p_search_index.add_argument(
        "--min-size",
        type=int,
        metavar="BYTES",
        help="Minimum file size in bytes (inclusive)",
    )
    p_search_index.add_argument(
        "--max-size",
        type=int,
        metavar="BYTES",
        help="Maximum file size in bytes (inclusive)",
    )
    p_search_index.add_argument(
        "--compression-method",
        type=str,
        help="Filter by compression method name (e.g., 'deflate', 'stored')",
    )
    p_search_index.add_argument(
        "--has-content-hash",
        action="store_true",
        help="Filter entries that have content hash",
    )
    p_search_index.add_argument(
        "--no-content-hash",
        action="store_true",
        help="Filter entries that don't have content hash",
    )
    
    # update-index
    p_update_index = subparsers.add_parser("update-index", help="Update an existing archive index or create a new one")
    p_update_index.add_argument("archive", type=Path, help="Path to the archive to index")
    p_update_index.add_argument(
        "--index",
        type=Path,
        help="Path to the index file (default: archive.index.json)",
    )
    p_update_index.add_argument(
        "--force",
        action="store_true",
        help="Force rebuild even if index appears up-to-date",
    )
    p_update_index.add_argument(
        "--include-content-hash",
        action="store_true",
        help="Calculate content hash for each entry",
    )
    p_update_index.add_argument(
        "--no-metadata",
        action="store_true",
        help="Do not include full metadata",
    )
    p_update_index.add_argument(
        "--format",
        choices=["zip", "tar", "7z", "rar"],
        help="Archive format (auto-detected if not specified)",
    )
    p_update_index.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    # batch-process
    p_batch_process = subparsers.add_parser("batch-process", help="Process multiple archives in batch with a specified operation")
    p_batch_process.add_argument("archives", nargs="+", type=Path, help="Paths to archives to process")
    p_batch_process.add_argument(
        "--operation",
        choices=["extract", "validate", "list", "statistics", "convert", "optimize"],
        required=True,
        help="Operation to perform on all archives",
    )
    p_batch_process.add_argument(
        "-d",
        "--output-dir",
        type=Path,
        help="Output directory for extract/convert/optimize operations (default: current directory)",
    )
    p_batch_process.add_argument(
        "--target-format",
        type=str,
        help="Target format for convert operation (e.g., 'zip', 'tar')",
    )
    p_batch_process.add_argument(
        "--compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        help="Compression method for convert/optimize operations",
    )
    p_batch_process.add_argument(
        "--compression-level",
        type=int,
        choices=range(10),
        metavar="[0-9]",
        help="Compression level (0-9) for convert/optimize operations",
    )
    p_batch_process.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Stop processing on first error (default: continue processing remaining archives)",
    )
    
    p_convert = subparsers.add_parser("convert", help="Convert an archive from one format to another")
    p_convert.add_argument("source", type=Path, help="Path to the source archive file")
    p_convert.add_argument("target", type=Path, help="Path to the target archive file to create")
    p_convert.add_argument(
        "--source-format",
        choices=["zip", "tar", "gzip", "bzip2", "xz", "7z", "rar"],
        help="Source archive format (auto-detected if not specified)",
    )
    p_convert.add_argument(
        "--target-format",
        choices=["zip", "tar", "gzip", "bzip2", "xz", "7z"],
        help="Target archive format (auto-detected from file extension if not specified)",
    )
    p_convert.add_argument(
        "--compression",
        choices=["stored", "deflate", "bzip2", "lzma", "ppmd"],
        help="Compression method for target archive (ZIP format only)",
    )
    p_convert.add_argument(
        "--compression-level",
        type=int,
        choices=range(10),
        metavar="[0-9]",
        help="Compression level (0-9, format-dependent)",
    )
    p_convert.add_argument(
        "--password",
        type=str,
        help="Password for encrypted source archives (ZIP only)",
    )
    p_convert.add_argument(
        "--password-file",
        type=Path,
        help="File containing password for encrypted source archives (ZIP only)",
    )
    p_convert.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="Do not preserve file metadata (timestamps, permissions)",
    )
    p_convert.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite target file if it already exists (default: False)",
    )
    p_convert.add_argument(
        "--use-external-tool-for-rar",
        action="store_true",
        help="Use external tools (unrar, 7z, unar) to extract compressed RAR entries when converting RAR archives (RAR source format only)",
    )
    p_convert.add_argument(
        "--external-tool",
        choices=["unrar", "7z", "unar"],
        help="Specific external tool to use for RAR extraction ('unrar', '7z', or 'unar'). Only used if --use-external-tool-for-rar is enabled. If not specified, the first available tool will be used.",
    )
    
    # create-checksum
    p_create_checksum = subparsers.add_parser("create-checksum", help="Create a checksum file (manifest) for archive entries")
    p_create_checksum.add_argument("archive", type=Path, help="Path to the archive to create checksums for")
    p_create_checksum.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Path to the checksum file to create (default: archive path + algorithm extension, e.g., archive.zip.sha256)",
    )
    p_create_checksum.add_argument(
        "--algorithm",
        choices=["md5", "sha1", "sha256", "sha512"],
        default="sha256",
        help="Hash algorithm to use (default: sha256)",
    )
    p_create_checksum.add_argument(
        "--format",
        choices=["zip", "tar", "7z", "rar"],
        help="Archive format (auto-detected if not specified)",
    )
    p_create_checksum.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    
    # verify-checksum
    p_verify_checksum = subparsers.add_parser("verify-checksum", help="Verify an archive against a checksum file")
    p_verify_checksum.add_argument("archive", type=Path, help="Path to the archive to verify")
    p_verify_checksum.add_argument("checksum_file", type=Path, help="Path to the checksum file to verify against")
    p_verify_checksum.add_argument(
        "--algorithm",
        choices=["md5", "sha1", "sha256", "sha512"],
        help="Hash algorithm used in checksum file (auto-detected from file extension or content if not specified)",
    )
    p_verify_checksum.add_argument(
        "--format",
        choices=["zip", "tar", "7z", "rar"],
        help="Archive format (auto-detected if not specified)",
    )
    p_verify_checksum.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress output",
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
        print("  python -m dnzip statistics archive.zip")
        print("  python -m dnzip search archive.zip \"*.txt\"")
        print("  python -m dnzip compare archive1.zip archive2.zip")
        print("  python -m dnzip diff archive1.zip archive2.zip")
        print("  python -m dnzip repair archive.zip --repair repaired.zip")
        print("  python -m dnzip export archive.zip metadata.json")
        print("  python -m dnzip extract archive.zip -d output_dir")
        print("  python -m dnzip create archive.zip folder")
        print("  python -m dnzip test archive.zip")
        print("  python -m dnzip gzip-compress file.txt")
        print("  python -m dnzip gzip-decompress file.txt.gz")
        print("  python -m dnzip bzip2-compress file.txt")
        print("  python -m dnzip bzip2-decompress file.txt.bz2")
        print("  python -m dnzip xz-compress file.txt")
        print("  python -m dnzip xz-decompress file.txt.xz")
        print("  python -m dnzip tar-create archive.tar folder")
        print("  python -m dnzip tar-list archive.tar")
        print("  python -m dnzip tar-extract archive.tar -d output_dir")
        print("  python -m dnzip 7z-list archive.7z")
        print("  python -m dnzip 7z-extract archive.7z -d output_dir")
        print("  python -m dnzip rar-list archive.rar")
        print("  python -m dnzip rar-extract archive.rar -d output_dir")
        print("  python -m dnzip rar-extractable archive.rar --detailed")
        print("  python -m dnzip rar-extract-external archive.rar -d ./extracted")
        print("  python -m dnzip rar-check-tools")
        print("  python -m dnzip update archive.zip entry.txt file.txt")
        print("  python -m dnzip delete archive.zip entry.txt")
        print("  python -m dnzip rename archive.zip old.txt new.txt")
        print("  python -m dnzip merge output.zip archive1.zip archive2.zip archive3.zip")
        print("  python -m dnzip split archive.zip output --max-size 100MB")
        print("  python -m dnzip convert archive.rar archive.zip")
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
    
    # Set up security audit logging if requested
    if hasattr(args, 'security_audit_log') and args.security_audit_log:
        if create_audit_logger is None:
            _print_error(
                "Security audit logging is not available. "
                "The security_audit module may not be installed.",
                exit_code=1
            )
        try:
            create_audit_logger(
                enabled=True,
                log_file=args.security_audit_log,
                log_level="INFO"
            )
        except ValueError as e:
            _print_error(
                f"Failed to initialize security audit logging: {e}",
                exit_code=1
            )

    try:
        if args.command == "list":
            _cmd_list(args.archive)
        elif args.command == "info":
            _cmd_info(args.archive, format=getattr(args, 'format', None))
        elif args.command == "properties":
            _cmd_properties(args.archive, format=getattr(args, 'format', None))
        elif args.command == "statistics":
            _cmd_statistics(args.archive)
        elif args.command == "search":
            _cmd_search(
                args.archive,
                args.pattern,
                use_regex=getattr(args, 'regex', False),
                case_sensitive=not getattr(args, 'case_insensitive', False),
                format=getattr(args, 'format', None),
            )
        elif args.command == "search-content":
            _cmd_search_content(
                args.archive,
                args.search_text,
                filename_pattern=getattr(args, 'filename_pattern', None),
                use_regex=getattr(args, 'regex', False),
                case_sensitive=not getattr(args, 'case_insensitive', False),
                text_encoding=getattr(args, 'text_encoding', 'utf-8'),
                binary_mode=getattr(args, 'binary', False),
                max_file_size=getattr(args, 'max_file_size', None),
                format=getattr(args, 'format', None),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "extract":
            quiet = getattr(args, 'quiet', False)
            allow_absolute_paths = getattr(args, 'allow_absolute_paths', False)
            max_path_length = getattr(args, 'max_path_length', None)
            password = getattr(args, "password", None)
            password_file = getattr(args, "password_file", None)
            _cmd_extract(
                args.archive,
                args.directory,
                quiet=quiet,
                allow_absolute_paths=allow_absolute_paths,
                max_path_length=max_path_length,
                password=password,
                password_file=password_file,
            )
        elif args.command == "extract-filtered":
            _cmd_extract_filtered(
                args.archive,
                args.directory,
                include_patterns=getattr(args, 'include_patterns', None),
                exclude_patterns=getattr(args, 'exclude_patterns', None),
                include_extensions=getattr(args, 'include_extensions', None),
                exclude_extensions=getattr(args, 'exclude_extensions', None),
                min_size=getattr(args, 'min_size', None),
                max_size=getattr(args, 'max_size', None),
                start_date=getattr(args, 'start_date', None),
                end_date=getattr(args, 'end_date', None),
                case_sensitive=getattr(args, 'case_sensitive', False),
                allow_absolute_paths=getattr(args, 'allow_absolute_paths', False),
                max_path_length=getattr(args, 'max_path_length', None),
                password=getattr(args, 'password', None),
                password_file=getattr(args, 'password_file', None),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "extract-with-conflict-resolution":
            _cmd_extract_with_conflict_resolution(
                args.archive,
                args.directory,
                conflict_strategy=getattr(args, 'conflict_strategy', 'rename'),
                allow_absolute_paths=getattr(args, 'allow_absolute_paths', False),
                max_path_length=getattr(args, 'max_path_length', None),
                password=getattr(args, 'password', None),
                password_file=getattr(args, 'password_file', None),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create":
            password = getattr(args, "password", None)
            password_file = getattr(args, "password_file", None)
            aes_version = getattr(args, "aes_version", 1)
            compression_level = getattr(args, 'compression_level', 6)
            quiet = getattr(args, 'quiet', False)
            split_size = getattr(args, 'split_size', None)
            threads = getattr(args, 'threads', 1)
            _cmd_create(
                args.archive,
                args.sources,
                args.compression,
                args.comment,
                compression_level,
                quiet=quiet,
                split_size=split_size,
                threads=threads,
                password=password,
                password_file=password_file,
                aes_version=aes_version,
            )
        elif args.command == "create-from-file-list":
            password = getattr(args, "password", None)
            password_file = getattr(args, "password_file", None)
            aes_version = getattr(args, "aes_version", 1)
            compression_level = getattr(args, 'compression_level', 6)
            quiet = getattr(args, 'quiet', False)
            _cmd_create_from_file_list(
                args.archive,
                args.file_list,
                base_dir=getattr(args, "base_dir", None),
                compression=args.compression,
                compression_level=compression_level,
                comment=args.comment,
                password=password,
                password_file=password_file,
                aes_version=aes_version,
                quiet=quiet,
            )
        elif args.command == "test":
            exit_code = _cmd_test(args.archive, skip_crc=getattr(args, 'skip_crc', False))
            sys.exit(exit_code)
        elif args.command == "verify":
            exit_code = _cmd_test(args.archive, skip_crc=getattr(args, 'skip_crc', False))
            sys.exit(exit_code)
        elif args.command == "gzip-compress":
            output_file = getattr(args, 'output', None)
            if output_file is None:
                output_file = Path(str(args.input_file) + ".gz")
            _cmd_gzip_compress(
                args.input_file,
                output_file,
                filename=getattr(args, 'filename', None),
                comment=getattr(args, 'comment', None),
                compression_level=getattr(args, 'compression_level', 6)
            )
        elif args.command == "gzip-decompress":
            output_file = getattr(args, 'output', None)
            _cmd_gzip_decompress(
                args.input_file,
                output_file,
                skip_crc=getattr(args, 'skip_crc', False)
            )
        elif args.command == "bzip2-compress":
            output_file = getattr(args, 'output', None)
            if output_file is None:
                output_file = Path(str(args.input_file) + ".bz2")
            _cmd_bzip2_compress(
                args.input_file,
                output_file,
                compression_level=getattr(args, 'compression_level', 9)
            )
        elif args.command == "bzip2-decompress":
            output_file = getattr(args, 'output', None)
            _cmd_bzip2_decompress(
                args.input_file,
                output_file
            )
        elif args.command == "xz-compress":
            output_file = getattr(args, 'output', None)
            if output_file is None:
                output_file = Path(str(args.input_file) + ".xz")
            _cmd_xz_compress(
                args.input_file,
                output_file,
                compression_level=getattr(args, 'compression_level', 6)
            )
        elif args.command == "xz-decompress":
            output_file = getattr(args, 'output', None)
            _cmd_xz_decompress(
                args.input_file,
                output_file
            )
        elif args.command == "tar-create":
            _cmd_tar_create(args.archive, args.sources)
        elif args.command == "tar-list":
            _cmd_tar_list(args.archive)
        elif args.command == "tar-extract":
            output_dir = getattr(args, 'output_dir', Path("."))
            allow_absolute_paths = getattr(args, 'allow_absolute_paths', False)
            max_path_length = getattr(args, 'max_path_length', None)
            _cmd_tar_extract(
                args.archive,
                output_dir,
                allow_absolute_paths=allow_absolute_paths,
                max_path_length=max_path_length,
            )
        elif args.command == "7z-create":
            compression_method = getattr(args, 'compression', 'lzma2')
            compression_level = getattr(args, 'compression_level', 6)
            _cmd_7z_create(
                args.archive,
                args.sources,
                compression_method=compression_method,
                compression_level=compression_level,
            )
        elif args.command == "7z-list":
            _cmd_7z_list(args.archive)
        elif args.command == "7z-extract":
            output_dir = getattr(args, 'output_dir', Path("."))
            allow_absolute_paths = getattr(args, 'allow_absolute_paths', False)
            max_path_length = getattr(args, 'max_path_length', None)
            _cmd_7z_extract(
                args.archive,
                output_dir,
                allow_absolute_paths=allow_absolute_paths,
                max_path_length=max_path_length,
            )
        elif args.command == "rar-list":
            _cmd_rar_list(args.archive)
        elif args.command == "rar-extract":
            output_dir = getattr(args, 'output_dir', Path("."))
            allow_absolute_paths = getattr(args, 'allow_absolute_paths', False)
            max_path_length = getattr(args, 'max_path_length', None)
            use_external_tool = getattr(args, 'use_external_tool', False)
            external_tool = getattr(args, 'external_tool', None)
            password = getattr(args, 'password', None)
            _cmd_rar_extract(
                args.archive,
                output_dir,
                allow_absolute_paths=allow_absolute_paths,
                max_path_length=max_path_length,
                use_external_tool=use_external_tool,
                external_tool=external_tool,
                password=password,
            )
        elif args.command == "rar-extractable":
            include_directories = not getattr(args, 'no_directories', False)
            detailed = getattr(args, 'detailed', False)
            _cmd_rar_extractable(
                args.archive,
                include_directories=include_directories,
                detailed=detailed,
            )
        elif args.command == "rar-extract-external":
            output_dir = getattr(args, 'output_dir', None)
            tool = getattr(args, 'tool', None)
            password = getattr(args, 'password', None)
            _cmd_rar_extract_external(
                args.archive,
                output_dir=output_dir,
                tool=tool,
                password=password,
            )
        elif args.command == "rar-check-tools":
            _cmd_rar_check_tools()
        elif args.command == "rar-compat":
            _cmd_rar_compat(args.archive, no_tool_check=getattr(args, 'no_tool_check', False))
        elif args.command == "update":
            compression_level = getattr(args, 'compression_level', 6)
            _cmd_update(
                args.archive,
                args.entry,
                args.source,
                compression=getattr(args, 'compression', 'deflate'),
                compression_level=compression_level
            )
        elif args.command == "delete":
            _cmd_delete(args.archive, args.entry)
        elif args.command == "rename":
            _cmd_rename(args.archive, args.old_name, args.new_name)
        elif args.command == "merge":
            overwrite = getattr(args, 'overwrite', False)
            conflict_resolution = getattr(args, 'conflict_resolution', 'skip')
            _cmd_merge(
                args.output,
                args.archives,
                overwrite=overwrite,
                conflict_resolution=conflict_resolution,
            )
        elif args.command == "split":
            overwrite = getattr(args, 'overwrite', False)
            max_size = getattr(args, 'max_size', None)
            max_entries = getattr(args, 'max_entries', None)
            _cmd_split(
                args.archive,
                args.output,
                max_size=max_size,
                max_entries=max_entries,
                overwrite=overwrite,
            )
        elif args.command == "convert":
            source_format = getattr(args, 'source_format', None)
            target_format = getattr(args, 'target_format', None)
            compression = getattr(args, 'compression', None)
            compression_level = getattr(args, 'compression_level', None)
            password = getattr(args, 'password', None)
            password_file = getattr(args, 'password_file', None)
            preserve_metadata = not getattr(args, 'no_preserve_metadata', False)
            overwrite = getattr(args, 'overwrite', False)
            use_external_tool_for_rar = getattr(args, 'use_external_tool_for_rar', False)
            external_tool = getattr(args, 'external_tool', None)
            _cmd_convert(
                args.source,
                args.target,
                source_format=source_format,
                target_format=target_format,
                compression=compression,
                compression_level=compression_level,
                password=password,
                password_file=password_file,
                preserve_metadata=preserve_metadata,
                overwrite=overwrite,
                use_external_tool_for_rar=use_external_tool_for_rar,
                external_tool=external_tool,
            )
        elif args.command == "compare":
            exit_code = _cmd_compare(
                args.archive1,
                args.archive2,
                format=getattr(args, 'format', None),
            )
            sys.exit(exit_code)
        elif args.command == "compare-formats":
            _cmd_compare_formats(
                args.archive1,
                args.archive2,
                format1=getattr(args, 'format1', None),
                format2=getattr(args, 'format2', None),
                timeout=getattr(args, 'timeout', 300),
            )
        elif args.command == "format-statistics":
            _cmd_format_statistics(
                args.archive,
                format_name=getattr(args, 'format', None),
                timeout=getattr(args, 'timeout', 300),
            )
        elif args.command == "diff":
            _cmd_diff(
                args.archive1,
                args.archive2,
                format=getattr(args, 'format', None),
                summary_only=getattr(args, 'summary_only', False),
            )
        elif args.command == "export":
            _cmd_export(
                args.archive,
                args.output,
                format=getattr(args, 'format', 'json'),
                archive_format=getattr(args, 'archive_format', None),
            )
        elif args.command == "optimize":
            password = getattr(args, 'password', None)
            password_file = getattr(args, 'password_file', None)
            _cmd_optimize(
                args.archive,
                args.output,
                compression=getattr(args, 'compression', None),
                compression_level=getattr(args, 'compression_level', None),
                password=password,
                password_file=password_file,
                no_preserve_metadata=getattr(args, 'no_preserve_metadata', False),
            )
        elif args.command == "repair":
            _cmd_repair(
                args.archive,
                output=getattr(args, 'output', None),
                repair=getattr(args, 'repair', False),
                crc_mode=getattr(args, 'crc_mode', 'strict'),
                format=getattr(args, 'format', None),
            )
        elif args.command == "recover":
            _cmd_recover(
                args.archive,
                args.output_dir,
                no_skip_crc=getattr(args, 'no_skip_crc', False),
                no_partial_recovery=getattr(args, 'no_partial_recovery', False),
                password=getattr(args, 'password', None),
                password_file=getattr(args, 'password_file', None),
                format=getattr(args, 'format', None),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "deduplicate":
            _cmd_deduplicate(
                args.archive,
                args.output,
                hash_algorithm=getattr(args, 'hash_algorithm', 'crc32'),
                keep_last=getattr(args, 'keep_last', False),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                compression=getattr(args, 'compression', None),
                compression_level=getattr(args, 'compression_level', None),
                password=getattr(args, 'password', None),
                password_file=getattr(args, 'password_file', None),
                format=getattr(args, 'format', None),
            )
        elif args.command == "find-duplicates":
            _cmd_find_duplicates(
                args.archives,
                hash_algorithm=getattr(args, 'hash_algorithm', 'crc32'),
                password=getattr(args, 'password', None),
                password_file=getattr(args, 'password_file', None),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-smart":
            _cmd_create_smart(
                args.archive,
                args.files,
                strategy=getattr(args, 'strategy', 'best_compression'),
                sample_size=getattr(args, 'sample_size', None),
                test_methods=getattr(args, 'test_methods', None),
                test_levels=getattr(args, 'test_levels', None),
                password=getattr(args, 'password', None),
                password_file=getattr(args, 'password_file', None),
                no_preserve_metadata=getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-preset":
            _cmd_create_preset(
                args.archive,
                args.files,
                preset=getattr(args, 'preset', 'balanced'),
                password=getattr(args, 'password', None),
                password_file=getattr(args, 'password_file', None),
                no_preserve_metadata=getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-clean":
            _cmd_create_clean(
                args.archive,
                args.files,
                preset=getattr(args, 'preset', 'balanced'),
                no_exclude_temp=getattr(args, 'no_exclude_temp', False),
                exclude_patterns=getattr(args, 'exclude_patterns', None),
                exclude_extensions=getattr(args, 'exclude_extensions', None),
                password=getattr(args, 'password', None),
                password_file=getattr(args, 'password_file', None),
                no_preserve_metadata=getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-dedup":
            _cmd_create_dedup(
                args.archive,
                args.files,
                hash_algorithm=getattr(args, 'hash_algorithm', 'crc32'),
                keep_first=not getattr(args, 'keep_last', False),
                compression=getattr(args, 'compression', None),
                compression_level=getattr(args, 'compression_level', None),
                preset=getattr(args, 'preset', None),
                password=getattr(args, 'password', None),
                password_file=getattr(args, 'password_file', None),
                no_preserve_metadata=getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-size-based":
            _cmd_create_size_based(
                args.archive,
                args.files,
                compression=getattr(args, 'compression', 'deflate'),
                small_threshold=getattr(args, 'small_threshold', 1024 * 1024),
                medium_threshold=getattr(args, 'medium_threshold', 10 * 1024 * 1024),
                small_level=getattr(args, 'small_level', 3),
                medium_level=getattr(args, 'medium_level', 6),
                large_level=getattr(args, 'large_level', 9),
                password=getattr(args, 'password', None),
                password_file=getattr(args, 'password_file', None),
                no_preserve_metadata=getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "backup":
            _cmd_backup(
                args.base_archive,
                args.files,
                timestamp_format=getattr(args, 'timestamp_format', '%Y-%m-%d_%H-%M-%S'),
                include_timezone=getattr(args, 'include_timezone', False),
                max_backups=getattr(args, 'max_backups', None),
                compression=getattr(args, 'compression', 'deflate'),
                compression_level=getattr(args, 'compression_level', 6),
                password=getattr(args, 'password', None),
                password_file=getattr(args, 'password_file', None),
                no_preserve_metadata=getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-content-based":
            _cmd_create_content_based(
                args.archive,
                args.files,
                preset=getattr(args, 'preset', 'balanced'),
                password=getattr(args, 'password', None),
                password_file=getattr(args, 'password_file', None),
                no_preserve_metadata=getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-incremental":
            _cmd_create_incremental(
                args.archive,
                args.files,
                args.reference,
                compare_by=getattr(args, 'compare_by', 'mtime'),
                compression=getattr(args, 'compression', 'deflate'),
                compression_level=getattr(args, 'compression_level', 6),
                password=getattr(args, 'password', None),
                password_file=getattr(args, 'password_file', None),
                reference_password=getattr(args, 'reference_password', None),
                reference_password_file=getattr(args, 'reference_password_file', None),
                no_preserve_metadata=getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-recent":
            _cmd_create_recent(
                args.archive,
                args.files,
                hours=getattr(args, 'hours', None),
                days=getattr(args, 'days', None),
                compression=getattr(args, 'compression', 'deflate'),
                compression_level=getattr(args, 'compression_level', 6),
                password=getattr(args, 'password', None),
                password_file=getattr(args, 'password_file', None),
                no_preserve_metadata=getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-organize":
            _cmd_create_organize(
                args.archive,
                args.files,
                organize_by=getattr(args, 'organize_by', 'type'),
                preserve_original_structure=getattr(args, 'preserve_original_structure', False),
                compression=getattr(args, 'compression', 'deflate'),
                compression_level=getattr(args, 'compression_level', 6),
                password=getattr(args, 'password', None),
                password_file=getattr(args, 'password_file', None),
                no_preserve_metadata=getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "analyze-files":
            _cmd_analyze_files(
                args.files,
                sample_size=getattr(args, 'sample_size', None),
                no_content_analysis=getattr(args, 'no_content_analysis', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-embedded-metadata":
            _cmd_create_embedded_metadata(
                args.archive,
                args.files,
                metadata_format=getattr(args, 'metadata_format', 'json'),
                include_manifest=not getattr(args, 'no_manifest', False),
                include_checksums=not getattr(args, 'no_checksums', False),
                include_creation_info=not getattr(args, 'no_creation_info', False),
                metadata_prefix=getattr(args, 'metadata_prefix', '.dnzip'),
                compression=getattr(args, 'compression', None),
                compression_level=getattr(args, 'compression_level', None),
                preset=getattr(args, 'preset', None),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-filter":
            from datetime import datetime
            
            # Parse dates if provided
            start_date = None
            end_date = None
            if getattr(args, 'start_date', None):
                try:
                    start_date = datetime.strptime(args.start_date, '%Y-%m-%d')
                except ValueError:
                    try:
                        start_date = datetime.strptime(args.start_date, '%Y-%m-%d %H:%M:%S')
                    except ValueError:
                        _print_error(f"Invalid start date format: {args.start_date}. Use YYYY-MM-DD or YYYY-MM-DD HH:MM:SS", exit_code=2)
            if getattr(args, 'end_date', None):
                try:
                    end_date = datetime.strptime(args.end_date, '%Y-%m-%d')
                except ValueError:
                    try:
                        end_date = datetime.strptime(args.end_date, '%Y-%m-%d %H:%M:%S')
                    except ValueError:
                        _print_error(f"Invalid end date format: {args.end_date}. Use YYYY-MM-DD or YYYY-MM-DD HH:MM:SS", exit_code=2)
            
            _cmd_create_filter(
                args.archive,
                args.files,
                include_patterns=getattr(args, 'include_patterns', None),
                exclude_patterns=getattr(args, 'exclude_patterns', None),
                include_regex=getattr(args, 'include_regex', None),
                exclude_regex=getattr(args, 'exclude_regex', None),
                include_extensions=getattr(args, 'include_extensions', None),
                exclude_extensions=getattr(args, 'exclude_extensions', None),
                min_size=getattr(args, 'min_size', None),
                max_size=getattr(args, 'max_size', None),
                start_date=start_date,
                end_date=end_date,
                case_sensitive=getattr(args, 'case_sensitive', False),
                compression=getattr(args, 'compression', None),
                compression_level=getattr(args, 'compression_level', None),
                preset=getattr(args, 'preset', None),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-verify":
            _cmd_create_verify(
                args.archive,
                args.files,
                verify_crc=not getattr(args, 'no_verify_crc', False),
                verify_size=not getattr(args, 'no_verify_size', False),
                verify_decompression=not getattr(args, 'no_verify_decompression', False),
                fail_fast=getattr(args, 'fail_fast', False),
                compression=getattr(args, 'compression', None),
                compression_level=getattr(args, 'compression_level', None),
                preset=getattr(args, 'preset', None),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-optimize":
            _cmd_create_optimize(
                args.archive,
                args.files,
                optimization_mode=getattr(args, 'optimization_mode', 'best_compression'),
                target_ratio=getattr(args, 'target_ratio', None),
                max_iterations=getattr(args, 'max_iterations', 3),
                test_methods=getattr(args, 'test_methods', None),
                test_levels=getattr(args, 'test_levels', None),
                compression=getattr(args, 'compression', None),
                compression_level=getattr(args, 'compression_level', None),
                preset=getattr(args, 'preset', None),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-parallel":
            _cmd_create_parallel(
                args.archive,
                args.files,
                auto_threads=not getattr(args, 'no_auto_threads', False),
                max_threads=getattr(args, 'max_threads', None),
                min_files_for_parallel=getattr(args, 'min_files_for_parallel', 4),
                compression=getattr(args, 'compression', None),
                compression_level=getattr(args, 'compression_level', None),
                preset=getattr(args, 'preset', None),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-redundant":
            _cmd_create_redundant(
                args.archive,
                args.files,
                redundancy_mode=getattr(args, 'redundancy_mode', 'copies'),
                num_copies=getattr(args, 'num_copies', 2),
                redundancy_location=getattr(args, 'redundancy_location', None),
                include_checksums=not getattr(args, 'no_checksums', False),
                checksum_algorithm=getattr(args, 'checksum_algorithm', 'sha256'),
                compression=getattr(args, 'compression', None),
                compression_level=getattr(args, 'compression_level', None),
                preset=getattr(args, 'preset', None),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-retry":
            _cmd_create_retry(
                args.archive,
                args.files,
                max_retries=getattr(args, 'max_retries', 3),
                retry_delay=getattr(args, 'retry_delay', 1.0),
                resume_on_interrupt=not getattr(args, 'no_resume', False),
                compression=getattr(args, 'compression', None),
                compression_level=getattr(args, 'compression_level', None),
                preset=getattr(args, 'preset', None),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-auto-format":
            _cmd_create_auto_format(
                args.archive,
                args.files,
                format_selection_strategy=getattr(args, 'format_selection_strategy', 'best_compression'),
                prefer_zip=not getattr(args, 'no_prefer_zip', False),
                prefer_tar=getattr(args, 'prefer_tar', False),
                prefer_7z=getattr(args, 'prefer_7z', False),
                compression=getattr(args, 'compression', None),
                compression_level=getattr(args, 'compression_level', None),
                preset=getattr(args, 'preset', None),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-entropy":
            _cmd_create_entropy(
                args.archive,
                args.files,
                entropy_threshold=getattr(args, 'entropy_threshold', 7.5),
                sample_size=getattr(args, 'sample_size', 8192),
                compression=getattr(args, 'compression', 'deflate'),
                compression_level=getattr(args, 'compression_level', 6),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-pattern":
            _cmd_create_pattern(
                args.archive,
                args.files,
                pattern_analysis_size=getattr(args, 'pattern_analysis_size', 16384),
                compression=getattr(args, 'compression', 'deflate'),
                compression_level=getattr(args, 'compression_level', 6),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-time-based":
            _cmd_create_time_based(
                args.archive,
                args.files,
                recent_threshold_days=getattr(args, 'recent_threshold_days', 7),
                old_threshold_days=getattr(args, 'old_threshold_days', 90),
                recent_compression=getattr(args, 'recent_compression', 'deflate'),
                recent_level=getattr(args, 'recent_level', 6),
                old_compression=getattr(args, 'old_compression', 'lzma'),
                old_level=getattr(args, 'old_level', 9),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-creation-based":
            _cmd_create_creation_based(
                args.archive,
                args.files,
                recent_threshold_days=getattr(args, 'recent_threshold_days', 7),
                old_threshold_days=getattr(args, 'old_threshold_days', 90),
                recent_compression=getattr(args, 'recent_compression', 'deflate'),
                recent_level=getattr(args, 'recent_level', 6),
                old_compression=getattr(args, 'old_compression', 'lzma'),
                old_level=getattr(args, 'old_level', 9),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-permission-based":
            _cmd_create_permission_based(
                args.archive,
                args.files,
                permission_rules=getattr(args, 'permission_rules', None),
                default_compression=getattr(args, 'default_compression', 'deflate'),
                default_level=getattr(args, 'default_level', 6),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-owner-based":
            _cmd_create_owner_based(
                args.archive,
                args.files,
                owner_rules=getattr(args, 'owner_rules', None),
                group_rules=getattr(args, 'group_rules', None),
                default_compression=getattr(args, 'default_compression', 'deflate'),
                default_level=getattr(args, 'default_level', 6),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-path-based":
            _cmd_create_path_based(
                args.archive,
                args.files,
                path_patterns=getattr(args, 'path_patterns', None),
                default_compression=getattr(args, 'default_compression', 'deflate'),
                default_level=getattr(args, 'default_level', 6),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-extension-based":
            _cmd_create_extension_based(
                args.archive,
                args.files,
                extension_rules=getattr(args, 'extension_rules', None),
                default_compression=getattr(args, 'default_compression', 'deflate'),
                default_level=getattr(args, 'default_level', 6),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-mime-based":
            _cmd_create_mime_based(
                args.archive,
                args.files,
                mime_rules=getattr(args, 'mime_rules', None),
                default_compression=getattr(args, 'default_compression', 'deflate'),
                default_level=getattr(args, 'default_level', 6),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-hybrid":
            _cmd_create_hybrid(
                args.archive,
                args.files,
                strategies=getattr(args, 'strategies', None),
                strategy_weights=getattr(args, 'strategy_weights', None),
                default_compression=getattr(args, 'default_compression', 'deflate'),
                default_level=getattr(args, 'default_level', 6),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-metadata-combined":
            _cmd_create_metadata_combined(
                args.archive,
                args.files,
                metadata_rules=getattr(args, 'metadata_rules', None),
                default_compression=getattr(args, 'default_compression', 'deflate'),
                default_level=getattr(args, 'default_level', 6),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-relationship-based":
            _cmd_create_relationship_based(
                args.archive,
                args.files,
                relationship_detection=getattr(args, 'relationship_detection', 'path'),
                relationship_threshold=getattr(args, 'relationship_threshold', 0.7),
                group_compression=getattr(args, 'group_compression', 'deflate'),
                group_level=getattr(args, 'group_level', 6),
                default_compression=getattr(args, 'default_compression', 'deflate'),
                default_level=getattr(args, 'default_level', 6),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-stability-based":
            _cmd_create_stability_based(
                args.archive,
                args.files,
                stable_threshold_ratio=getattr(args, 'stable_threshold_ratio', 0.8),
                unstable_threshold_ratio=getattr(args, 'unstable_threshold_ratio', 0.2),
                stable_compression=getattr(args, 'stable_compression', 'lzma'),
                stable_level=getattr(args, 'stable_level', 9),
                unstable_compression=getattr(args, 'unstable_compression', 'deflate'),
                unstable_level=getattr(args, 'unstable_level', 6),
                default_compression=getattr(args, 'default_compression', 'deflate'),
                default_level=getattr(args, 'default_level', 6),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-priority-based":
            _cmd_create_priority_based(
                args.archive,
                args.files,
                priority_rules=getattr(args, 'priority_rules', None),
                default_compression=getattr(args, 'default_compression', 'deflate'),
                default_level=getattr(args, 'default_level', 6),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-count-based":
            _cmd_create_count_based(
                args.archive,
                args.files,
                few_files_threshold=getattr(args, 'few_files_threshold', 10),
                many_files_threshold=getattr(args, 'many_files_threshold', 100),
                few_files_compression=getattr(args, 'few_files_compression', 'deflate'),
                few_files_level=getattr(args, 'few_files_level', 9),
                many_files_compression=getattr(args, 'many_files_compression', 'deflate'),
                many_files_level=getattr(args, 'many_files_level', 6),
                default_compression=getattr(args, 'default_compression', 'deflate'),
                default_level=getattr(args, 'default_level', 6),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-total-size-based":
            _cmd_create_total_size_based(
                args.archive,
                args.files,
                small_archive_threshold=getattr(args, 'small_archive_threshold', 100 * 1024 * 1024),
                large_archive_threshold=getattr(args, 'large_archive_threshold', 1024 * 1024 * 1024),
                small_archive_compression=getattr(args, 'small_archive_compression', 'deflate'),
                small_archive_level=getattr(args, 'small_archive_level', 9),
                large_archive_compression=getattr(args, 'large_archive_compression', 'deflate'),
                large_archive_level=getattr(args, 'large_archive_level', 6),
                default_compression=getattr(args, 'default_compression', 'deflate'),
                default_level=getattr(args, 'default_level', 6),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-efficiency-based":
            _cmd_create_efficiency_based(
                args.archive,
                args.files,
                sample_size=getattr(args, 'sample_size', 10),
                sample_percent=getattr(args, 'sample_percent', None),
                test_methods=getattr(args, 'test_methods', None),
                test_levels=getattr(args, 'test_levels', None),
                min_sample_files=getattr(args, 'min_sample_files', 3),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-type-distribution-based":
            _cmd_create_type_distribution_based(
                args.archive,
                args.files,
                dominant_threshold=getattr(args, 'dominant_threshold', 0.5),
                type_compression_map=getattr(args, 'type_compression_map', None),
                default_compression=getattr(args, 'default_compression', 'deflate'),
                default_level=getattr(args, 'default_level', 6),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-adaptive":
            _cmd_create_adaptive(
                args.archive,
                args.files,
                initial_compression=getattr(args, 'initial_compression', 'deflate'),
                initial_level=getattr(args, 'initial_level', 6),
                adaptation_threshold=getattr(args, 'adaptation_threshold', 0.95),
                test_methods=getattr(args, 'test_methods', None),
                test_levels=getattr(args, 'test_levels', None),
                adaptation_window=getattr(args, 'adaptation_window', 10),
                min_improvement=getattr(args, 'min_improvement', 0.05),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-target-based":
            _cmd_create_target_based(
                args.archive,
                args.files,
                target_ratio=getattr(args, 'target_ratio', None),
                target_space_saved=getattr(args, 'target_space_saved', None),
                target_space_saved_percent=getattr(args, 'target_space_saved_percent', None),
                initial_compression=getattr(args, 'initial_compression', 'deflate'),
                initial_level=getattr(args, 'initial_level', 6),
                test_methods=getattr(args, 'test_methods', None),
                test_levels=getattr(args, 'test_levels', None),
                max_iterations=getattr(args, 'max_iterations', 3),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-speed-based":
            _cmd_create_speed_based(
                args.archive,
                args.files,
                speed_mode=getattr(args, 'speed_mode', 'balanced'),
                max_time_seconds=getattr(args, 'max_time_seconds', None),
                fast_compression=getattr(args, 'fast_compression', 'deflate'),
                fast_level=getattr(args, 'fast_level', 3),
                balanced_compression=getattr(args, 'balanced_compression', 'deflate'),
                balanced_level=getattr(args, 'balanced_level', 6),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-quality-based":
            _cmd_create_quality_based(
                args.archive,
                args.files,
                quality_mode=getattr(args, 'quality_mode', 'balanced'),
                min_compression_ratio=getattr(args, 'min_compression_ratio', None),
                quality_threshold=getattr(args, 'quality_threshold', 0.7),
                test_methods=getattr(args, 'test_methods', None),
                test_levels=getattr(args, 'test_levels', None),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-age-based":
            _cmd_create_age_based(
                args.archive,
                args.files,
                recent_age_days=getattr(args, 'recent_age_days', 7),
                old_age_days=getattr(args, 'old_age_days', 90),
                recent_compression=getattr(args, 'recent_compression', 'deflate'),
                recent_level=getattr(args, 'recent_level', 6),
                old_compression=getattr(args, 'old_compression', 'deflate'),
                old_level=getattr(args, 'old_level', 9),
                default_compression=getattr(args, 'default_compression', 'deflate'),
                default_level=getattr(args, 'default_level', 6),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-size-distribution-based":
            _cmd_create_size_distribution_based(
                args.archive,
                args.files,
                small_file_threshold=getattr(args, 'small_file_threshold', 1024 * 1024),
                large_file_threshold=getattr(args, 'large_file_threshold', 10 * 1024 * 1024),
                mostly_small_compression=getattr(args, 'mostly_small_compression', 'deflate'),
                mostly_small_level=getattr(args, 'mostly_small_level', 6),
                mostly_large_compression=getattr(args, 'mostly_large_compression', 'lzma'),
                mostly_large_level=getattr(args, 'mostly_large_level', 9),
                mixed_compression=getattr(args, 'mixed_compression', 'deflate'),
                mixed_level=getattr(args, 'mixed_level', 6),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-activity-based":
            _cmd_create_activity_based(
                args.archive,
                args.files,
                high_activity_threshold_hours=getattr(args, 'high_activity_threshold_hours', 24),
                low_activity_threshold_days=getattr(args, 'low_activity_threshold_days', 30),
                high_activity_compression=getattr(args, 'high_activity_compression', 'deflate'),
                high_activity_level=getattr(args, 'high_activity_level', 6),
                low_activity_compression=getattr(args, 'low_activity_compression', 'lzma'),
                low_activity_level=getattr(args, 'low_activity_level', 9),
                medium_activity_compression=getattr(args, 'medium_activity_compression', 'deflate'),
                medium_activity_level=getattr(args, 'medium_activity_level', 6),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-performance-requirements-based":
            _cmd_create_performance_requirements_based(
                args.archive,
                args.files,
                max_time_seconds=getattr(args, 'max_time_seconds', None),
                min_compression_ratio=getattr(args, 'min_compression_ratio', None),
                max_compression_time_per_file=getattr(args, 'max_compression_time_per_file', None),
                test_methods=getattr(args, 'test_methods', None),
                test_levels=getattr(args, 'test_levels', None),
                default_compression=getattr(args, 'default_compression', 'deflate'),
                default_level=getattr(args, 'default_level', 6),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-compressibility-based":
            _cmd_create_compressibility_based(
                args.archive,
                args.files,
                highly_compressible_threshold=getattr(args, 'highly_compressible_threshold', 0.5),
                poorly_compressible_threshold=getattr(args, 'poorly_compressible_threshold', 0.9),
                highly_compressible_compression=getattr(args, 'highly_compressible_compression', 'lzma'),
                highly_compressible_level=getattr(args, 'highly_compressible_level', 9),
                poorly_compressible_compression=getattr(args, 'poorly_compressible_compression', 'stored'),
                poorly_compressible_level=getattr(args, 'poorly_compressible_level', 0),
                default_compression=getattr(args, 'default_compression', 'deflate'),
                default_level=getattr(args, 'default_level', 6),
                test_sample_size=getattr(args, 'test_sample_size', 5),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-effectiveness-scoring-based":
            _cmd_create_effectiveness_scoring_based(
                args.archive,
                args.files,
                high_effectiveness_threshold=getattr(args, 'high_effectiveness_threshold', 0.6),
                low_effectiveness_threshold=getattr(args, 'low_effectiveness_threshold', 0.3),
                high_effectiveness_compression=getattr(args, 'high_effectiveness_compression', 'lzma'),
                high_effectiveness_level=getattr(args, 'high_effectiveness_level', 9),
                low_effectiveness_compression=getattr(args, 'low_effectiveness_compression', 'stored'),
                low_effectiveness_level=getattr(args, 'low_effectiveness_level', 0),
                default_compression=getattr(args, 'default_compression', 'deflate'),
                default_level=getattr(args, 'default_level', 6),
                test_methods=getattr(args, 'test_methods', None),
                test_levels=getattr(args, 'test_levels', None),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-pattern-based":
            _cmd_create_pattern_based(
                args.archive,
                args.files,
                pattern_analysis_size=getattr(args, 'pattern_analysis_size', 16384),
                compression=getattr(args, 'compression', 'deflate'),
                compression_level=getattr(args, 'compression_level', 6),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-similarity-based":
            _cmd_create_similarity_based(
                args.archive,
                args.files,
                similarity_threshold=getattr(args, 'similarity_threshold', 0.8),
                sample_size=getattr(args, 'sample_size', 4096),
                group_compression=getattr(args, 'group_compression', 'deflate'),
                group_level=getattr(args, 'group_level', 6),
                default_compression=getattr(args, 'default_compression', 'deflate'),
                default_level=getattr(args, 'default_level', 6),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-naming-based":
            _cmd_create_naming_based(
                args.archive,
                args.files,
                naming_patterns=getattr(args, 'naming_patterns', None),
                default_compression=getattr(args, 'default_compression', 'deflate'),
                default_level=getattr(args, 'default_level', 6),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-depth-based":
            _cmd_create_depth_based(
                args.archive,
                args.files,
                depth_thresholds=getattr(args, 'depth_thresholds', None),
                depth_compressions=getattr(args, 'depth_compressions', None),
                default_compression=getattr(args, 'default_compression', 'deflate'),
                default_level=getattr(args, 'default_level', 6),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-access-based":
            _cmd_create_access_based(
                args.archive,
                args.files,
                frequent_threshold_days=getattr(args, 'frequent_threshold_days', 7),
                rare_threshold_days=getattr(args, 'rare_threshold_days', 90),
                frequent_compression=getattr(args, 'frequent_compression', 'deflate'),
                frequent_level=getattr(args, 'frequent_level', 6),
                rare_compression=getattr(args, 'rare_compression', 'lzma'),
                rare_level=getattr(args, 'rare_level', 9),
                archive_comment=getattr(args, 'comment', None),
                password=getattr(args, 'password', None),
                aes_version=getattr(args, 'aes_version', 1),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "batch-convert-smart":
            _cmd_batch_convert_smart(
                args.archives,
                args.output_dir,
                target_format=getattr(args, 'target_format', 'zip'),
                strategy=getattr(args, 'strategy', 'best_compression'),
                sample_size=getattr(args, 'sample_size', None),
                test_methods=getattr(args, 'test_methods', None),
                test_levels=getattr(args, 'test_levels', None),
                source_format=getattr(args, 'source_format', None),
                password=getattr(args, 'password', None),
                password_file=getattr(args, 'password_file', None),
                use_external_tool_for_rar=getattr(args, 'use_external_tool_for_rar', False),
                no_preserve_metadata=getattr(args, 'no_preserve_metadata', False),
                stop_on_error=getattr(args, 'stop_on_error', False),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "extract-extractable":
            _cmd_extract_extractable(
                args.source,
                args.target,
                source_format=getattr(args, 'source_format', None),
                target_format=getattr(args, 'target_format', None),
                compression=getattr(args, 'compression', None),
                compression_level=getattr(args, 'compression_level', None),
                password=getattr(args, 'password', None),
                password_file=getattr(args, 'password_file', None),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
            )
        elif args.command == "normalize":
            _cmd_normalize(
                args.archive,
                args.output,
                normalize_paths=not getattr(args, 'no_normalize_paths', False),
                remove_empty_dirs=not getattr(args, 'keep_empty_dirs', False),
                standardize_compression=getattr(args, 'standardize_compression', None),
                compression_level=getattr(args, 'compression_level', None),
                sort_entries=not getattr(args, 'no_sort', False),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                password=getattr(args, 'password', None),
                password_file=getattr(args, 'password_file', None),
                format=getattr(args, 'format', None),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "filter":
            _cmd_filter(
                args.archive,
                args.output,
                include_patterns=getattr(args, 'include_patterns', None),
                exclude_patterns=getattr(args, 'exclude_patterns', None),
                include_extensions=getattr(args, 'include_extensions', None),
                exclude_extensions=getattr(args, 'exclude_extensions', None),
                min_size=getattr(args, 'min_size', None),
                max_size=getattr(args, 'max_size', None),
                start_date=getattr(args, 'start_date', None),
                end_date=getattr(args, 'end_date', None),
                case_sensitive=getattr(args, 'case_sensitive', False),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                compression=getattr(args, 'compression', None),
                compression_level=getattr(args, 'compression_level', None),
                password=getattr(args, 'password', None),
                password_file=getattr(args, 'password_file', None),
                format=getattr(args, 'format', None),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "create-index":
            _cmd_create_index(
                args.archive,
                index=getattr(args, 'index', None),
                include_content_hash=getattr(args, 'include_content_hash', False),
                include_metadata=not getattr(args, 'no_metadata', False),
                format=getattr(args, 'format', None),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "search-index":
            has_content_hash = None
            if getattr(args, 'has_content_hash', False):
                has_content_hash = True
            elif getattr(args, 'no_content_hash', False):
                has_content_hash = False
            _cmd_search_index(
                args.index,
                args.pattern,
                use_regex=getattr(args, 'regex', False),
                case_sensitive=getattr(args, 'case_sensitive', True),
                search_metadata=getattr(args, 'search_metadata', False),
                min_size=getattr(args, 'min_size', None),
                max_size=getattr(args, 'max_size', None),
                compression_method=getattr(args, 'compression_method', None),
                has_content_hash=has_content_hash,
            )
        elif args.command == "update-index":
            _cmd_update_index(
                args.archive,
                index=getattr(args, 'index', None),
                force=getattr(args, 'force', False),
                include_content_hash=getattr(args, 'include_content_hash', False),
                include_metadata=not getattr(args, 'no_metadata', False),
                format=getattr(args, 'format', None),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "analyze":
            _cmd_analyze(
                args.archive,
                format_name=getattr(args, 'format', None),
            )
        elif args.command == "analyze-compression":
            _cmd_analyze_compression(
                args.files,
                sample_size=getattr(args, 'sample_size', None),
                test_methods=getattr(args, 'test_methods', None),
                test_levels=getattr(args, 'test_levels', None),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "health-check":
            _cmd_health_check(
                args.archive,
                format_name=getattr(args, 'format', None),
            )
        elif args.command == "sync":
            _cmd_sync(
                args.archive,
                args.source_directory,
                compression=getattr(args, 'compression', 'deflate'),
                compression_level=getattr(args, 'compression_level', 6),
                remove_deleted=getattr(args, 'remove_deleted', False),
                compare_by=getattr(args, 'compare_by', 'mtime'),
                preserve_metadata=not getattr(args, 'no_preserve_metadata', False),
                password=getattr(args, 'password', None),
                password_file=getattr(args, 'password_file', None),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "batch-process":
            _cmd_batch_process(
                args.archives,
                args.operation,
                output_dir=getattr(args, 'output_dir', None),
                target_format=getattr(args, 'target_format', None),
                compression=getattr(args, 'compression', None),
                compression_level=getattr(args, 'compression_level', None),
                stop_on_error=getattr(args, 'stop_on_error', False),
            )
        elif args.command == "benchmark":
            _cmd_benchmark(
                args.benchmark_type,
                num_files=getattr(args, 'num_files', 20),
                file_size_mb=getattr(args, 'file_size_mb', 0.1),
                data_size_mb=getattr(args, 'data_size_mb', 10),
                compression=getattr(args, 'compression', 'deflate'),
                compression_level=getattr(args, 'compression_level', 6),
                output_file=getattr(args, 'output', None)
            )
        elif args.command == "benchmark-compression":
            _cmd_benchmark_compression(
                args.archive,
                methods=getattr(args, 'methods', None),
                levels=getattr(args, 'levels', None),
                max_entries=getattr(args, 'max_entries', None),
                no_sample=getattr(args, 'no_sample', False),
                no_timing=getattr(args, 'no_timing', False),
                format=getattr(args, 'format', None),
                quiet=getattr(args, 'quiet', False)
            )
        elif args.command == "create-checksum":
            _cmd_create_checksum(
                args.archive,
                output=getattr(args, 'output', None),
                algorithm=getattr(args, 'algorithm', 'sha256'),
                format=getattr(args, 'format', None),
                quiet=getattr(args, 'quiet', False),
            )
        elif args.command == "verify-checksum":
            _cmd_verify_checksum(
                args.archive,
                args.checksum_file,
                algorithm=getattr(args, 'algorithm', None),
                format=getattr(args, 'format', None),
                quiet=getattr(args, 'quiet', False),
            )
        else:
            parser.error(f"Unknown command: {args.command!r}")
    except (ZipError, ZipCrcError, ZipFormatError, SevenZipFormatError, ZipUnsupportedFeature, RarError, RarFormatError, RarUnsupportedFeature) as e:
        # Try to provide helpful suggestions based on error type and file format
        suggestion = None
        error_msg = str(e)
        
        # Check if we can detect the file format and provide suggestions
        if hasattr(args, 'archive'):
            detected_format = _detect_file_format(args.archive)
            if detected_format:
                # Provide format-specific suggestions
                if isinstance(e, ZipFormatError) and detected_format != 'zip':
                    suggestion = _get_format_suggestion(args.archive, detected_format, args.command)
                elif isinstance(e, (SevenZipFormatError, RarFormatError, RarError)):
                    # Format-specific errors - check if wrong command was used
                    if args.command in ('list', 'extract', 'create'):
                        suggestion = _get_format_suggestion(args.archive, detected_format, args.command)
        
        # Provide compression method suggestions for unsupported features
        if isinstance(e, ZipUnsupportedFeature):
            if 'PPMd' in error_msg or 'ppmd' in error_msg.lower():
                suggestion = "PPMd compression is not yet implemented. Try 'deflate', 'bzip2', or 'lzma' instead."
            elif 'compression' in error_msg.lower():
                suggestion = "Try using a different compression method: 'stored', 'deflate', 'bzip2', or 'lzma'."
        
        # Provide password suggestions for encryption errors
        if isinstance(e, (ZipError, ZipFormatError)) and ('password' in error_msg.lower() or 'encrypt' in error_msg.lower()):
            if not hasattr(args, 'password') or getattr(args, 'password', None) is None:
                suggestion = "This archive appears to be encrypted. Try adding --password or --password-file option."
        
        _print_error(error_msg, exit_code=1, suggestion=suggestion)
    except FileNotFoundError as e:
        # Try to extract file path from exception message
        error_str = str(e)
        if "'" in error_str:
            file_path = error_str.split("'")[1]
        elif '"' in error_str:
            file_path = error_str.split('"')[1]
        else:
            file_path = error_str
        suggestion = f"Check that the file exists and the path is correct: {file_path}"
        _print_error(f"File not found: {file_path}", exit_code=2, suggestion=suggestion)
    except PermissionError as e:
        # Try to extract file path from exception message
        error_str = str(e)
        if "'" in error_str:
            file_path = error_str.split("'")[1]
        elif '"' in error_str:
            file_path = error_str.split('"')[1]
        else:
            file_path = error_str
        suggestion = "Check file permissions. You may need to run with appropriate permissions or change file permissions."
        _print_error(f"Permission denied: {file_path}", exit_code=2, suggestion=suggestion)
    except KeyboardInterrupt:
        _print_error("Interrupted by user", exit_code=130)


if __name__ == "__main__":
    main()


