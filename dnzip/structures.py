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
ZIP structure definitions and parsing functions.

This module defines dataclasses for all ZIP file structures including
local file headers, central directory headers, end of central directory
records, and ZIP64 extensions.
"""

import struct
from dataclasses import dataclass
from datetime import datetime
from typing import BinaryIO, Optional

from .constants import (
    CENTRAL_DIR_HEADER,
    CENTRAL_DIR_HEADER_SIZE,
    DATA_DESCRIPTOR,
    DATA_DESCRIPTOR_SIZE,
    END_OF_CENTRAL_DIR,
    END_OF_CENTRAL_DIR_SIZE,
    LOCAL_FILE_HEADER,
    LOCAL_FILE_HEADER_SIZE,
    ZIP64_END_OF_CENTRAL_DIR,
    ZIP64_END_OF_CENTRAL_DIR_LOCATOR,
    ZIP64_END_OF_CENTRAL_DIR_SIZE,
    ZIP64_EXTRA_FIELD_TAG,
    ZIP64_LOCATOR_SIZE,
    ZIP64_DATA_DESCRIPTOR_SIZE,
)
from .errors import ZipFormatError
from .utils import (
    dos_datetime_to_timestamp,
    read_exact,
    read_uint16,
    read_uint32,
    read_uint64,
)


@dataclass
class LocalFileHeader:
    """Local file header structure.

    This header appears before each file's compressed data in the ZIP archive.
    """

    signature: int
    version: int
    flags: int
    compression_method: int
    mod_time: int
    mod_date: int
    crc32: int
    compressed_size: int
    uncompressed_size: int
    filename_len: int
    extra_len: int
    filename: bytes
    extra: bytes

    @property
    def date_time(self) -> datetime:
        """Get modification date/time as datetime object."""
        return dos_datetime_to_timestamp(self.mod_date, self.mod_time)


@dataclass
class CentralDirectoryHeader:
    """Central directory header structure.

    This header appears in the central directory and contains information
    about a file entry, including a pointer to the local file header.
    """

    signature: int
    version_made_by: int
    version: int
    flags: int
    compression_method: int
    mod_time: int
    mod_date: int
    crc32: int
    compressed_size: int
    uncompressed_size: int
    filename_len: int
    extra_len: int
    comment_len: int
    disk_num: int
    internal_attrs: int
    external_attrs: int
    local_header_offset: int
    filename: bytes
    extra: bytes
    comment: bytes

    @property
    def date_time(self) -> datetime:
        """Get modification date/time as datetime object."""
        return dos_datetime_to_timestamp(self.mod_date, self.mod_time)


@dataclass
class EndOfCentralDirectory:
    """End of Central Directory record.

    This record marks the end of the central directory and contains
    information needed to locate the central directory.
    """

    signature: int
    disk_num: int
    cd_disk: int
    cd_records_on_disk: int
    cd_records_total: int
    cd_size: int
    cd_offset: int
    comment_len: int
    comment: bytes


@dataclass
class Zip64EndOfCentralDirectory:
    """ZIP64 End of Central Directory record.

    This record is used when ZIP64 extensions are needed (large files,
    many entries, etc.).
    """

    signature: int
    size: int
    version_made_by: int
    version_needed: int
    disk_num: int
    cd_disk: int
    cd_records_on_disk: int
    cd_records_total: int
    cd_size: int
    cd_offset: int


@dataclass
class Zip64Locator:
    """ZIP64 End of Central Directory Locator.

    This record points to the ZIP64 End of Central Directory record.
    """

    signature: int
    disk_num: int
    zip64_eocd_offset: int
    total_disks: int


@dataclass
class Zip64ExtraField:
    """ZIP64 extra field data.

    This structure contains 64-bit sizes and offsets when needed.
    """

    original_size: Optional[int] = None
    compressed_size: Optional[int] = None
    local_header_offset: Optional[int] = None
    disk_start: Optional[int] = None


@dataclass
class DataDescriptor:
    """Data descriptor structure.

    Used when the data descriptor flag is set in the local file header.
    Contains the actual CRC32 and sizes after the compressed data.
    """

    signature: int
    crc32: int
    compressed_size: int
    uncompressed_size: int


@dataclass
class Zip64DataDescriptor:
    """ZIP64 data descriptor structure.

    Used when ZIP64 extensions are needed for data descriptors.
    """

    signature: int
    crc32: int
    compressed_size: int
    uncompressed_size: int


@dataclass
class ZipEntry:
    """ZIP entry metadata.

    This class represents a file or directory entry in a ZIP archive,
    combining information from the central directory header and
    ZIP64 extra fields.
    """

    name: str
    is_dir: bool
    compressed_size: int
    uncompressed_size: int
    crc32: int
    compression_method: int
    flags: int
    date_time: datetime
    local_header_offset: int
    extra_field: bytes
    zip64_extra: Optional[Zip64ExtraField] = None
    comment: bytes = b""


def parse_local_file_header(f: BinaryIO) -> LocalFileHeader:
    """Parse a local file header from the current file position.

    Args:
        f: Binary file-like object positioned at the start of a local file header.

    Returns:
        LocalFileHeader object.

    Raises:
        ZipFormatError: If the signature is invalid or file is truncated.
    """
    signature = read_uint32(f)
    if signature != LOCAL_FILE_HEADER:
        raise ZipFormatError(
            f"Invalid local file header signature: 0x{signature:08X}, "
            f"expected 0x{LOCAL_FILE_HEADER:08X}"
        )

    version = read_uint16(f)
    flags = read_uint16(f)
    compression_method = read_uint16(f)
    mod_time = read_uint16(f)
    mod_date = read_uint16(f)
    crc32 = read_uint32(f)
    compressed_size = read_uint32(f)
    uncompressed_size = read_uint32(f)
    filename_len = read_uint16(f)
    extra_len = read_uint16(f)

    # Validate length fields are reasonable (prevent memory exhaustion attacks)
    # ZIP spec limits filename to 255 bytes, but we allow up to 65535 (max uint16)
    # for extra field, reasonable limit is 64KB
    if filename_len > 65535:
        raise ZipFormatError(f"Invalid filename length: {filename_len} (max 65535)")
    if extra_len > 65535:
        raise ZipFormatError(f"Invalid extra field length: {extra_len} (max 65535)")

    filename = read_exact(f, filename_len)
    extra = read_exact(f, extra_len)

    return LocalFileHeader(
        signature=signature,
        version=version,
        flags=flags,
        compression_method=compression_method,
        mod_time=mod_time,
        mod_date=mod_date,
        crc32=crc32,
        compressed_size=compressed_size,
        uncompressed_size=uncompressed_size,
        filename_len=filename_len,
        extra_len=extra_len,
        filename=filename,
        extra=extra,
    )


def parse_central_directory_header(f: BinaryIO) -> CentralDirectoryHeader:
    """Parse a central directory header from the current file position.

    Args:
        f: Binary file-like object positioned at the start of a central directory header.

    Returns:
        CentralDirectoryHeader object.

    Raises:
        ZipFormatError: If the signature is invalid or file is truncated.
    """
    signature = read_uint32(f)
    if signature != CENTRAL_DIR_HEADER:
        raise ZipFormatError(
            f"Invalid central directory header signature: 0x{signature:08X}, "
            f"expected 0x{CENTRAL_DIR_HEADER:08X}"
        )

    version_made_by = read_uint16(f)
    version = read_uint16(f)
    flags = read_uint16(f)
    compression_method = read_uint16(f)
    mod_time = read_uint16(f)
    mod_date = read_uint16(f)
    crc32 = read_uint32(f)
    compressed_size = read_uint32(f)
    uncompressed_size = read_uint32(f)
    filename_len = read_uint16(f)
    extra_len = read_uint16(f)
    comment_len = read_uint16(f)
    disk_num = read_uint16(f)
    internal_attrs = read_uint16(f)
    external_attrs = read_uint32(f)
    local_header_offset = read_uint32(f)

    # Validate length fields are reasonable (prevent memory exhaustion attacks)
    # ZIP spec limits filename to 255 bytes, but we allow up to 65535 (max uint16)
    # for extra field and comment, reasonable limit is 64KB each
    if filename_len > 65535:
        raise ZipFormatError(f"Invalid filename length: {filename_len} (max 65535)")
    if extra_len > 65535:
        raise ZipFormatError(f"Invalid extra field length: {extra_len} (max 65535)")
    if comment_len > 65535:
        raise ZipFormatError(f"Invalid comment length: {comment_len} (max 65535)")

    filename = read_exact(f, filename_len)
    extra = read_exact(f, extra_len)
    comment = read_exact(f, comment_len)

    return CentralDirectoryHeader(
        signature=signature,
        version_made_by=version_made_by,
        version=version,
        flags=flags,
        compression_method=compression_method,
        mod_time=mod_time,
        mod_date=mod_date,
        crc32=crc32,
        compressed_size=compressed_size,
        uncompressed_size=uncompressed_size,
        filename_len=filename_len,
        extra_len=extra_len,
        comment_len=comment_len,
        disk_num=disk_num,
        internal_attrs=internal_attrs,
        external_attrs=external_attrs,
        local_header_offset=local_header_offset,
        filename=filename,
        extra=extra,
        comment=comment,
    )


def parse_eocd(f: BinaryIO) -> EndOfCentralDirectory:
    """Parse an End of Central Directory record from the current file position.

    Args:
        f: Binary file-like object positioned at the start of an EOCD record.

    Returns:
        EndOfCentralDirectory object.

    Raises:
        ZipFormatError: If the signature is invalid or file is truncated.
    """
    signature = read_uint32(f)
    if signature != END_OF_CENTRAL_DIR:
        raise ZipFormatError(
            f"Invalid EOCD signature: 0x{signature:08X}, "
            f"expected 0x{END_OF_CENTRAL_DIR:08X}"
        )

    disk_num = read_uint16(f)
    cd_disk = read_uint16(f)
    cd_records_on_disk = read_uint16(f)
    cd_records_total = read_uint16(f)
    cd_size = read_uint32(f)
    cd_offset = read_uint32(f)
    comment_len = read_uint16(f)
    
    # Validate comment length is reasonable (prevent memory exhaustion attacks)
    # ZIP spec allows up to 65535 bytes for EOCD comment
    if comment_len > 65535:
        raise ZipFormatError(f"Invalid EOCD comment length: {comment_len} (max 65535)")
    
    comment = read_exact(f, comment_len)

    return EndOfCentralDirectory(
        signature=signature,
        disk_num=disk_num,
        cd_disk=cd_disk,
        cd_records_on_disk=cd_records_on_disk,
        cd_records_total=cd_records_total,
        cd_size=cd_size,
        cd_offset=cd_offset,
        comment_len=comment_len,
        comment=comment,
    )


def parse_zip64_eocd(f: BinaryIO) -> Zip64EndOfCentralDirectory:
    """Parse a ZIP64 End of Central Directory record from the current file position.

    Args:
        f: Binary file-like object positioned at the start of a ZIP64 EOCD record.

    Returns:
        Zip64EndOfCentralDirectory object.

    Raises:
        ZipFormatError: If the signature is invalid or file is truncated.
    """
    signature = read_uint32(f)
    if signature != ZIP64_END_OF_CENTRAL_DIR:
        raise ZipFormatError(
            f"Invalid ZIP64 EOCD signature: 0x{signature:08X}, "
            f"expected 0x{ZIP64_END_OF_CENTRAL_DIR:08X}"
        )

    size = read_uint64(f)
    version_made_by = read_uint16(f)
    version_needed = read_uint16(f)
    disk_num = read_uint32(f)
    cd_disk = read_uint32(f)
    cd_records_on_disk = read_uint64(f)
    cd_records_total = read_uint64(f)
    cd_size = read_uint64(f)
    cd_offset = read_uint64(f)

    return Zip64EndOfCentralDirectory(
        signature=signature,
        size=size,
        version_made_by=version_made_by,
        version_needed=version_needed,
        disk_num=disk_num,
        cd_disk=cd_disk,
        cd_records_on_disk=cd_records_on_disk,
        cd_records_total=cd_records_total,
        cd_size=cd_size,
        cd_offset=cd_offset,
    )


def parse_zip64_locator(f: BinaryIO) -> Zip64Locator:
    """Parse a ZIP64 locator from the current file position.

    Args:
        f: Binary file-like object positioned at the start of a ZIP64 locator.

    Returns:
        Zip64Locator object.

    Raises:
        ZipFormatError: If the signature is invalid or file is truncated.
    """
    signature = read_uint32(f)
    if signature != ZIP64_END_OF_CENTRAL_DIR_LOCATOR:
        raise ZipFormatError(
            f"Invalid ZIP64 locator signature: 0x{signature:08X}, "
            f"expected 0x{ZIP64_END_OF_CENTRAL_DIR_LOCATOR:08X}"
        )

    disk_num = read_uint32(f)
    zip64_eocd_offset = read_uint64(f)
    total_disks = read_uint32(f)

    return Zip64Locator(
        signature=signature,
        disk_num=disk_num,
        zip64_eocd_offset=zip64_eocd_offset,
        total_disks=total_disks,
    )


def parse_zip64_extra_field(extra_data: bytes) -> Optional[Zip64ExtraField]:
    """Parse ZIP64 extra field from extra field data.

    Args:
        extra_data: Raw extra field bytes.

    Returns:
        Zip64ExtraField object if found, None otherwise.
    """
    if len(extra_data) < 4:
        return None

    pos = 0
    while pos < len(extra_data) - 3:
        # Validate we have enough bytes for tag and size fields
        if pos + 4 > len(extra_data):
            break
        
        tag = struct.unpack("<H", extra_data[pos : pos + 2])[0]
        size = struct.unpack("<H", extra_data[pos + 2 : pos + 4])[0]
        pos += 4

        # Validate size field is reasonable (not negative, not too large)
        if size < 0 or size > len(extra_data):
            break
        
        # Validate we have enough bytes for the field data
        if pos + size > len(extra_data):
            break

        if tag == ZIP64_EXTRA_FIELD_TAG:

            field_data = extra_data[pos : pos + size]
            pos += size

            # Parse ZIP64 extra field
            # Fields are present only if the corresponding 32-bit field is 0xFFFFFFFF
            zip64_extra = Zip64ExtraField()
            field_pos = 0

            # Original size (8 bytes)
            if field_pos + 8 <= len(field_data):
                zip64_extra.original_size = struct.unpack("<Q", field_data[field_pos : field_pos + 8])[0]
                field_pos += 8

            # Compressed size (8 bytes)
            if field_pos + 8 <= len(field_data):
                zip64_extra.compressed_size = struct.unpack("<Q", field_data[field_pos : field_pos + 8])[0]
                field_pos += 8

            # Local header offset (8 bytes)
            if field_pos + 8 <= len(field_data):
                zip64_extra.local_header_offset = struct.unpack("<Q", field_data[field_pos : field_pos + 8])[0]
                field_pos += 8

            # Disk start (4 bytes)
            if field_pos + 4 <= len(field_data):
                zip64_extra.disk_start = struct.unpack("<I", field_data[field_pos : field_pos + 4])[0]

            return zip64_extra
        else:
            pos += size

    return None


def parse_data_descriptor(f: BinaryIO, is_zip64: bool = False) -> DataDescriptor | Zip64DataDescriptor:
    """Parse a data descriptor from the current file position.

    Args:
        f: Binary file-like object positioned at the start of a data descriptor.
        is_zip64: Whether this is a ZIP64 data descriptor.

    Returns:
        DataDescriptor or Zip64DataDescriptor object.

    Raises:
        ZipFormatError: If the signature is invalid or file is truncated.
    """
    signature = read_uint32(f)
    if signature != DATA_DESCRIPTOR:
        raise ZipFormatError(
            f"Invalid data descriptor signature: 0x{signature:08X}, "
            f"expected 0x{DATA_DESCRIPTOR:08X}"
        )

    crc32 = read_uint32(f)

    if is_zip64:
        compressed_size = read_uint64(f)
        uncompressed_size = read_uint64(f)
        return Zip64DataDescriptor(
            signature=signature,
            crc32=crc32,
            compressed_size=compressed_size,
            uncompressed_size=uncompressed_size,
        )
    else:
        compressed_size = read_uint32(f)
        uncompressed_size = read_uint32(f)
        return DataDescriptor(
            signature=signature,
            crc32=crc32,
            compressed_size=compressed_size,
            uncompressed_size=uncompressed_size,
        )

