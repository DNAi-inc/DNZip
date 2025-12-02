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
Debugging utilities for ZIP64 library.

This module provides tools for analyzing and debugging ZIP file structures.
"""

import struct
from typing import BinaryIO, Optional

from .constants import (
    CENTRAL_DIR_HEADER,
    DATA_DESCRIPTOR,
    END_OF_CENTRAL_DIR,
    LOCAL_FILE_HEADER,
    ZIP64_END_OF_CENTRAL_DIR,
    ZIP64_END_OF_CENTRAL_DIR_LOCATOR,
)
from .utils import read_uint16, read_uint32, read_uint64, read_exact


def hex_dump(data: bytes, offset: int = 0, length: Optional[int] = None) -> str:
    """Create a hex dump of binary data.

    Args:
        data: Binary data to dump.
        offset: Starting offset for display.
        length: Maximum length to dump (None for all).

    Returns:
        Formatted hex dump string.
    """
    if length is not None:
        data = data[:length]

    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i : i + 16]
        hex_part = " ".join(f"{b:02X}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{offset + i:08X}  {hex_part:<48}  {ascii_part}")

    return "\n".join(lines)


def dump_zip_structure(file_path: str) -> str:
    """Dump the structure of a ZIP file.

    Args:
        file_path: Path to ZIP file.

    Returns:
        Formatted string describing the ZIP structure.
    """
    output = []
    output.append(f"ZIP File Structure: {file_path}\n")
    output.append("=" * 80)

    with open(file_path, "rb") as f:
        f.seek(0, 2)  # Seek to end
        file_size = f.tell()
        f.seek(0)

        output.append(f"\nFile size: {file_size} bytes ({file_size / 1024 / 1024:.2f} MB)")

        # Scan for structures
        offset = 0
        local_headers = []
        central_headers = []
        eocd_offset = None
        zip64_eocd_offset = None
        zip64_locator_offset = None

        while offset < file_size:
            f.seek(offset)
            try:
                sig = read_uint32(f)
            except Exception:
                break

            if sig == LOCAL_FILE_HEADER:
                local_headers.append(offset)
                # Skip header to find next
                f.seek(offset + 4)
                filename_len = read_uint16(f)
                extra_len = read_uint16(f)
                f.seek(offset + 30)
                compressed_size = read_uint32(f)
                offset += 30 + filename_len + extra_len + compressed_size
            elif sig == CENTRAL_DIR_HEADER:
                central_headers.append(offset)
                f.seek(offset + 4)
                filename_len = read_uint16(f)
                extra_len = read_uint16(f)
                comment_len = read_uint16(f)
                offset += 46 + filename_len + extra_len + comment_len
            elif sig == END_OF_CENTRAL_DIR:
                eocd_offset = offset
                offset += 22  # Minimum EOCD size
            elif sig == ZIP64_END_OF_CENTRAL_DIR:
                zip64_eocd_offset = offset
                f.seek(offset + 4)
                size = read_uint64(f)
                offset += 12 + size  # 4 (sig) + 8 (size) + size
            elif sig == ZIP64_END_OF_CENTRAL_DIR_LOCATOR:
                zip64_locator_offset = offset
                offset += 20
            elif sig == DATA_DESCRIPTOR:
                offset += 12  # Classic data descriptor
            else:
                offset += 1  # Unknown, advance slowly

        # Output findings
        output.append(f"\nLocal File Headers: {len(local_headers)}")
        for i, off in enumerate(local_headers[:10]):  # Show first 10
            output.append(f"  [{i}] Offset: 0x{off:08X}")

        output.append(f"\nCentral Directory Headers: {len(central_headers)}")
        for i, off in enumerate(central_headers[:10]):  # Show first 10
            output.append(f"  [{i}] Offset: 0x{off:08X}")

        if eocd_offset is not None:
            output.append(f"\nEnd of Central Directory: 0x{eocd_offset:08X}")

        if zip64_eocd_offset is not None:
            output.append(f"\nZIP64 End of Central Directory: 0x{zip64_eocd_offset:08X}")

        if zip64_locator_offset is not None:
            output.append(f"\nZIP64 Locator: 0x{zip64_locator_offset:08X}")

    return "\n".join(output)


def verify_zip_structure(file_path: str) -> tuple[bool, list[str]]:
    """Verify ZIP file structure correctness.

    Args:
        file_path: Path to ZIP file.

    Returns:
        Tuple of (is_valid, list_of_errors).
    """
    errors = []

    try:
        from .reader import ZipReader

        with ZipReader(file_path) as z:
            entries = z.list()
            # Try to read each entry
            for entry_name in entries:
                try:
                    with z.open(entry_name) as f:
                        f.read()
                except Exception as e:
                    errors.append(f"Error reading {entry_name}: {e}")

    except Exception as e:
        errors.append(f"Error opening ZIP file: {e}")

    return len(errors) == 0, errors


def compare_archives(file1_path: str, file2_path: str) -> str:
    """Compare two ZIP archives.

    Args:
        file1_path: Path to first ZIP file.
        file2_path: Path to second ZIP file.

    Returns:
        Comparison report.
    """
    output = []
    output.append(f"Comparing: {file1_path} vs {file2_path}\n")
    output.append("=" * 80)

    try:
        from .reader import ZipReader

        with ZipReader(file1_path) as z1, ZipReader(file2_path) as z2:
            entries1 = set(z1.list())
            entries2 = set(z2.list())

            # Compare entry lists
            only_in_1 = entries1 - entries2
            only_in_2 = entries2 - entries1
            common = entries1 & entries2

            output.append(f"\nEntries in file1 only: {len(only_in_1)}")
            for entry in list(only_in_1)[:10]:
                output.append(f"  - {entry}")

            output.append(f"\nEntries in file2 only: {len(only_in_2)}")
            for entry in list(only_in_2)[:10]:
                output.append(f"  - {entry}")

            output.append(f"\nCommon entries: {len(common)}")

            # Compare content of common entries
            differences = []
            for entry in list(common)[:20]:  # Check first 20
                try:
                    with z1.open(entry) as f1, z2.open(entry) as f2:
                        data1 = f1.read()
                        data2 = f2.read()
                        if data1 != data2:
                            differences.append(entry)
                except Exception as e:
                    differences.append(f"{entry} (error: {e})")

            if differences:
                output.append(f"\nEntries with different content: {len(differences)}")
                for entry in differences[:10]:
                    output.append(f"  - {entry}")
            else:
                output.append("\nAll common entries have identical content")

    except Exception as e:
        output.append(f"\nError during comparison: {e}")

    return "\n".join(output)

