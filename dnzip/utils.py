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
Utility functions for ZIP64 library.

This module provides helper functions for CRC32 calculation, DOS date/time
conversion, and safe binary I/O operations.
"""

import struct
import zlib
from datetime import datetime
from typing import BinaryIO

from .errors import ZipFormatError


def crc32(data: bytes) -> int:
    """Calculate CRC32 checksum for data.

    Args:
        data: Bytes to calculate CRC32 for.

    Returns:
        CRC32 value as unsigned 32-bit integer.
    """
    return zlib.crc32(data) & 0xFFFFFFFF


def dos_datetime_to_timestamp(dos_date: int, dos_time: int) -> datetime:
    """Convert DOS date and time to Python datetime.

    DOS date format (16 bits):
        Bits 0-4: Day (1-31)
        Bits 5-8: Month (1-12)
        Bits 9-15: Year - 1980 (0-127, so 1980-2107)

    DOS time format (16 bits):
        Bits 0-4: Second / 2 (0-29, so 0-58 seconds in 2-second increments)
        Bits 5-10: Minute (0-59)
        Bits 11-15: Hour (0-23)

    Args:
        dos_date: DOS date value (16-bit unsigned integer).
        dos_time: DOS time value (16-bit unsigned integer).

    Returns:
        datetime object representing the DOS date/time.
    """
    day = dos_date & 0x1F
    month = (dos_date >> 5) & 0x0F
    year = ((dos_date >> 9) & 0x7F) + 1980

    second = (dos_time & 0x1F) * 2
    minute = (dos_time >> 5) & 0x3F
    hour = (dos_time >> 11) & 0x1F

    try:
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        # If date is invalid, return a default datetime
        return datetime(1980, 1, 1, 0, 0, 0)


def timestamp_to_dos_datetime(dt: datetime) -> tuple[int, int]:
    """Convert Python datetime to DOS date and time.

    Args:
        dt: datetime object to convert.

    Returns:
        Tuple of (dos_date, dos_time) as 16-bit unsigned integers.

    Raises:
        ZipFormatError: If datetime values are out of valid DOS date/time ranges.
    """
    # Validate and clamp year (DOS format: 1980-2107)
    year = dt.year - 1980
    if year < 0:
        year = 0
    elif year > 127:
        year = 127

    # Validate day and month are within valid ranges
    # DOS date format: day (1-31), month (1-12)
    if dt.day < 1 or dt.day > 31:
        raise ZipFormatError(f"Invalid day value: {dt.day} (must be 1-31)")
    if dt.month < 1 or dt.month > 12:
        raise ZipFormatError(f"Invalid month value: {dt.month} (must be 1-12)")

    # Validate time components are within valid ranges
    # DOS time format: hour (0-23), minute (0-59), second (0-59, stored as second/2)
    if dt.hour < 0 or dt.hour > 23:
        raise ZipFormatError(f"Invalid hour value: {dt.hour} (must be 0-23)")
    if dt.minute < 0 or dt.minute > 59:
        raise ZipFormatError(f"Invalid minute value: {dt.minute} (must be 0-59)")
    if dt.second < 0 or dt.second > 59:
        raise ZipFormatError(f"Invalid second value: {dt.second} (must be 0-59)")

    dos_date = dt.day | (dt.month << 5) | (year << 9)
    dos_time = (dt.second // 2) | (dt.minute << 5) | (dt.hour << 11)

    return (dos_date & 0xFFFF, dos_time & 0xFFFF)


def read_exact(f: BinaryIO, size: int) -> bytes:
    """Read exactly 'size' bytes from file, raising ZipFormatError on short read.

    Args:
        f: Binary file-like object to read from.
        size: Number of bytes to read.

    Returns:
        Exactly 'size' bytes of data.

    Raises:
        ZipFormatError: If fewer than 'size' bytes could be read or size is invalid.
    """
    # Validate size is non-negative (defensive programming)
    if size < 0:
        raise ZipFormatError(f"Invalid read size: {size} (must be non-negative)")
    
    data = f.read(size)
    if len(data) != size:
        raise ZipFormatError(
            f"Unexpected end of file: expected {size} bytes, got {len(data)}"
        )
    return data


def read_uint16(f: BinaryIO) -> int:
    """Read a little-endian 16-bit unsigned integer from file.

    Args:
        f: Binary file-like object to read from.

    Returns:
        16-bit unsigned integer value.
    """
    data = read_exact(f, 2)
    return struct.unpack("<H", data)[0]


def read_uint32(f: BinaryIO) -> int:
    """Read a little-endian 32-bit unsigned integer from file.

    Args:
        f: Binary file-like object to read from.

    Returns:
        32-bit unsigned integer value.
    """
    data = read_exact(f, 4)
    return struct.unpack("<I", data)[0]


def read_uint64(f: BinaryIO) -> int:
    """Read a little-endian 64-bit unsigned integer from file.

    Args:
        f: Binary file-like object to read from.

    Returns:
        64-bit unsigned integer value.
    """
    data = read_exact(f, 8)
    return struct.unpack("<Q", data)[0]


def write_uint16(f: BinaryIO, value: int) -> None:
    """Write a little-endian 16-bit unsigned integer to file.

    Args:
        f: Binary file-like object to write to.
        value: 16-bit unsigned integer value to write.

    Raises:
        ZipFormatError: If the write operation fails or writes fewer bytes than expected.
    """
    data = struct.pack("<H", value & 0xFFFF)
    written = f.write(data)
    if written != len(data):
        raise ZipFormatError(f"Write operation failed: expected to write {len(data)} bytes, wrote {written} bytes")


def write_uint32(f: BinaryIO, value: int) -> None:
    """Write a little-endian 32-bit unsigned integer to file.

    Args:
        f: Binary file-like object to write to.
        value: 32-bit unsigned integer value to write.

    Raises:
        ZipFormatError: If the write operation fails or writes fewer bytes than expected.
    """
    data = struct.pack("<I", value & 0xFFFFFFFF)
    written = f.write(data)
    if written != len(data):
        raise ZipFormatError(f"Write operation failed: expected to write {len(data)} bytes, wrote {written} bytes")


def write_uint64(f: BinaryIO, value: int) -> None:
    """Write a little-endian 64-bit unsigned integer to file.

    Args:
        f: Binary file-like object to write to.
        value: 64-bit unsigned integer value to write.

    Raises:
        ZipFormatError: If the write operation fails or writes fewer bytes than expected.
    """
    data = struct.pack("<Q", value & 0xFFFFFFFFFFFFFFFF)
    written = f.write(data)
    if written != len(data):
        raise ZipFormatError(f"Write operation failed: expected to write {len(data)} bytes, wrote {written} bytes")

