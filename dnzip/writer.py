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
ZIP archive writer implementation.

This module provides the ZipWriter class for creating ZIP and ZIP64 archives.
"""

import os
import struct
import zlib
from datetime import datetime
from typing import BinaryIO, Optional

from .constants import (
    CENTRAL_DIR_HEADER,
    COMP_DEFLATE,
    COMP_STORED,
    COMPRESSION_METHODS,
    DATA_DESCRIPTOR,
    END_OF_CENTRAL_DIR,
    FLAG_DATA_DESCRIPTOR,
    FLAG_UTF8,
    LOCAL_FILE_HEADER,
    MAX_CD_OFFSET,
    MAX_CD_SIZE,
    MAX_ENTRIES,
    MAX_FILE_SIZE,
    VERSION_DEFAULT,
    VERSION_MADE_BY_DEFAULT,
    VERSION_ZIP64,
    ZIP64_END_OF_CENTRAL_DIR,
    ZIP64_END_OF_CENTRAL_DIR_LOCATOR,
    ZIP64_EXTRA_FIELD_TAG,
)
from .errors import ZipCompressionError, ZipFormatError, ZipUnsupportedFeature
from .utils import (
    crc32,
    timestamp_to_dos_datetime,
    write_uint16,
    write_uint32,
    write_uint64,
)


class ZipWriter:
    """Writer for ZIP and ZIP64 archives.

    This class provides methods to create ZIP archives, supporting both
    classic ZIP and ZIP64 formats.

    Example:
        with ZipWriter("archive.zip") as z:
            z.add_bytes("hello.txt", b"Hello, World!")
            z.add_file("doc.pdf", "/path/to/doc.pdf")
    """

    def __init__(self, file: str | BinaryIO, mode: str = "w"):
        """Initialize ZipWriter with a file path or file-like object.

        Args:
            file: Path to ZIP file (str, Path, or pathlib.Path) or binary file-like object opened for writing.
            mode: File mode (currently only "w" is supported).

        Raises:
            ZipFormatError: If the file cannot be opened.
        """
        # Validate mode parameter
        if mode != "w":
            raise ZipFormatError(f"Unsupported mode: {mode} (only 'w' is supported)")
        
        # Handle Path objects
        if hasattr(file, '__fspath__'):  # Path-like object (pathlib.Path)
            file = str(file)
        
        if isinstance(file, str):
            self._file = open(file, "wb")
            self._should_close = True
        else:
            # Validate file-like object has required methods
            if not hasattr(file, 'write'):
                raise ZipFormatError("File-like object must have a write() method")
            if not hasattr(file, 'seek'):
                raise ZipFormatError("File-like object must have a seek() method")
            if not hasattr(file, 'tell'):
                raise ZipFormatError("File-like object must have a tell() method")
            self._file = file
            self._should_close = False

        self._pending_entries: list[dict] = []
        self._current_offset: int = 0
        self._closed: bool = False
        self._needs_zip64: bool = False

    def _compress_data(self, data: bytes, method: str) -> bytes:
        """Compress data using the specified method.

        Args:
            data: Data to compress.
            method: Compression method name ("stored", "deflate", etc.).

        Returns:
            Compressed data as bytes.

        Raises:
            ZipUnsupportedFeature: If compression method is not supported.
        """
        if method not in COMPRESSION_METHODS:
            raise ZipUnsupportedFeature(f"Unsupported compression method: {method}")

        comp_method = COMPRESSION_METHODS[method]

        if comp_method == COMP_STORED:
            return data
        elif comp_method == COMP_DEFLATE:
            try:
                compressor = zlib.compressobj(level=zlib.Z_DEFAULT_COMPRESSION, wbits=-zlib.MAX_WBITS)
                compressed = compressor.compress(data)
                compressed += compressor.flush()
                return compressed
            except Exception as e:
                raise ZipCompressionError(f"Deflate compression failed: {e}") from e
        else:
            raise ZipUnsupportedFeature(f"Compression method {method} not yet implemented")

    def _needs_zip64_for_entry(self, entry_info: dict) -> bool:
        """Check if an entry needs ZIP64 extensions.

        Args:
            entry_info: Entry information dictionary.

        Returns:
            True if ZIP64 is needed for this entry.
        """
        compressed_size = len(entry_info["compressed_data"])
        uncompressed_size = len(entry_info["data"])

        # Validate sizes are non-negative
        if compressed_size < 0 or uncompressed_size < 0:
            raise ZipFormatError(f"Invalid entry sizes: compressed={compressed_size}, uncompressed={uncompressed_size}")

        return (
            uncompressed_size > MAX_FILE_SIZE
            or compressed_size > MAX_FILE_SIZE
            or entry_info["local_header_offset"] > MAX_FILE_SIZE
        )

    def _write_zip64_extra_field(
        self, uncompressed_size: int, compressed_size: int, local_header_offset: int
    ) -> bytes:
        """Write ZIP64 extra field data.

        Args:
            uncompressed_size: Uncompressed size (64-bit).
            compressed_size: Compressed size (64-bit).
            local_header_offset: Local header offset (64-bit).

        Returns:
            ZIP64 extra field as bytes.
        """
        # ZIP64 extra field contains:
        # - Original size (8 bytes)
        # - Compressed size (8 bytes)
        # - Local header offset (8 bytes)
        # - Disk start number (4 bytes) - not needed for single-disk archives

        field_data = bytearray()
        field_data.extend(struct.pack("<Q", uncompressed_size))
        field_data.extend(struct.pack("<Q", compressed_size))
        field_data.extend(struct.pack("<Q", local_header_offset))

        # Write tag and size
        extra_field = bytearray()
        extra_field.extend(struct.pack("<H", ZIP64_EXTRA_FIELD_TAG))
        extra_field.extend(struct.pack("<H", len(field_data)))
        extra_field.extend(field_data)

        return bytes(extra_field)

    def _write_local_file_header(self, entry_info: dict) -> None:
        """Write a local file header.

        Args:
            entry_info: Dictionary containing entry information:
                - name: Entry name (str)
                - data: Entry data (bytes)
                - compressed_data: Compressed data (bytes)
                - crc32: CRC32 checksum (int)
                - compression_method: Compression method ID (int)
                - mod_time: Modification time (int, DOS time)
                - mod_date: Modification date (int, DOS date)
                - flags: General purpose bit flags (int)
        """
        if self._closed:
            raise ZipFormatError("Archive is closed")
        if self._file is None:
            raise ZipFormatError("Archive file is closed")
        
        # Encode filename (already validated in add_bytes)
        name_bytes = entry_info["name"].encode("utf-8")
        filename_len = len(name_bytes)

        # Check if ZIP64 is needed for this entry
        needs_zip64 = self._needs_zip64_for_entry(entry_info)
        if needs_zip64:
            self._needs_zip64 = True

        # Create ZIP64 extra field if needed
        compressed_size = len(entry_info["compressed_data"])
        uncompressed_size = len(entry_info["data"])

        if needs_zip64:
            zip64_extra = self._write_zip64_extra_field(
                uncompressed_size, compressed_size, entry_info["local_header_offset"]
            )
            extra_len = len(zip64_extra)
            # Store 0xFFFFFFFF in 32-bit fields to indicate ZIP64
            stored_compressed_size = MAX_FILE_SIZE
            stored_uncompressed_size = MAX_FILE_SIZE
        else:
            zip64_extra = b""
            extra_len = 0
            stored_compressed_size = compressed_size
            stored_uncompressed_size = uncompressed_size

        # Write local file header signature
        write_uint32(self._file, LOCAL_FILE_HEADER)

        # Version needed to extract (ZIP64 if needed)
        version = VERSION_ZIP64 if needs_zip64 else VERSION_DEFAULT
        write_uint16(self._file, version)

        # General purpose bit flags
        flags = entry_info.get("flags", FLAG_UTF8)  # Use UTF-8 flag for UTF-8 filenames
        write_uint16(self._file, flags)

        # Compression method
        write_uint16(self._file, entry_info["compression_method"])

        # Modification time and date
        write_uint16(self._file, entry_info["mod_time"])
        write_uint16(self._file, entry_info["mod_date"])

        # CRC32
        write_uint32(self._file, entry_info["crc32"])

        # Compressed and uncompressed sizes (may be 0xFFFFFFFF for ZIP64)
        write_uint32(self._file, stored_compressed_size)
        write_uint32(self._file, stored_uncompressed_size)

        # Filename length and extra field length
        write_uint16(self._file, filename_len)
        write_uint16(self._file, extra_len)

        # Filename
        written = self._file.write(name_bytes)
        if written != len(name_bytes):
            raise ZipFormatError(f"Write operation failed: expected to write {len(name_bytes)} bytes, wrote {written} bytes")

        # Extra field (ZIP64 if needed)
        if zip64_extra:
            written = self._file.write(zip64_extra)
            if written != len(zip64_extra):
                raise ZipFormatError(f"Write operation failed: expected to write {len(zip64_extra)} bytes, wrote {written} bytes")

        # Update current offset (header size + data will be written after)
        self._current_offset += 30 + filename_len + extra_len

    def _write_central_directory(self) -> tuple[int, int]:
        """Write the central directory containing all entry headers.

        Returns:
            Tuple of (cd_offset, cd_size).
        """
        if self._closed:
            raise ZipFormatError("Archive is closed")
        if self._file is None:
            raise ZipFormatError("Archive file is closed")
        
        cd_start_offset = self._current_offset

        for entry_info in self._pending_entries:
            name_bytes = entry_info["name"].encode("utf-8")
            filename_len = len(name_bytes)
            comment_len = 0  # No comment

            # Check if ZIP64 is needed for this entry
            needs_zip64 = self._needs_zip64_for_entry(entry_info)
            if needs_zip64:
                self._needs_zip64 = True

            # Create ZIP64 extra field if needed
            compressed_size = len(entry_info["compressed_data"])
            uncompressed_size = len(entry_info["data"])

            if needs_zip64:
                zip64_extra = self._write_zip64_extra_field(
                    uncompressed_size, compressed_size, entry_info["local_header_offset"]
                )
                extra_len = len(zip64_extra)
                stored_compressed_size = MAX_FILE_SIZE
                stored_uncompressed_size = MAX_FILE_SIZE
                stored_local_header_offset = MAX_FILE_SIZE
            else:
                zip64_extra = b""
                extra_len = 0
                stored_compressed_size = compressed_size
                stored_uncompressed_size = uncompressed_size
                stored_local_header_offset = entry_info["local_header_offset"]

            # Write central directory header signature
            write_uint32(self._file, CENTRAL_DIR_HEADER)

            # Version made by
            write_uint16(self._file, VERSION_MADE_BY_DEFAULT)

            # Version needed to extract (ZIP64 if needed)
            version = VERSION_ZIP64 if needs_zip64 else VERSION_DEFAULT
            write_uint16(self._file, version)

            # General purpose bit flags
            flags = entry_info.get("flags", FLAG_UTF8)
            write_uint16(self._file, flags)

            # Compression method
            write_uint16(self._file, entry_info["compression_method"])

            # Modification time and date
            write_uint16(self._file, entry_info["mod_time"])
            write_uint16(self._file, entry_info["mod_date"])

            # CRC32
            write_uint32(self._file, entry_info["crc32"])

            # Compressed and uncompressed sizes (may be 0xFFFFFFFF for ZIP64)
            write_uint32(self._file, stored_compressed_size)
            write_uint32(self._file, stored_uncompressed_size)

            # Filename length, extra field length, comment length
            write_uint16(self._file, filename_len)
            write_uint16(self._file, extra_len)
            write_uint16(self._file, comment_len)

            # Disk number (0 for single-disk archives)
            write_uint16(self._file, 0)

            # Internal file attributes
            write_uint16(self._file, 0)

            # External file attributes
            # For files: 0o100644 (regular file, rw-r--r--)
            # For directories: 0o040755 (directory, rwxr-xr-x)
            # Note: name is already normalized to use forward slashes
            if entry_info["name"].endswith("/"):
                external_attrs = 0o040755 << 16  # Directory
            else:
                external_attrs = 0o100644 << 16  # Regular file
            write_uint32(self._file, external_attrs)

            # Local header offset (may be 0xFFFFFFFF for ZIP64)
            write_uint32(self._file, stored_local_header_offset)

            # Filename
            written = self._file.write(name_bytes)
            if written != len(name_bytes):
                raise ZipFormatError(f"Write operation failed: expected to write {len(name_bytes)} bytes, wrote {written} bytes")

            # Extra field (ZIP64 if needed)
            if zip64_extra:
                written = self._file.write(zip64_extra)
                if written != len(zip64_extra):
                    raise ZipFormatError(f"Write operation failed: expected to write {len(zip64_extra)} bytes, wrote {written} bytes")

            # Comment (none)

            # Update current offset
            self._current_offset += 46 + filename_len + extra_len + comment_len

        cd_size = self._current_offset - cd_start_offset
        return cd_start_offset, cd_size

    def _check_needs_zip64(self, cd_offset: int, cd_size: int) -> bool:
        """Check if ZIP64 is needed for the archive.

        Args:
            cd_offset: Central directory offset.
            cd_size: Central directory size.

        Returns:
            True if ZIP64 is needed.
        """
        num_entries = len(self._pending_entries)

        return (
            self._needs_zip64
            or num_entries > MAX_ENTRIES
            or cd_size > MAX_CD_SIZE
            or cd_offset > MAX_CD_OFFSET
        )

    def _write_zip64_eocd(self, cd_offset: int, cd_size: int) -> None:
        """Write ZIP64 End of Central Directory record.

        Args:
            cd_offset: Central directory offset (64-bit).
            cd_size: Central directory size (64-bit).
        """
        if self._file is None:
            raise ZipFormatError("Archive file is closed")
        
        num_entries = len(self._pending_entries)

        # ZIP64 EOCD size (fixed part is 56 bytes, but size field excludes signature and size itself)
        zip64_eocd_size = 56 - 12  # 44 bytes (56 - 4 (signature) - 8 (size field))

        # Write ZIP64 EOCD signature
        write_uint32(self._file, ZIP64_END_OF_CENTRAL_DIR)

        # Size of ZIP64 EOCD record (excluding signature and size field)
        write_uint64(self._file, zip64_eocd_size)

        # Version made by
        write_uint16(self._file, VERSION_MADE_BY_DEFAULT)

        # Version needed to extract
        write_uint16(self._file, VERSION_ZIP64)

        # Disk numbers (0 for single-disk archives)
        write_uint32(self._file, 0)  # Number of this disk
        write_uint32(self._file, 0)  # Disk with start of central directory

        # Number of entries (64-bit)
        write_uint64(self._file, num_entries)  # Entries on this disk
        write_uint64(self._file, num_entries)  # Total entries

        # Central directory size and offset (64-bit)
        write_uint64(self._file, cd_size)
        write_uint64(self._file, cd_offset)

    def _write_zip64_locator(self, zip64_eocd_offset: int) -> None:
        """Write ZIP64 End of Central Directory Locator.

        Args:
            zip64_eocd_offset: Offset of ZIP64 EOCD from start of file.
        """
        if self._file is None:
            raise ZipFormatError("Archive file is closed")
        
        # Write ZIP64 locator signature
        write_uint32(self._file, ZIP64_END_OF_CENTRAL_DIR_LOCATOR)

        # Disk number with ZIP64 EOCD (0 for single-disk archives)
        write_uint32(self._file, 0)

        # Offset of ZIP64 EOCD
        write_uint64(self._file, zip64_eocd_offset)

        # Total number of disks (1 for single-disk archives)
        write_uint32(self._file, 1)

    def _write_eocd(self, cd_offset: int, cd_size: int) -> None:
        """Write the End of Central Directory record.

        If ZIP64 is needed, also writes ZIP64 EOCD and locator.

        Args:
            cd_offset: Offset of central directory from start of file.
            cd_size: Size of central directory in bytes.
        """
        if self._closed:
            raise ZipFormatError("Archive is closed")
        if self._file is None:
            raise ZipFormatError("Archive file is closed")
        
        num_entries = len(self._pending_entries)

        # Check if ZIP64 is needed
        needs_zip64 = self._check_needs_zip64(cd_offset, cd_size)

        if needs_zip64:
            # Write ZIP64 EOCD first
            zip64_eocd_offset = self._current_offset
            self._write_zip64_eocd(cd_offset, cd_size)
            self._current_offset += 56  # ZIP64 EOCD size

            # Write ZIP64 locator
            zip64_locator_offset = self._current_offset
            self._write_zip64_locator(zip64_eocd_offset)
            self._current_offset += 20  # ZIP64 locator size

            # Write classic EOCD with 0xFFFF/0xFFFFFFFF values
            write_uint32(self._file, END_OF_CENTRAL_DIR)
            write_uint16(self._file, 0)  # Number of this disk
            write_uint16(self._file, 0)  # Disk with start of central directory
            write_uint16(self._file, MAX_ENTRIES)  # Entries on this disk (0xFFFF)
            write_uint16(self._file, MAX_ENTRIES)  # Total entries (0xFFFF)
            write_uint32(self._file, MAX_CD_SIZE)  # CD size (0xFFFFFFFF)
            write_uint32(self._file, MAX_CD_OFFSET)  # CD offset (0xFFFFFFFF)
            write_uint16(self._file, 0)  # Comment length
        else:
            # Write classic EOCD
            write_uint32(self._file, END_OF_CENTRAL_DIR)
            write_uint16(self._file, 0)  # Number of this disk
            write_uint16(self._file, 0)  # Disk with start of central directory
            write_uint16(self._file, num_entries)  # Entries on this disk
            write_uint16(self._file, num_entries)  # Total entries
            write_uint32(self._file, cd_size)  # CD size
            write_uint32(self._file, cd_offset)  # CD offset
            write_uint16(self._file, 0)  # Comment length

    def _write_data_descriptor(
        self, crc32_val: int, compressed_size: int, uncompressed_size: int, is_zip64: bool = False
    ) -> None:
        """Write a data descriptor after compressed data.

        Args:
            crc32_val: CRC32 checksum.
            compressed_size: Compressed size.
            uncompressed_size: Uncompressed size.
            is_zip64: Whether to write ZIP64 data descriptor.
        """
        # Write data descriptor signature
        write_uint32(self._file, DATA_DESCRIPTOR)

        # CRC32
        write_uint32(self._file, crc32_val)

        if is_zip64:
            # ZIP64 data descriptor (64-bit sizes)
            write_uint64(self._file, compressed_size)
            write_uint64(self._file, uncompressed_size)
            self._current_offset += 24  # ZIP64 data descriptor size: 4 (sig) + 4 (crc) + 8 + 8 = 24
        else:
            # Classic data descriptor (32-bit sizes)
            write_uint32(self._file, compressed_size)
            write_uint32(self._file, uncompressed_size)
            self._current_offset += 16  # Classic data descriptor size: 4 (sig) + 4 (crc) + 4 + 4 = 16

    def add_bytes(
        self, name: str, data: bytes, compression: str = "deflate", use_data_descriptor: bool = False
    ) -> None:
        """Add an entry from bytes data.

        Args:
            name: Entry name (path within ZIP archive).
            data: Data to add as bytes.
            compression: Compression method ("stored", "deflate", etc.).
            use_data_descriptor: If True, use data descriptor (for streaming).

        Raises:
            ZipFormatError: If the archive is closed.
            ZipUnsupportedFeature: If compression method is not supported.
        """
        if self._closed:
            raise ZipFormatError("Archive is closed")

        # Normalize path separators (use forward slash)
        if "\\" in name:
            name = name.replace("\\", "/")

        # Encode filename to check length
        name_bytes = name.encode("utf-8")
        
        # Validate filename length (ZIP spec limits to 255 bytes)
        # Note: Some ZIP implementations are more lenient, but we enforce the spec
        if len(name_bytes) > 255:
            raise ZipFormatError(f"Entry name too long: {len(name_bytes)} bytes (max 255 bytes per ZIP specification)")
        
        # Validate filename is not empty
        if not name:
            raise ZipFormatError("Entry name cannot be empty")
        
        # Validate filename doesn't contain null bytes (security/robustness)
        if "\x00" in name:
            raise ZipFormatError("Entry name cannot contain null bytes")

        # Calculate CRC32
        entry_crc32 = crc32(data)

        # Compress data
        compressed_data = self._compress_data(data, compression)
        compression_method = COMPRESSION_METHODS[compression]

        # Get current time for modification date
        now = datetime.now()
        mod_date, mod_time = timestamp_to_dos_datetime(now)

        # Store local header offset
        local_header_offset = self._current_offset

        # Determine if ZIP64 is needed
        compressed_size = len(compressed_data)
        uncompressed_size = len(data)
        needs_zip64 = (
            uncompressed_size > MAX_FILE_SIZE
            or compressed_size > MAX_FILE_SIZE
            or local_header_offset > MAX_FILE_SIZE
        )

        # Set flags
        flags = FLAG_UTF8  # UTF-8 encoding
        if use_data_descriptor:
            flags |= FLAG_DATA_DESCRIPTOR

        # Write local file header
        entry_info = {
            "name": name,
            "data": data,
            "compressed_data": compressed_data,
            "crc32": entry_crc32,
            "compression_method": compression_method,
            "mod_time": mod_time,
            "mod_date": mod_date,
            "flags": flags,
            "local_header_offset": local_header_offset,
            "use_data_descriptor": use_data_descriptor,
            "needs_zip64": needs_zip64,
        }
        
        # Note: name_bytes already calculated above for validation

        if use_data_descriptor:
            # Write local header with zero sizes
            self._write_local_file_header_with_data_descriptor(entry_info)
        else:
            self._write_local_file_header(entry_info)

        # Write compressed data
        if self._file is None:
            raise ZipFormatError("Archive file is closed")
        
        written = self._file.write(compressed_data)
        if written != len(compressed_data):
            raise ZipFormatError(f"Write operation failed: expected to write {len(compressed_data)} bytes, wrote {written} bytes")
        self._current_offset += len(compressed_data)

        # Write data descriptor if needed
        if use_data_descriptor:
            self._write_data_descriptor(entry_crc32, compressed_size, uncompressed_size, needs_zip64)
            # Note: _write_data_descriptor already updates _current_offset

        # Store entry info for central directory
        self._pending_entries.append(entry_info)

    def _write_local_file_header_with_data_descriptor(self, entry_info: dict) -> None:
        """Write a local file header with data descriptor flag and zero sizes.

        Args:
            entry_info: Entry information dictionary.
        """
        name_bytes = entry_info["name"].encode("utf-8")
        filename_len = len(name_bytes)

        needs_zip64 = entry_info.get("needs_zip64", False)
        if needs_zip64:
            self._needs_zip64 = True

        # For data descriptors, we don't include ZIP64 extra in local header
        # (sizes are in data descriptor instead)
        # But we might need it for local header offset if > 4GB
        local_header_offset = entry_info["local_header_offset"]
        if local_header_offset > MAX_FILE_SIZE:
            # Need ZIP64 extra for local header offset only
            zip64_extra = self._write_zip64_extra_field(0, 0, local_header_offset)
            extra_len = len(zip64_extra)
        else:
            zip64_extra = b""
            extra_len = 0

        # Write local file header signature
        write_uint32(self._file, LOCAL_FILE_HEADER)

        # Version needed to extract (ZIP64 if needed)
        version = VERSION_ZIP64 if needs_zip64 else VERSION_DEFAULT
        write_uint16(self._file, version)

        # General purpose bit flags (with DATA_DESCRIPTOR flag)
        flags = entry_info.get("flags", FLAG_UTF8 | FLAG_DATA_DESCRIPTOR)
        write_uint16(self._file, flags)

        # Compression method
        write_uint16(self._file, entry_info["compression_method"])

        # Modification time and date
        write_uint16(self._file, entry_info["mod_time"])
        write_uint16(self._file, entry_info["mod_date"])

        # CRC32 (zero for data descriptor)
        write_uint32(self._file, 0)

        # Compressed and uncompressed sizes (zero for data descriptor)
        write_uint32(self._file, 0)
        write_uint32(self._file, 0)

        # Filename length and extra field length
        write_uint16(self._file, filename_len)
        write_uint16(self._file, extra_len)

        # Filename
        written = self._file.write(name_bytes)
        if written != len(name_bytes):
            raise ZipFormatError(f"Write operation failed: expected to write {len(name_bytes)} bytes, wrote {written} bytes")

        # Extra field (ZIP64 if needed for local header offset)
        if zip64_extra:
            written = self._file.write(zip64_extra)
            if written != len(zip64_extra):
                raise ZipFormatError(f"Write operation failed: expected to write {len(zip64_extra)} bytes, wrote {written} bytes")

        # Update current offset
        self._current_offset += 30 + filename_len + extra_len

    def add_stream(
        self, name: str, stream: BinaryIO, compression: str = "deflate"
    ) -> None:
        """Add an entry from a stream (unknown size).

        Args:
            name: Entry name (path within ZIP archive).
            stream: Binary file-like object to read from.
            compression: Compression method ("stored", "deflate", etc.).

        Raises:
            ZipFormatError: If the archive is closed or stream cannot be read.
            ZipUnsupportedFeature: If compression method is not supported.
        """
        if self._closed:
            raise ZipFormatError("Archive is closed")
        
        # Validate stream has read method
        if not hasattr(stream, 'read'):
            raise ZipFormatError("Stream object must have a read() method")
        
        # Read all data from stream (for now - true streaming would require
        # more complex implementation with on-the-fly compression)
        try:
            data = stream.read()
        except Exception as e:
            raise ZipFormatError(f"Failed to read from stream: {e}") from e
        
        self.add_bytes(name, data, compression, use_data_descriptor=True)

    def add_file(
        self, name_in_zip: str, source_path: str, compression: str = "deflate"
    ) -> None:
        """Add an entry from a file on disk.

        Args:
            name_in_zip: Entry name (path within ZIP archive).
            source_path: Path to source file on disk.
            compression: Compression method ("stored", "deflate", etc.).

        Raises:
            ZipFormatError: If the source file cannot be read.
            ZipUnsupportedFeature: If compression method is not supported.
        """
        if self._closed:
            raise ZipFormatError("Archive is closed")
        
        if not os.path.exists(source_path):
            raise ZipFormatError(f"Source file not found: {source_path}")

        try:
            with open(source_path, "rb") as f:
                data = f.read()
        except PermissionError as e:
            raise ZipFormatError(f"Permission denied reading file: {source_path}") from e
        except OSError as e:
            raise ZipFormatError(f"Error reading file {source_path}: {e}") from e

        self.add_bytes(name_in_zip, data, compression)

    def close(self) -> None:
        """Write central directory and EOCD, then close the archive."""
        if self._closed:
            return

        # Ensure file is closed even if writing fails
        # Also ensure _closed is set to True even if writing fails
        try:
            # Write central directory
            cd_offset, cd_size = self._write_central_directory()

            # Write End of Central Directory
            self._write_eocd(cd_offset, cd_size)
        finally:
            # Always close file if we opened it, even if writing failed
            if self._should_close and self._file:
                try:
                    self._file.close()
                except Exception:
                    pass  # Ignore errors during cleanup
                self._file = None
            
            # Always mark as closed, even if writing failed
            # This prevents further operations on an incomplete archive
            self._closed = True

    def __enter__(self) -> "ZipWriter":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        self.close()
