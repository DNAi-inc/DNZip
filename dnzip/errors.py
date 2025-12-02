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
Custom exception classes for ZIP64 library.

This module defines specific exception types for different error conditions
that can occur when reading or writing ZIP archives.
"""


class ZipError(Exception):
    """Base exception class for all ZIP-related errors."""

    pass


class ZipFormatError(ZipError):
    """Raised when a ZIP file has an invalid format or structure.

    This exception is raised when:
    - Required signatures are missing or incorrect
    - File structure is corrupted
    - Required fields are missing or invalid
    """

    pass


class ZipUnsupportedFeature(ZipError):
    """Raised when encountering an unsupported ZIP feature.

    This exception is raised when:
    - Compression method is not supported
    - Encryption is used (not supported)
    - Other unsupported features are encountered
    """

    pass


class ZipCrcError(ZipError):
    """Raised when CRC32 checksum validation fails.

    This exception is raised when the computed CRC32 of decompressed data
    does not match the expected CRC32 stored in the archive.
    """

    pass


class ZipCompressionError(ZipError):
    """Raised when decompression fails.

    This exception is raised when:
    - Decompression algorithm fails
    - Compressed data is corrupted
    - Invalid compression parameters
    """

    pass

