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
ZIP format constants including signatures, compression methods, flags, and version numbers.

This module defines all the constants used throughout the ZIP64 library for parsing
and writing ZIP and ZIP64 archives.
"""

# ZIP file signatures (magic numbers)
LOCAL_FILE_HEADER = 0x04034B50  # "PK\x03\x04"
CENTRAL_DIR_HEADER = 0x02014B50  # "PK\x01\x02"
END_OF_CENTRAL_DIR = 0x06054B50  # "PK\x05\x06"
ZIP64_END_OF_CENTRAL_DIR = 0x06064B50  # "PK\x06\x06"
ZIP64_END_OF_CENTRAL_DIR_LOCATOR = 0x07064B50  # "PK\x06\x07"
DATA_DESCRIPTOR = 0x08074B50  # "PK\x07\x08"

# Compression methods
COMP_STORED = 0  # No compression
COMP_DEFLATE = 8  # Deflate compression (zlib)
COMP_BZIP2 = 12  # BZIP2 compression
COMP_LZMA = 14  # LZMA compression

# Compression method names (for API)
COMPRESSION_STORED = "stored"
COMPRESSION_DEFLATE = "deflate"
COMPRESSION_BZIP2 = "bzip2"
COMPRESSION_LZMA = "lzma"

# Compression method mapping
COMPRESSION_METHODS = {
    COMPRESSION_STORED: COMP_STORED,
    COMPRESSION_DEFLATE: COMP_DEFLATE,
    COMPRESSION_BZIP2: COMP_BZIP2,
    COMPRESSION_LZMA: COMP_LZMA,
}

# Reverse mapping
METHOD_TO_NAME = {
    COMP_STORED: COMPRESSION_STORED,
    COMP_DEFLATE: COMPRESSION_DEFLATE,
    COMP_BZIP2: COMPRESSION_BZIP2,
    COMP_LZMA: COMPRESSION_LZMA,
}

# General purpose bit flags
FLAG_ENCRYPTED = 0x0001  # File is encrypted
FLAG_DATA_DESCRIPTOR = 0x0008  # Data descriptor follows file data
FLAG_STRONG_ENCRYPTION = 0x0040  # Strong encryption used
FLAG_UTF8 = 0x0800  # UTF-8 encoding for filename/comment

# ZIP version constants
VERSION_DEFAULT = 20  # Default version needed to extract
VERSION_ZIP64 = 45  # ZIP64 format version
VERSION_MADE_BY_DEFAULT = 63  # Made by: Unix (63 = 3.0 * 20 + 3)

# Classic ZIP limits (32-bit)
MAX_FILE_SIZE = 0xFFFFFFFF  # 4 GiB - 1
MAX_ENTRIES = 0xFFFF  # 65535 entries
MAX_CD_SIZE = 0xFFFFFFFF  # 4 GiB - 1
MAX_CD_OFFSET = 0xFFFFFFFF  # 4 GiB - 1

# ZIP64 extra field tag
ZIP64_EXTRA_FIELD_TAG = 0x0001

# Local file header size (fixed part)
LOCAL_FILE_HEADER_SIZE = 30

# Central directory header size (fixed part, excluding filename/extra/comment)
CENTRAL_DIR_HEADER_SIZE = 46

# End of central directory size (fixed part, excluding comment)
END_OF_CENTRAL_DIR_SIZE = 22

# ZIP64 end of central directory size (fixed part)
ZIP64_END_OF_CENTRAL_DIR_SIZE = 56

# ZIP64 locator size (fixed part)
ZIP64_LOCATOR_SIZE = 20

# Data descriptor size (classic)
DATA_DESCRIPTOR_SIZE = 12

# ZIP64 data descriptor size
ZIP64_DATA_DESCRIPTOR_SIZE = 20

