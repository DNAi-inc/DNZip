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
ZIP archive reader implementation.

This module provides the ZipReader class for reading ZIP and ZIP64 archives.
"""

import io
import zlib
from typing import BinaryIO, Optional

from .constants import COMP_STORED, COMP_DEFLATE, COMPRESSION_STORED, COMPRESSION_DEFLATE, FLAG_DATA_DESCRIPTOR, FLAG_ENCRYPTED
from .errors import ZipCompressionError, ZipCrcError, ZipFormatError, ZipUnsupportedFeature
from .structures import (
    EndOfCentralDirectory,
    LocalFileHeader,
    Zip64EndOfCentralDirectory,
    Zip64Locator,
    ZipEntry,
    parse_central_directory_header,
    parse_data_descriptor,
    parse_eocd,
    parse_local_file_header,
    parse_zip64_eocd,
    parse_zip64_locator,
    parse_zip64_extra_field,
)
from .utils import crc32, read_exact


class ZipReader:
    """Reader for ZIP and ZIP64 archives.

    This class provides methods to read and extract files from ZIP archives,
    supporting both classic ZIP and ZIP64 formats.

    Example:
        with ZipReader("archive.zip") as z:
            print(z.list())
            data = z.open("file.txt").read()
    """

    def __init__(self, file: str | BinaryIO):
        """Initialize ZipReader with a file path or file-like object.

        Args:
            file: Path to ZIP file (str, Path, or pathlib.Path) or binary file-like object.

        Raises:
            ZipFormatError: If the file cannot be opened or is not a valid ZIP.
        """
        # Handle Path objects
        if hasattr(file, '__fspath__'):  # Path-like object (pathlib.Path)
            file = str(file)
        
        if isinstance(file, str):
            self._file = open(file, "rb")
            self._should_close = True
        else:
            # Validate file-like object has required methods
            if not hasattr(file, 'read'):
                raise ZipFormatError("File-like object must have a read() method")
            if not hasattr(file, 'seek'):
                raise ZipFormatError("File-like object must have a seek() method")
            if not hasattr(file, 'tell'):
                raise ZipFormatError("File-like object must have a tell() method")
            self._file = file
            self._should_close = False

        self._entries: dict[str, ZipEntry] = {}
        self._eocd: Optional[EndOfCentralDirectory] = None
        self._zip64_eocd: Optional[Zip64EndOfCentralDirectory] = None
        self._zip64_locator: Optional[Zip64Locator] = None
        self._closed: bool = False

        # Parse the archive
        # If parsing fails, ensure file is closed if we opened it
        try:
            self._parse_archive()
        except Exception:
            # Close file on error if we opened it
            if self._should_close and self._file:
                try:
                    self._file.close()
                except Exception:
                    pass  # Ignore errors during cleanup
                self._file = None
            raise

    def _find_eocd(self) -> EndOfCentralDirectory:
        """Find and parse the End of Central Directory record.

        Scans backward from the end of the file to find the EOCD signature.
        The EOCD can be preceded by up to 65535 bytes of comment.

        Returns:
            EndOfCentralDirectory object.

        Raises:
            ZipFormatError: If EOCD cannot be found.
        """
        if self._file is None:
            raise ZipFormatError("Archive file is closed")
        
        # Get file size
        self._file.seek(0, io.SEEK_END)
        file_size = self._file.tell()

        # EOCD is at least 22 bytes, and comment can be up to 65535 bytes
        # So we need to scan back at most 65535 + 22 = 65557 bytes
        max_scan = min(65557, file_size)

        # Read the last chunk
        self._file.seek(max(0, file_size - max_scan))
        data = self._file.read()

        # Search backward for EOCD signature (0x06054B50)
        eocd_pos = data.rfind(b"PK\x05\x06")
        if eocd_pos == -1:
            raise ZipFormatError("End of Central Directory record not found")

        # Calculate absolute position
        absolute_pos = file_size - len(data) + eocd_pos

        # Check for ZIP64 locator (it comes right before EOCD)
        # ZIP64 locator is 20 bytes, so check if we have room for it
        if absolute_pos >= 20:
            locator_pos = absolute_pos - 20
            self._file.seek(locator_pos)
            try:
                locator = parse_zip64_locator(self._file)
                self._zip64_locator = locator
                # Find ZIP64 EOCD
                self._zip64_eocd = self._find_zip64_eocd()
            except ZipFormatError:
                # No ZIP64 locator, this is a classic ZIP
                pass

        # Parse EOCD
        self._file.seek(absolute_pos)
        eocd = parse_eocd(self._file)

        return eocd

    def _find_zip64_eocd(self) -> Zip64EndOfCentralDirectory:
        """Find and parse the ZIP64 End of Central Directory record.

        Returns:
            Zip64EndOfCentralDirectory object.

        Raises:
            ZipFormatError: If ZIP64 EOCD cannot be found.
        """
        if self._file is None:
            raise ZipFormatError("Archive file is closed")
        
        if not self._zip64_locator:
            raise ZipFormatError("ZIP64 locator not found")

        # Validate ZIP64 EOCD offset is within file bounds
        zip64_eocd_offset = self._zip64_locator.zip64_eocd_offset
        self._file.seek(0, io.SEEK_END)
        file_size = self._file.tell()
        
        if zip64_eocd_offset < 0 or zip64_eocd_offset >= file_size:
            raise ZipFormatError(
                f"Invalid ZIP64 EOCD offset: {zip64_eocd_offset} (file size: {file_size})"
            )

        # Seek to ZIP64 EOCD position
        self._file.seek(zip64_eocd_offset)
        return parse_zip64_eocd(self._file)

    def _parse_central_directory(self) -> list[ZipEntry]:
        """Parse the central directory and build ZipEntry objects.

        Returns:
            List of ZipEntry objects.

        Raises:
            ZipFormatError: If the central directory cannot be parsed.
        """
        if self._file is None:
            raise ZipFormatError("Archive file is closed")
        
        if not self._eocd:
            raise ZipFormatError("EOCD not found")

        entries = []

        # Use ZIP64 values if available, otherwise use classic EOCD values
        if self._zip64_eocd:
            cd_offset = self._zip64_eocd.cd_offset
            cd_size = self._zip64_eocd.cd_size
            num_entries = self._zip64_eocd.cd_records_total
        else:
            cd_offset = self._eocd.cd_offset
            cd_size = self._eocd.cd_size
            num_entries = self._eocd.cd_records_total

        # Validate entry count is reasonable (prevent denial of service)
        if num_entries < 0:
            raise ZipFormatError(f"Invalid entry count: {num_entries} (must be non-negative)")
        # Reasonable limit: 10 million entries (prevents excessive memory usage)
        if num_entries > 10_000_000:
            raise ZipFormatError(f"Entry count too large: {num_entries} (max 10,000,000)")

        # Validate central directory offset is within file bounds
        self._file.seek(0, io.SEEK_END)
        file_size = self._file.tell()
        
        if cd_offset < 0 or cd_offset >= file_size:
            raise ZipFormatError(
                f"Invalid central directory offset: {cd_offset} (file size: {file_size})"
            )
        
        if cd_offset + cd_size > file_size:
            raise ZipFormatError(
                f"Central directory extends beyond file: offset {cd_offset}, size {cd_size} (file size: {file_size})"
            )

        # Seek to central directory
        self._file.seek(cd_offset)

        # Parse each central directory entry
        for _ in range(num_entries):
            cd_header = parse_central_directory_header(self._file)

            # Decode filename (assume UTF-8 if flag is set, otherwise try UTF-8 and fall back to CP437)
            flags = cd_header.flags
            if flags & 0x0800:  # UTF-8 flag
                filename = cd_header.filename.decode("utf-8", errors="replace")
            else:
                try:
                    filename = cd_header.filename.decode("utf-8", errors="replace")
                except UnicodeDecodeError:
                    # Fall back to CP437 or latin-1
                    filename = cd_header.filename.decode("latin-1", errors="replace")

            # Normalize path separators (use forward slash for consistency)
            # This matches the writer's behavior and ensures consistent entry names
            if "\\" in filename:
                filename = filename.replace("\\", "/")

            # Check if directory (trailing slash or external attributes)
            is_dir = filename.endswith("/")
            if not is_dir:
                # Check external attributes (Unix: 0o040000 = directory)
                is_dir = (cd_header.external_attrs >> 16) & 0o040000 != 0

            # Parse ZIP64 extra field if present
            zip64_extra = parse_zip64_extra_field(cd_header.extra)

            # Determine actual sizes and offset
            if zip64_extra:
                uncompressed_size = (
                    zip64_extra.original_size
                    if zip64_extra.original_size is not None
                    else cd_header.uncompressed_size
                )
                compressed_size = (
                    zip64_extra.compressed_size
                    if zip64_extra.compressed_size is not None
                    else cd_header.compressed_size
                )
                local_header_offset = (
                    zip64_extra.local_header_offset
                    if zip64_extra.local_header_offset is not None
                    else cd_header.local_header_offset
                )
            else:
                uncompressed_size = cd_header.uncompressed_size
                compressed_size = cd_header.compressed_size
                local_header_offset = cd_header.local_header_offset

            entry = ZipEntry(
                name=filename,
                is_dir=is_dir,
                compressed_size=compressed_size,
                uncompressed_size=uncompressed_size,
                crc32=cd_header.crc32,
                compression_method=cd_header.compression_method,
                flags=cd_header.flags,
                date_time=cd_header.date_time,
                local_header_offset=local_header_offset,
                extra_field=cd_header.extra,
                zip64_extra=zip64_extra,
                comment=cd_header.comment,
            )

            entries.append(entry)
            self._entries[filename] = entry

        # Validate that we parsed the expected number of entries
        # This helps detect corrupted central directories where parsing might fail silently
        if len(entries) != num_entries:
            raise ZipFormatError(
                f"Entry count mismatch: expected {num_entries} entries, parsed {len(entries)} entries"
            )

        return entries

    def _parse_archive(self) -> None:
        """Parse the entire archive structure."""
        self._eocd = self._find_eocd()
        self._parse_central_directory()

    def _decompress_entry(self, entry: ZipEntry) -> bytes:
        """Decompress a ZIP entry's data.

        Args:
            entry: ZipEntry object with entry metadata.

        Returns:
            Decompressed data as bytes.

        Raises:
            ZipUnsupportedFeature: If compression method is not supported or entry is encrypted.
            ZipCompressionError: If decompression fails.
        """
        if self._file is None:
            raise ZipFormatError("Archive file is closed")
        
        # Check if entry is encrypted (not supported)
        if entry.flags & FLAG_ENCRYPTED:
            raise ZipUnsupportedFeature(f"Entry '{entry.name}' is encrypted (encryption not supported)")
        
        # Validate local header offset is within file bounds
        local_header_offset = entry.local_header_offset
        self._file.seek(0, io.SEEK_END)
        file_size = self._file.tell()
        
        if local_header_offset < 0 or local_header_offset >= file_size:
            raise ZipFormatError(
                f"Invalid local header offset for entry '{entry.name}': {local_header_offset} (file size: {file_size})"
            )
        
        # Seek to local file header
        self._file.seek(local_header_offset)
        local_header = parse_local_file_header(self._file)

        # Check if data descriptor flag is set
        has_data_descriptor = (local_header.flags & FLAG_DATA_DESCRIPTOR) != 0

        # Parse ZIP64 extra field from local header if present
        local_zip64_extra = parse_zip64_extra_field(local_header.extra)

        # Determine compressed size
        if has_data_descriptor:
            # With data descriptor, local header has zero sizes
            # Use sizes from central directory (which should have correct values)
            actual_compressed_size = entry.compressed_size
        elif local_zip64_extra and local_zip64_extra.compressed_size is not None:
            # Use ZIP64 size from local header
            actual_compressed_size = local_zip64_extra.compressed_size
        else:
            # Use size from local header
            actual_compressed_size = local_header.compressed_size
            # If local header size is 0xFFFFFFFF, use entry size (might be ZIP64)
            if actual_compressed_size == 0xFFFFFFFF:
                actual_compressed_size = entry.compressed_size

        # Validate compressed size is within file bounds
        current_pos = self._file.tell()
        if current_pos + actual_compressed_size > file_size:
            raise ZipFormatError(
                f"Compressed data extends beyond file for entry '{entry.name}': "
                f"position {current_pos}, size {actual_compressed_size} (file size: {file_size})"
            )

        # Read compressed data
        compressed_data = read_exact(self._file, actual_compressed_size)

        # If data descriptor is present, read it (but we already have sizes from CD)
        # The data descriptor comes after the compressed data
        if has_data_descriptor:
            # Skip data descriptor (sizes are already in central directory)
            # But verify it's there for correctness
            try:
                current_pos = self._file.tell()
                # Validate data descriptor position is within file bounds
                # Classic data descriptor: 4 (sig) + 4 (crc) + 4 + 4 = 16 bytes
                # ZIP64 data descriptor: 4 (sig) + 4 (crc) + 8 + 8 = 24 bytes
                is_zip64_descriptor = (entry.compressed_size > 0xFFFFFFFF or entry.uncompressed_size > 0xFFFFFFFF)
                descriptor_size = 24 if is_zip64_descriptor else 16
                
                if current_pos + descriptor_size > file_size:
                    raise ZipFormatError(
                        f"Data descriptor extends beyond file for entry '{entry.name}': "
                        f"position {current_pos}, size {descriptor_size} (file size: {file_size})"
                    )
                
                descriptor = parse_data_descriptor(self._file, is_zip64=is_zip64_descriptor)
                # Verify CRC matches (optional, but good for validation)
                # Note: We'll validate CRC after decompression
            except ZipFormatError:
                # Data descriptor might not be present if sizes were known
                # This is okay, we have sizes from central directory
                pass

        # Decompress based on method
        if entry.compression_method == COMP_STORED:
            return compressed_data
        elif entry.compression_method == COMP_DEFLATE:
            try:
                decompressor = zlib.decompressobj(-zlib.MAX_WBITS)
                data = decompressor.decompress(compressed_data)
                if decompressor.unused_data:
                    raise ZipCompressionError("Extra data after compressed stream")
                return data
            except zlib.error as e:
                raise ZipCompressionError(f"Deflate decompression failed: {e}") from e
        else:
            raise ZipUnsupportedFeature(
                f"Unsupported compression method: {entry.compression_method}"
            )

    def _validate_crc32(self, data: bytes, expected_crc: int) -> None:
        """Validate CRC32 checksum of decompressed data.

        Args:
            data: Decompressed data to validate.
            expected_crc: Expected CRC32 value from archive.

        Raises:
            ZipCrcError: If CRC32 does not match.
        """
        actual_crc = crc32(data)
        if actual_crc != expected_crc:
            raise ZipCrcError(
                f"CRC32 mismatch: expected 0x{expected_crc:08X}, got 0x{actual_crc:08X}"
            )

    def list(self) -> list[str]:
        """List all entry names in the archive.

        Returns:
            List of entry names (files and directories).
        """
        return list(self._entries.keys())

    def get_info(self, name: str) -> Optional[ZipEntry]:
        """Get metadata for a specific entry.

        Args:
            name: Entry name (must match exactly, including path separators).

        Returns:
            ZipEntry object if found, None otherwise.
        """
        # Normalize path separators to match stored entry names
        if "\\" in name:
            name = name.replace("\\", "/")
        return self._entries.get(name)

    def open(self, name: str) -> BinaryIO:
        """Open an entry for reading decompressed data.

        Args:
            name: Entry name to open.

        Returns:
            BinaryIO file-like object containing decompressed data.

        Raises:
            ZipFormatError: If the archive is closed.
            KeyError: If entry is not found.
            ZipUnsupportedFeature: If compression method is not supported.
            ZipCompressionError: If decompression fails.
            ZipCrcError: If CRC32 validation fails.
        """
        if self._closed:
            raise ZipFormatError("Archive is closed")
        
        # Normalize path separators to match stored entry names
        if "\\" in name:
            name = name.replace("\\", "/")
        
        if name not in self._entries:
            raise KeyError(f"Entry not found: {name}")

        entry = self._entries[name]

        if entry.is_dir:
            return io.BytesIO(b"")

        # Decompress entry
        data = self._decompress_entry(entry)

        # Validate CRC32
        self._validate_crc32(data, entry.crc32)

        return io.BytesIO(data)

    def close(self) -> None:
        """Close the archive file."""
        if self._closed:
            return
        
        if self._should_close and self._file:
            self._file.close()
            self._file = None
        
        self._closed = True

    def __enter__(self) -> "ZipReader":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        self.close()

