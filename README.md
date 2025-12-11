## DNZIP - ZIP64 Python Library

A pure Python compression formats engine, no external dependencies, and a small built-in CLI.

- **Language**: Python 3.8+
- **Package name**: `dnzip` (previously `zip64py`)
- **License**: Apache License 2.0
- **Version**: 0.1.2 (Development Build 0.1.3-dev.508)
- **Status**: Production-ready (195/195 tests passing)

---

## Features

- **Full ZIP/ZIP64 support** (local headers, central directory, ZIP64 EOCD, data descriptors)
- **Read and write** standard ZIP and ZIP64 archives
- **GZIP format support** (RFC 1952) for single-file compression
- **BZIP2 format support** for single-file compression
- **XZ format support** (LZMA2) for single-file compression
- **TAR format support** (USTAR) for multi-file archives
- **7Z format support** (framework in place, full parsing/writing pending) for 7-Zip archives
- **Compression methods**: STORED (no compression), DEFLATE (zlib-based), BZIP2 (bz2-based), LZMA (lzma-based), and PPMd (method 98, framework in place, not yet implemented) for ZIP archives
- **Pure Python, no external dependencies**
- **Defensive implementation**:
  - Bounds checking for offsets, sizes, counts
  - CRC32 verification
  - Safe handling of malformed archives
- **Streaming support** via `StreamingZipWriter` and data descriptors
- **Debug utilities** for inspecting/validating archives
- **Command-line interface** via `python -m dnzip`

---

## Installation

### From PyPI (recommended)

### From source

```bash
git clone <repository-url>
cd DNZip
pip install -e .
```

> **Note**: The import name is always `dnzip`, even when installing from source.

---

## Quick Start (Python API)

### Reading an archive

```python
from dnzip import ZipReader

archive_path = "archive.zip"

with ZipReader(archive_path) as z:
    # List all entry names
    for name in z.list():
        print(name)

    # Get metadata for a specific entry
    info = z.get_info("docs/readme.txt")
    if info is not None:
        print(f"Name: {info.name}")
        print(f"Uncompressed size: {info.uncompressed_size}")
        print(f"Compressed size:   {info.compressed_size}")
        print(f"Compression method: {info.compression_method}")
        print(f"Is directory: {info.is_dir}")

    # Read entry contents (open() supports context manager)
    with z.open("docs/readme.txt") as f:
        data = f.read()
        print(data.decode("utf-8", errors="replace"))
    
    # Iterate over entry names (iterator API)
    for name in z:
        print(name)
    
    # Iterate over ZipEntry objects
    for entry in z.iter_entries():
        print(f"{entry.name}: {entry.uncompressed_size} bytes")
    
    # Iterate over file entries only (excluding directories)
    for entry in z.iter_files():
        print(f"File: {entry.name}")
    
    # Get archive metadata
    print(f"Total entries: {z.get_entry_count()}")
    print(f"Total size: {z.get_total_size()} bytes")
    print(f"Compression ratio: {z.get_compression_ratio():.2%}")
    
    # Get archive comment
    archive_comment = z.get_archive_comment()
    if archive_comment:
        print(f"Archive comment: {archive_comment.decode('utf-8')}")
```

### Writing an archive

```python
from dnzip import ZipWriter

archive_path = "archive.zip"

with ZipWriter(archive_path, archive_comment="Created with DNZIP") as z:
    # Add data from bytes (DEFLATE compression by default)
    z.add_bytes("hello.txt", b"Hello, World!", comment="Greeting file")
    
    # Add data with entry comment
    z.add_bytes("readme.txt", b"Read me!", comment="Important documentation")

    # Add an existing file from disk with comment
    z.add_file("docs/manual.pdf", "/path/to/manual.pdf", comment="User manual")
```

### Writing with explicit compression

```python
from dnzip import ZipWriter

with ZipWriter("mixed.zip") as z:
    # Store without compression
    z.add_bytes("raw.bin", b"\x00\x01\x02", compression="stored")

    # Compress using DEFLATE with default compression level (6)
    z.add_bytes("text.txt", b"Highly compressible text" * 100, compression="deflate")
    
    # Compress using DEFLATE with maximum compression level (9)
    z.add_bytes("best.txt", b"Highly compressible text" * 100, compression="deflate", compression_level=9)
    
    # Compress using DEFLATE with fastest compression level (1)
    z.add_bytes("fast.txt", b"Highly compressible text" * 100, compression="deflate", compression_level=1)
    
    # Compress using BZIP2 with default compression level (9)
    z.add_bytes("bzip2.txt", b"Highly compressible text" * 100, compression="bzip2")
    
    # Compress using BZIP2 with maximum compression level (9)
    z.add_bytes("bzip2_best.txt", b"Highly compressible text" * 100, compression="bzip2", compression_level=9)
    
    # Compress using BZIP2 with fastest compression level (1)
    z.add_bytes("bzip2_fast.txt", b"Highly compressible text" * 100, compression="bzip2", compression_level=1)
    
    # Compress using LZMA with default compression level (6)
    z.add_bytes("lzma.txt", b"Highly compressible text" * 100, compression="lzma")
    
    # Compress using LZMA with maximum compression level (9)
    z.add_bytes("lzma_best.txt", b"Highly compressible text" * 100, compression="lzma", compression_level=9)
    
    # Compress using LZMA with fastest compression level (1)
    z.add_bytes("lzma_fast.txt", b"Highly compressible text" * 100, compression="lzma", compression_level=1)
    
    # Note: PPMd compression (method 98) is not yet implemented
    # Attempting to use compression="ppmd" will raise ZipUnsupportedFeature error
```

### Compression Levels

DNZIP supports compression levels for different compression methods:

**DEFLATE compression (0-9):**
- **0**: No compression (same as STORED method)
- **1**: Fastest compression (lowest CPU usage, larger files)
- **6**: Default compression level (balanced)
- **9**: Best compression (highest CPU usage, smallest files)

**BZIP2 compression (1-9):**
- **1**: Fastest compression (lowest CPU usage, larger files)
- **9**: Best compression (highest CPU usage, smallest files, default)

**LZMA compression (0-9):**
- **0**: No compression (same as STORED method)
- **1**: Fastest compression (lowest CPU usage, larger files)
- **6**: Default compression level (balanced)
- **9**: Best compression (highest CPU usage, smallest files)

The compression level parameter is ignored when using the STORED compression method.

**Note**: Compression level ranges differ by method:
- DEFLATE and LZMA use levels 0-9 (where 6 is the default)
- BZIP2 uses levels 1-9 (where 9 is the default)
- LZMA typically provides the best compression ratios but is slower than DEFLATE
- BZIP2 typically provides better compression ratios than DEFLATE for text files, but compression and decompression are slower
- PPMd compression (method 98) is not yet implemented - framework is in place for future support

### Archive and Entry Comments

DNZIP supports both archive-level comments and per-entry comments:

```python
from dnzip import ZipWriter, ZipReader

# Create archive with archive comment and entry comments
with ZipWriter("archive.zip", archive_comment="Archive created on 2025-01-01") as z:
    z.add_bytes("readme.txt", b"Read me!", comment="Important file")
    z.add_bytes("data.txt", b"Data", comment="Data file")

# Read comments
with ZipReader("archive.zip") as z:
    # Get archive comment
    archive_comment = z.get_archive_comment()
    print(f"Archive: {archive_comment.decode('utf-8')}")
    
    # Get entry comments
    for entry in z.iter_entries():
        if entry.comment:
            print(f"{entry.name}: {entry.comment.decode('utf-8')}")
```

Comments can be provided as bytes or strings (strings are encoded as UTF-8). Maximum comment length is 65535 bytes per ZIP specification.

### Extra Fields

DNZIP supports reading and writing ZIP extra fields, which store additional metadata such as Unix permissions, NTFS timestamps, and extended timestamps.

#### Reading Extra Fields

Extra fields are automatically parsed when reading archives:

```python
from dnzip import ZipReader

with ZipReader("archive.zip") as z:
    entry = z.get_info("file.txt")
    if entry and entry.extra_fields:
        # Access Unix extra fields
        if entry.extra_fields.unix:
            print(f"UID: {entry.extra_fields.unix.uid}")
            print(f"GID: {entry.extra_fields.unix.gid}")
        
        # Access NTFS timestamps
        if entry.extra_fields.ntfs:
            print(f"Modification time: {entry.extra_fields.ntfs.mtime}")
            print(f"Access time: {entry.extra_fields.ntfs.atime}")
            print(f"Creation time: {entry.extra_fields.ntfs.ctime}")
        
        # Access Extended Timestamp fields
        if entry.extra_fields.extended_timestamp:
            print(f"Modification time: {entry.extra_fields.extended_timestamp.mtime}")
```

#### Writing Extra Fields

You can specify extra fields when creating archives:

```python
from dnzip import ZipWriter
from dnzip.structures import ExtraFields, UnixExtraField, NtfsExtraField, ExtendedTimestampExtraField
from datetime import datetime

# Create extra fields
extra_fields = ExtraFields()
extra_fields.unix = UnixExtraField(version=1, uid=1000, gid=1000)
extra_fields.extended_timestamp = ExtendedTimestampExtraField(
    mtime=datetime(2021, 1, 1, 12, 0, 0)
)

# Write archive with extra fields
with ZipWriter("archive.zip") as z:
    z.add_bytes("file.txt", b"data", extra_fields=extra_fields)
    
    # Auto-generate Unix extra fields from file system
    z.add_file("file2.txt", "/path/to/file2.txt", auto_unix_extra=True)
```

**Supported Extra Field Types:**
- **Unix Extra Field** (`UnixExtraField`): Unix permissions, UID, GID
- **NTFS Extra Field** (`NtfsExtraField`): NTFS timestamps (modification, access, creation time)
- **Extended Timestamp** (`ExtendedTimestampExtraField`): Unix-style timestamps
- **Unicode Path** (`UnicodePathExtraField`): UTF-8 encoded filenames

**Auto-Generation of Unix Extra Fields:**

When using `add_file()`, you can enable automatic generation of Unix extra fields from the file system:

```python
with ZipWriter("archive.zip") as z:
    # Auto-generate Unix extra fields (UID, GID) from file system
    z.add_file("file.txt", "/path/to/file.txt", auto_unix_extra=True)
    
    # Disable auto-generation
    z.add_file("file2.txt", "/path/to/file2.txt", auto_unix_extra=False)
```

Note: Auto-generation only works on Unix-like systems and requires appropriate file system permissions.

### Iterator API

DNZIP provides efficient iterator methods for processing archives:

```python
from dnzip import ZipReader

with ZipReader("archive.zip") as z:
    # Iterate over entry names
    for name in z:
        print(name)
    
    # Iterate over ZipEntry objects with full metadata
    for entry in z.iter_entries():
        print(f"{entry.name}: {entry.uncompressed_size} bytes, "
              f"compressed: {entry.compressed_size} bytes")
    
    # Iterate over file entries only (excluding directories)
    total_size = 0
    for entry in z.iter_files():
        total_size += entry.uncompressed_size
    print(f"Total file size: {total_size} bytes")
```

### Metadata Extraction

DNZIP provides convenient methods to extract archive metadata:

```python
from dnzip import ZipReader

with ZipReader("archive.zip") as z:
    # Get entry count
    print(f"Entries: {z.get_entry_count()}")
    
    # Get total uncompressed size
    print(f"Total size: {z.get_total_size():,} bytes")
    
    # Get total compressed size
    print(f"Compressed size: {z.get_total_compressed_size():,} bytes")
    
    # Get compression ratio
    ratio = z.get_compression_ratio()
    print(f"Compression ratio: {ratio:.2%}")
    
    # Get oldest and newest entries
    oldest = z.get_oldest_entry()
    newest = z.get_newest_entry()
    if oldest and newest:
        print(f"Oldest: {oldest.name} ({oldest.date_time})")
        print(f"Newest: {newest.name} ({newest.date_time})")
```

### GZIP Format Support

DNZIP supports reading and writing GZIP compressed files (RFC 1952). GZIP is a single-file compression format commonly used on Unix-like systems.

#### Reading GZIP Files

```python
from dnzip import GzipReader

# Read and decompress a GZIP file
with GzipReader("file.txt.gz") as gz:
    data = gz.read()
    print(data.decode("utf-8"))
    
    # Access GZIP metadata
    if gz.filename:
        print(f"Original filename: {gz.filename}")
    if gz.comment:
        print(f"Comment: {gz.comment}")
    print(f"Modification time: {gz.mtime}")
```

#### Writing GZIP Files

```python
from dnzip import GzipWriter

# Compress data to a GZIP file
data = b"Hello, World! This is test data for GZIP compression."

with GzipWriter("output.gz", filename="original.txt", comment="Test file") as gz:
    gz.write(data)

# Compress with specific compression level
with GzipWriter("output.gz", compression_level=9) as gz:  # Maximum compression
    gz.write(data)

# Compress with modification time
import time
mtime = int(time.time()) - 3600  # 1 hour ago
with GzipWriter("output.gz", mtime=mtime) as gz:
    gz.write(data)
```

#### GZIP CRC32 Verification

GZIP files include CRC32 checksums for data integrity verification:

```python
from dnzip import GzipReader

# Strict mode (default): raise error on CRC mismatch
with GzipReader("file.gz", crc_verification="strict") as gz:
    data = gz.read()

# Warn mode: warn but continue on CRC mismatch
import warnings
with warnings.catch_warnings():
    warnings.simplefilter("always")
    with GzipReader("file.gz", crc_verification="warn") as gz:
        data = gz.read()

# Skip mode: skip CRC verification entirely
with GzipReader("file.gz", crc_verification="skip") as gz:
    data = gz.read()
```

#### GZIP Compression Levels

GZIP compression levels range from 0 to 9:
- **0**: No compression (fastest)
- **1**: Fastest compression (lowest CPU usage)
- **6**: Default compression level (balanced)
- **9**: Best compression (highest CPU usage, smallest files)

### BZIP2 Format Support

DNZIP supports reading and writing BZIP2 compressed files. BZIP2 uses the Burrows-Wheeler transform and Huffman coding for compression, typically providing better compression ratios than GZIP for text files, though at the cost of slower compression and decompression.

#### Reading BZIP2 Files

```python
from dnzip import Bzip2Reader

# Read and decompress a BZIP2 file
with Bzip2Reader("file.txt.bz2") as bz2:
    data = bz2.read()
    print(data.decode("utf-8"))
```

#### Writing BZIP2 Files

```python
from dnzip import Bzip2Writer

# Compress data to a BZIP2 file
data = b"Hello, World! This is test data for BZIP2 compression."

with Bzip2Writer("output.bz2", compression_level=9) as bz2:
    bz2.write(data)

# Write data in chunks (streaming)
with Bzip2Writer("output.bz2") as bz2:
    bz2.write(b"First chunk of data. ")
    bz2.write(b"Second chunk of data. ")
    bz2.write(b"Third chunk of data.")
```

#### BZIP2 Compression Levels

BZIP2 compression levels range from 1 to 9:
- **1**: Fastest compression (lowest CPU usage, larger files)
- **5**: Balanced compression (moderate CPU usage)
- **9**: Best compression (highest CPU usage, smallest files, default)

Note: BZIP2 compression levels are different from DEFLATE/GZIP - higher values provide better compression but require more CPU time.

### XZ Format Support

DNZIP supports reading and writing XZ compressed files. XZ uses the LZMA2 compression algorithm and provides excellent compression ratios, often better than both GZIP and BZIP2, though at the cost of slower compression and decompression. XZ is commonly used on Unix-like systems and is the default compression format for many Linux distributions.

#### Reading XZ Files

```python
from dnzip import XzReader

# Read and decompress an XZ file
with XzReader("file.txt.xz") as xz:
    data = xz.read()
    print(data.decode("utf-8"))
```

#### Writing XZ Files

```python
from dnzip import XzWriter

# Compress data to an XZ file
data = b"Hello, World! This is test data for XZ compression."

with XzWriter("output.xz", compression_level=6) as xz:
    xz.write(data)

# Write data in chunks (streaming)
with XzWriter("output.xz") as xz:
    xz.write(b"First chunk of data. ")
    xz.write(b"Second chunk of data. ")
    xz.write(b"Third chunk of data.")
```

#### XZ Compression Levels

XZ compression levels range from 0 to 9:
- **0**: Fastest compression (lowest CPU usage, larger files)
- **3**: Balanced compression (moderate CPU usage)
- **6**: Good compression (default, good balance)
- **9**: Best compression (highest CPU usage, smallest files)

Note: XZ compression levels are similar to LZMA - higher values provide better compression but require significantly more CPU time and memory. Level 6 is a good default for most use cases.

### TAR Format Support

DNZIP supports reading and writing TAR archives. TAR (Tape Archive) is a popular archive format on Unix-like systems that combines multiple files into a single archive file. DNZIP implements USTAR format support, which includes extended headers for long filenames and metadata.

#### Reading TAR Archives

```python
from dnzip import TarReader

# Read a TAR archive
with TarReader("archive.tar") as tar:
    # List all entries
    for name in tar.list():
        print(name)
    
    # Get entry information
    entry = tar.get_entry("file.txt")
    if entry:
        print(f"Size: {entry.size}")
        print(f"Mode: {oct(entry.mode)}")
        print(f"Modified: {entry.mtime}")
    
    # Read entry data
    data = tar.read_entry("file.txt")
    print(data.decode("utf-8"))
    
    # Iterate over entries
    for entry in tar.iter_entries():
        print(f"{entry.name}: {entry.size} bytes")
```

#### Writing TAR Archives

```python
from dnzip import TarWriter
from datetime import datetime

# Create a TAR archive
with TarWriter("archive.tar") as tar:
    # Add file from bytes
    tar.add_bytes("file.txt", b"Hello, World!")
    
    # Add file with permissions and timestamp
    tar.add_bytes(
        "script.sh",
        b"#!/bin/bash\necho 'Hello'",
        mode=0o755,
        mtime=datetime(2020, 1, 1, 12, 0, 0)
    )
    
    # Add directory
    tar.add_directory("mydir/")
    
    # Add file from filesystem
    tar.add_file("archive_name.txt", "/path/to/source.txt")
```

#### TAR Entry Types

TAR supports different entry types:
- **Regular files**: Standard file entries
- **Directories**: Directory entries (size is 0)
- **Symbolic links**: Link entries (future enhancement)
- **Hard links**: Hard link entries (future enhancement)

#### TAR Format Notes

- DNZIP implements **USTAR format** (POSIX.1-1988), which supports filenames up to 255 characters via prefix extension
- File permissions are preserved when reading/writing
- Timestamps are stored as Unix timestamps
- TAR archives can be combined with compression formats (tar.gz, tar.bz2, tar.xz) by wrapping TarReader/TarWriter with compression readers/writers

---

### 7Z Format Support

DNZIP includes a framework implementation for 7Z archives (7-Zip format). The 7Z format is complex with encoded headers and supports multiple compression methods (LZMA, LZMA2, BZIP2, PPMd, etc.). Currently, DNZIP provides a framework that can recognize 7Z files and provides helpful error messages. Full parsing and writing implementation is pending.

> **Note**: 7Z format support is currently in framework stage. Full implementation requires parsing the encoded header structure, which includes streams info, files info, and compression method handling.

#### 7Z Framework Usage

```python
from dnzip import SevenZipReader, SevenZipWriter
from dnzip.errors import ZipUnsupportedFeature

# Attempting to read a 7Z archive (framework only)
try:
    with SevenZipReader("archive.7z") as sz:
        # This will raise ZipUnsupportedFeature with helpful error message
        entries = list(sz.iter_entries())
except ZipUnsupportedFeature as e:
    print(f"7Z format not yet fully implemented: {e}")

# Attempting to write a 7Z archive (framework only)
try:
    with SevenZipWriter("archive.7z", compression_method="lzma2", compression_level=6) as sz:
        # This will raise ZipUnsupportedFeature with helpful error message
        sz.add_bytes("file.txt", b"Hello, World!")
except ZipUnsupportedFeature as e:
    print(f"7Z format not yet fully implemented: {e}")
```

#### 7Z Framework Features

- **Signature Recognition**: Verifies 7Z magic number (`7z\xBC\xAF\x27\x1C`)
- **Version Parsing**: Reads archive version (currently 0.3)
- **Header Structure**: Basic header parsing (full encoded header parsing pending)
- **Compression Methods**: Framework supports method selection (copy, lzma, lzma2)
- **Compression Levels**: Framework supports level configuration (0-9)
- **Error Messages**: Clear error messages explaining framework status

#### 7Z Format Notes

- 7Z format uses **encoded headers** that require decompression/compression
- Full implementation will support multiple compression methods (LZMA, LZMA2, BZIP2, PPMd, DEFLATE)
- 7Z format supports file metadata (names, sizes, attributes, timestamps)
- Framework provides structure for future full implementation
- CLI commands (`7z-list`, `7z-extract`, `7z-create`) are available but indicate framework-only status

---

### RAR Format Support (Read-Only)

DNZIP provides **read-only** support for RAR archives (RAR v4 format). RAR is a proprietary archive format developed by Eugene Roshal, and DNZIP implements basic reading capabilities for RAR v4 archives. Write support is not available due to the proprietary nature of the RAR format.

> **Important Limitations**: 
> - **Read-only support**: DNZIP can only read RAR archives, not create or modify them
> - **STORED compression only**: Only RAR archives with STORED (uncompressed) entries can be fully read
> - **RAR v4 and v5 formats**: Both RAR v4 and v5 formats are supported for reading (v5 parsing implemented in dev build 0.1.3-dev.48)
> - **No compression methods**: RAR compression methods (FASTEST, FAST, NORMAL, GOOD, BEST) use proprietary algorithms and are not supported
> - **No encryption**: Encrypted RAR archives are not supported
> - **No split archives**: Multi-part RAR archives (.part1.rar, .part2.rar, etc.) are not supported

#### Reading RAR Archives

```python
from dnzip import RarReader
from dnzip.errors import RarUnsupportedFeature

# Read a RAR archive
try:
    with RarReader("archive.rar") as rar:
        # List all entries
        for name in rar.list():
            print(name)
        
        # Get entry information
        entry = rar.get_entry("file.txt")
        if entry:
            print(f"Size: {entry.size}")
            print(f"Compressed size: {entry.compressed_size}")
            print(f"Modified: {entry.mtime}")
            print(f"Is directory: {entry.is_directory}")
            print(f"Compression method: {entry.compression_method}")
        
        # Read entry data (only works for STORED entries)
        try:
            data = rar.read_entry("file.txt")
            print(data.decode("utf-8"))
        except RarUnsupportedFeature as e:
            print(f"Cannot read compressed entry: {e}")
        
        # Iterate over entries
        for entry in rar.iter_entries():
            print(f"{entry.name}: {entry.size} bytes")
        
        # Iterate over files only (excluding directories)
        for entry in rar.iter_files():
            print(f"File: {entry.name}")
        
        # Get archive statistics
        print(f"Total entries: {rar.get_entry_count()}")
        print(f"Total size: {rar.get_total_size()} bytes")
        print(f"Total compressed size: {rar.get_total_compressed_size()} bytes")
except RarUnsupportedFeature as e:
    print(f"RAR archive uses unsupported features: {e}")
```

#### RAR Entry Properties

The `RarEntry` dataclass provides the following properties:

- `name`: Entry name (filename or path)
- `size`: Uncompressed size in bytes
- `compressed_size`: Compressed size in bytes
- `is_directory`: `True` if entry is a directory
- `is_encrypted`: `True` if entry is encrypted (not supported)
- `compression_method`: Compression method code (only STORED is supported)
- `crc32`: CRC32 checksum
- `mtime`: Modification time as `datetime` object (may be `None`)
- `attributes`: File attributes
- `version`: RAR version needed to extract
- `host_os`: Host operating system code
- `split_before`: `True` if file is split before this part (not supported)
- `split_after`: `True` if file is split after this part (not supported)

#### RAR Compression Methods

RAR supports several compression methods, but DNZIP only supports STORED (no compression):

- **STORED (0x30)**: No compression - **Supported** ✅
- **FASTEST (0x31)**: Fastest compression - **Not supported** ❌
- **FAST (0x32)**: Fast compression - **Not supported** ❌
- **NORMAL (0x33)**: Normal compression - **Not supported** ❌
- **GOOD (0x34)**: Good compression - **Not supported** ❌
- **BEST (0x35)**: Best compression - **Not supported** ❌

When encountering compressed entries, DNZIP will raise `RarUnsupportedFeature` with a helpful error message explaining the limitation and suggesting alternatives.

#### RAR Format Notes

- **RAR v4 Format**: DNZIP supports reading RAR v4 archives (signature: `Rar!\x1a\x07\x00`)
- **RAR v5 Format**: RAR v5 format parsing is fully implemented (signature: `Rar!\x1a\x07\x01\x00`) - can read archive headers, file headers, and extract file metadata (names, sizes, timestamps, attributes) from RAR v5 archives
- **Proprietary Algorithm**: RAR compression uses a proprietary algorithm based on PPM (Prediction by Partial Matching) and LZSS (Lempel-Ziv-Storer-Szymanski) that is not available in Python's standard library
- **Alternative Tools**: For extracting compressed RAR files, use tools like `unrar` or `7-Zip`, then re-compress using a supported format if needed
- **CLI Commands**: DNZIP CLI provides `rar-list` and `rar-extract` commands for basic read operations

#### RAR Error Handling

DNZIP provides specific error classes for RAR operations:

- `RarError`: Base exception class for all RAR-related errors
- `RarFormatError`: Raised when RAR file format is invalid or corrupted
- `RarUnsupportedFeature`: Raised when encountering unsupported RAR features (compression, encryption, split archives, etc.)
- `RarCompressionError`: Raised when decompression fails (not currently used, as compression is not supported)

Example error handling:

```python
from dnzip import RarReader
from dnzip.errors import RarFormatError, RarUnsupportedFeature

try:
    with RarReader("archive.rar") as rar:
        data = rar.read_entry("compressed_file.txt")
except RarFormatError as e:
    print(f"Invalid RAR format: {e}")
except RarUnsupportedFeature as e:
    print(f"Unsupported RAR feature: {e}")
    # Error message will explain what feature is not supported
    # and suggest alternatives
except KeyError as e:
    print(f"Entry not found: {e}")
```

---

## Python API Overview

### ZipReader

**Constructor**

- `ZipReader(path_or_file, crc_verification: str = "strict")`  
  - `path_or_file`: `str | pathlib.Path | BinaryIO`  
  - `crc_verification`: CRC32 verification mode:
    - `"strict"` (default): Raise `ZipCrcError` on CRC mismatch
    - `"warn"`: Warn but continue on CRC mismatch
    - `"skip"`: Skip CRC verification entirely

**Core methods**

- `list() -> list[str]`  
  **List all entry names** in the archive.

- `get_info(name: str) -> ZipEntry | None`  
  **Return metadata** for a specific entry, or `None` if it does not exist.

- `open(name: str) -> BinaryIO`  
  **Open an entry** for reading decompressed data.  
  - Returns a file-like object that supports context manager protocol.
  - Recommended usage: `with zip_reader.open("file.txt") as f: data = f.read()`
  - Raises `KeyError` if the entry does not exist.
  - Validates CRC32 on read.

- `__iter__() -> Iterator[str]`  
  **Iterate over entry names** in the archive.  
  - Enables `for name in zip_reader:` pattern.

- `iter_entries() -> Iterator[ZipEntry]`  
  **Iterate over ZipEntry objects** in the archive.  
  - Returns iterator of ZipEntry objects with full metadata.

- `iter_files() -> Iterator[ZipEntry]`  
  **Iterate over file entries only** (excluding directories).  
  - Returns iterator of ZipEntry objects for files only.

- `get_entry_count() -> int`  
  **Get total number of entries** (files and directories).

- `get_total_size() -> int`  
  **Get total uncompressed size** of all file entries (excluding directories).

- `get_total_compressed_size() -> int`  
  **Get total compressed size** of all file entries (excluding directories).

- `get_compression_ratio() -> float`  
  **Get overall compression ratio** (compressed_size / uncompressed_size).  
  - Returns 0.0 for empty archives or archives with only directories.

- `get_oldest_entry() -> ZipEntry | None`  
  **Get entry with oldest modification time**.  
  - Returns None for empty archives.

- `get_newest_entry() -> ZipEntry | None`  
  **Get entry with newest modification time**.  
  - Returns None for empty archives.

- `get_archive_comment() -> bytes`  
  **Get the archive comment** from the End of Central Directory record.  
  - Returns empty bytes if no comment exists.

- `close() -> None`  
  Close the underlying file handle.

**Context manager**

```python
from dnzip import ZipReader

with ZipReader("archive.zip") as z:
    print(z.list())
```

---

### ZipWriter

**Constructor**

- `ZipWriter(path_or_file, mode: str = "w", archive_comment: bytes | str = b"")`  
  - `path_or_file`: `str | pathlib.Path | BinaryIO`  
  - `mode`: currently `"w"` (write new archive)
  - `archive_comment`: Optional archive comment (bytes or str, max 65535 bytes)

**Core methods**

- `add_bytes(name: str, data: bytes, compression: str = "deflate", comment: bytes | str = b"", compression_level: int = 6, extra_fields: ExtraFields | None = None) -> None`  
  Add an entry with the given `name` and in-memory `data`.  
  - `comment`: Optional entry comment (bytes or str, max 65535 bytes).
  - `compression_level`: Compression level (0-9 for DEFLATE, default: 6).
  - `extra_fields`: Optional ExtraFields object containing extra field data to write.

- `add_file(name_in_zip: str, source_path: str | Path, compression: str = "deflate", comment: bytes | str = b"", compression_level: int = 6, extra_fields: ExtraFields | None = None, auto_unix_extra: bool = True) -> None`  
  Add an entry whose contents are read from `source_path` on disk.  
  - `comment`: Optional entry comment (bytes or str, max 65535 bytes).
  - `compression_level`: Compression level (0-9 for DEFLATE, default: 6).
  - `extra_fields`: Optional ExtraFields object containing extra field data to write.
  - `auto_unix_extra`: If True (default), automatically generate Unix extra fields (UID, GID) from file system metadata.

- `add_stream(name: str, stream: BinaryIO, compression: str = "deflate", compression_level: int = 6, extra_fields: ExtraFields | None = None) -> None`  
  Add an entry from a stream (unknown size).  
  - `compression_level`: Compression level (0-9 for DEFLATE, default: 6).
  - `extra_fields`: Optional ExtraFields object containing extra field data to write.

- `close() -> None`  
  Finalize the central directory / ZIP64 records and close the archive.

**Context manager**

```python
from dnzip import ZipWriter

with ZipWriter("backup.zip") as z:
    z.add_file("database.dump", "/var/backups/db.dump", compression="deflate")
```

---

### StreamingZipWriter (streaming writes)

For very large files or streaming data, use `StreamingZipWriter`, which writes entries
using **data descriptors** so sizes do not need to be known up-front.

```python
from dnzip import StreamingZipWriter

large_path = "/path/to/very-large-file.bin"

with open(large_path, "rb") as src, StreamingZipWriter("large.zip") as z:
    # Stream from an open file object
    z.add_stream("large.bin", src)
```

You can also stream from any object with a `read()` method (e.g., network streams,
custom generators that wrap `BytesIO`, etc.). The operation is wrapped in safe error
handling and will raise a `ZipError` subclass on failure.

---

## Debugging and Verification Utilities

DNZIP includes a small `debug` module (not imported by default) that helps you
inspect and verify archives during development.

```python
from dnzip.debug import dump_zip_structure, verify_zip_structure

archive_path = "archive.zip"

# Print a human-readable breakdown of the ZIP/ZIP64 structures
dump_zip_structure(archive_path)

# Perform structural checks (signatures, offsets, sizes)
verify_zip_structure(archive_path)
```

These tools are intended for advanced debugging and regression testing and were used
to validate DNZIP against archives from multiple tools.

---

## Command-Line Usage (CLI)

DNZIP ships with a small CLI entrypoint implemented in `dnzip/__main__.py`.  
You can invoke it with:

```bash
python -m dnzip <command> [options]
```

### Listing entries

```bash
python -m dnzip list archive.zip
```

This prints one entry name per line.

### Showing detailed info

```bash
python -m dnzip info archive.zip
```

This prints a table similar to:

```text
Archive: archive.zip
================================================================================
Name                                                Size      Compr.    Method
--------------------------------------------------------------------------------
docs/readme.txt                                      512         200         8
images/logo.png                                   123456       45678         8
...
```

### Extracting an archive

```bash
# Extract into the current directory
python -m dnzip extract archive.zip

# Extract into a specific directory
python -m dnzip extract archive.zip -d output_dir

# Extract without progress output
python -m dnzip extract archive.zip -d output_dir --quiet
```

- DNZIP preserves the directory tree.
- Extraction is **safe by default**: paths are validated to prevent extracting
  outside the target directory (no `../` traversal).
- Progress indicators are shown by default; use `--quiet` to suppress them.

### Creating an archive

```bash
# Create archive.zip from a directory tree
python -m dnzip create archive.zip data_folder

# Create archive.zip from multiple paths
python -m dnzip create archive.zip file1.txt dir2 another/file3.bin

# Use STORED (no compression)
python -m dnzip create archive.zip data_folder -c stored

# Use BZIP2 compression
python -m dnzip create archive.zip data_folder -c bzip2

# Use BZIP2 compression with compression level
python -m dnzip create archive.zip data_folder -c bzip2 --compression-level 9

# Use LZMA compression
python -m dnzip create archive.zip data_folder -c lzma

# Use LZMA compression with compression level
python -m dnzip create archive.zip data_folder -c lzma --compression-level 9

# Suppress progress output
python -m dnzip create archive.zip data_folder --quiet
```

- Directory trees are preserved inside the archive.
- By default, DNZIP uses `"deflate"` compression; `"stored"`, `"bzip2"`, `"lzma"`, and `"ppmd"` (not yet implemented) are also supported.
- Progress indicators are shown by default; use `--quiet` to suppress them.

### Progress Indicators

DNZIP displays progress indicators for long-running operations (create, extract):

```bash
# Extract with progress (default)
python -m dnzip extract archive.zip -d output_dir

# Extract without progress output
python -m dnzip extract archive.zip -d output_dir --quiet
```

Progress indicators show:
- Individual file progress (percentage and visual bar)
- Overall progress (file count and percentage)
- File names being processed

The `--quiet` flag suppresses all progress output for cleaner script integration.

### Testing archive integrity

```bash
# Test archive integrity without extracting
python -m dnzip test archive.zip

# Skip CRC32 verification for faster testing
python -m dnzip test archive.zip --skip-crc

# Verify command (alias for test)
python -m dnzip verify archive.zip
```

### GZIP compression and decompression

DNZIP supports GZIP format compression and decompression via CLI:

```bash
# Compress a file to GZIP format
python -m dnzip gzip-compress file.txt

# Compress with custom output filename
python -m dnzip gzip-compress file.txt -o custom.gz

# Compress with original filename and comment in header
python -m dnzip gzip-compress file.txt --filename original.txt --comment "Test file"

# Compress with specific compression level (0-9)
python -m dnzip gzip-compress file.txt --compression-level 9

# Decompress a GZIP file
python -m dnzip gzip-decompress file.txt.gz

# Decompress with custom output filename
python -m dnzip gzip-decompress file.txt.gz -o output.txt

# Decompress without CRC32 verification (faster)
python -m dnzip gzip-decompress file.txt.gz --skip-crc
```

### BZIP2 compression and decompression

DNZIP supports BZIP2 format compression and decompression via CLI:

```bash
# Compress a file to BZIP2 format
python -m dnzip bzip2-compress file.txt

# Specify output filename
python -m dnzip bzip2-compress file.txt -o custom.bz2

# Use specific compression level (1-9, default: 9)
python -m dnzip bzip2-compress file.txt --compression-level 5

# Decompress a BZIP2 file
python -m dnzip bzip2-decompress file.txt.bz2

# Specify output filename
python -m dnzip bzip2-decompress file.txt.bz2 -o output.txt
```

### XZ compression and decompression

DNZIP supports XZ format compression and decompression via CLI:

```bash
# Compress a file to XZ format
python -m dnzip xz-compress file.txt

# Specify output filename
python -m dnzip xz-compress file.txt -o custom.xz

# Use specific compression level (0-9, default: 6)
python -m dnzip xz-compress file.txt --compression-level 9

# Decompress an XZ file
python -m dnzip xz-decompress file.txt.xz

# Specify output filename
python -m dnzip xz-decompress file.txt.xz -o output.txt

# TAR archive operations
python -m dnzip tar-create archive.tar file1.txt file2.txt directory/
python -m dnzip tar-list archive.tar
python -m dnzip tar-extract archive.tar -d output_dir
```

---

## Error Handling

DNZIP uses a small hierarchy of custom exceptions (all inheriting from `ZipError`):

- **`ZipError`**: Base class for all DNZIP-specific errors.
- **`ZipFormatError`**: Structural problem with the archive (invalid signatures,
  out-of-bounds offsets, malformed ZIP64 structures, etc.).
- **`ZipUnsupportedFeature`**: The archive requests a feature that DNZIP does not
  support (e.g., encryption).
- **`ZipCrcError`**: CRC32 checksum mismatch detected while reading.
- **`ZipCompressionError`**: Compression/decompression failed.

You can catch `ZipError` to handle all DNZIP issues in one place:

```python
from dnzip import ZipReader
from dnzip.errors import ZipError

try:
    with ZipReader("archive.zip") as z:
        print(z.list())
except ZipError as exc:
    print(f"DNZIP failed: {exc}")
```

The CLI wraps these exceptions and reports clear error messages on stderr.

---

## Requirements

- Python **3.8 or higher**
- No third-party dependencies (only the standard library)

---

## Development

### Running tests

From the project root:

```bash
python3 run_tests.py
```

Or, with pytest installed:

```bash
pip install pytest pytest-cov
pytest tests/ -v
pytest tests/ --cov=dnzip --cov-report=html
```

### Project structure

```text
dnzip/
├── __init__.py       # Public API (ZipReader, ZipWriter, StreamingZipWriter)
├── __main__.py       # CLI entrypoint (python -m dnzip)
├── constants.py      # ZIP and ZIP64 format constants
├── debug.py          # Debugging and verification utilities
├── errors.py         # Exception classes
├── reader.py         # ZipReader implementation
├── stream.py         # StreamingZipWriter implementation
├── structures.py     # ZIP structure definitions and parsers
├── utils.py          # Utility functions (CRC32, DOS timestamps, I/O helpers)
└── writer.py         # ZipWriter implementation

tests/
├── test_*.py         # 16 comprehensive test modules (195 tests total)
```

---

## Migration Guide from `zipfile` Module

If you're currently using Python's standard library `zipfile` module, migrating to DNZIP is straightforward. Here's a side-by-side comparison:

### Reading Archives

**zipfile:**
```python
import zipfile

with zipfile.ZipFile("archive.zip", "r") as z:
    names = z.namelist()
    info = z.getinfo("file.txt")
    data = z.read("file.txt")
```

**DNZIP:**
```python
from dnzip import ZipReader

with ZipReader("archive.zip") as z:
    names = z.list()  # Same as namelist()
    info = z.get_info("file.txt")  # Returns ZipEntry or None
    data = z.open("file.txt").read()  # open() returns file-like object
```

**Key differences:**
- `ZipReader` is read-only (no mode parameter needed)
- `get_info()` returns `None` if entry doesn't exist (zipfile raises KeyError)
- `open()` returns a file-like object (supports context manager)
- `read()` method doesn't exist; use `open().read()` instead

### Writing Archives

**zipfile:**
```python
import zipfile

with zipfile.ZipFile("archive.zip", "w") as z:
    z.writestr("file.txt", b"data")
    z.write("/path/to/file.txt", "file.txt")
```

**DNZIP:**
```python
from dnzip import ZipWriter

with ZipWriter("archive.zip") as z:
    z.add_bytes("file.txt", b"data")
    z.add_file("file.txt", "/path/to/file.txt")
```

**Key differences:**
- `add_bytes()` replaces `writestr()` (more explicit naming)
- `add_file()` replaces `write()` (parameter order: archive_name, source_path)
- No `mode` parameter needed (always write mode)

### Compression Methods

**zipfile:**
```python
import zipfile

with zipfile.ZipFile("archive.zip", "w", compression=zipfile.ZIP_DEFLATE) as z:
    z.writestr("file.txt", b"data")
```

**DNZIP:**
```python
from dnzip import ZipWriter

with ZipWriter("archive.zip") as z:
    z.add_bytes("file.txt", b"data", compression="deflate")
```

**Compression method mapping:**
- `zipfile.ZIP_STORED` → `compression="stored"`
- `zipfile.ZIP_DEFLATE` → `compression="deflate"`
- `zipfile.ZIP_BZIP2` → `compression="bzip2"` (DNZIP only)
- `zipfile.ZIP_LZMA` → `compression="lzma"` (DNZIP only)
- PPMd (method 98) → `compression="ppmd"` (DNZIP only, framework in place, not yet implemented)

### Compression Levels

**zipfile:**
```python
import zipfile
import zlib

with zipfile.ZipFile("archive.zip", "w") as z:
    z.writestr("file.txt", b"data", compress_type=zipfile.ZIP_DEFLATE)
    # No built-in compression level control
```

**DNZIP:**
```python
from dnzip import ZipWriter

with ZipWriter("archive.zip") as z:
    z.add_bytes("file.txt", b"data", compression="deflate", compression_level=9)
```

**Advantages:**
- Built-in compression level support (0-9 for DEFLATE/LZMA, 1-9 for BZIP2)
- No need to configure zlib separately

### Archive Comments

**zipfile:**
```python
import zipfile

with zipfile.ZipFile("archive.zip", "w") as z:
    z.comment = b"Archive comment"
```

**DNZIP:**
```python
from dnzip import ZipWriter

with ZipWriter("archive.zip", archive_comment="Archive comment") as z:
    pass
```

### Entry Comments

**zipfile:**
```python
import zipfile

with zipfile.ZipFile("archive.zip", "w") as z:
    z.writestr(zipfile.ZipInfo("file.txt"), b"data", compress_type=zipfile.ZIP_DEFLATE)
    # Entry comments require ZipInfo objects
```

**DNZIP:**
```python
from dnzip import ZipWriter

with ZipWriter("archive.zip") as z:
    z.add_bytes("file.txt", b"data", comment="Entry comment")
```

**Advantages:**
- Simpler API: just pass `comment` parameter
- Supports both bytes and strings (auto-encoded to UTF-8)

### Error Handling

**zipfile:**
```python
import zipfile

try:
    with zipfile.ZipFile("archive.zip", "r") as z:
        data = z.read("missing.txt")
except zipfile.BadZipFile:
    print("Invalid ZIP file")
except KeyError:
    print("Entry not found")
```

**DNZIP:**
```python
from dnzip import ZipReader
from dnzip.errors import ZipError, ZipFormatError, ZipCrcError

try:
    with ZipReader("archive.zip") as z:
        info = z.get_info("missing.txt")
        if info is None:
            print("Entry not found")
        else:
            data = z.open("missing.txt").read()
except ZipFormatError:
    print("Invalid ZIP file")
except ZipCrcError:
    print("CRC32 mismatch")
except ZipError as e:
    print(f"DNZIP error: {e}")
```

**Advantages:**
- More specific exception types
- `get_info()` returns `None` instead of raising KeyError
- Clearer error hierarchy

### Streaming Support

**zipfile:**
```python
import zipfile

# No built-in streaming support for unknown sizes
```

**DNZIP:**
```python
from dnzip import StreamingZipWriter

with open("large.bin", "rb") as src, StreamingZipWriter("large.zip") as z:
    z.add_stream("large.bin", src)
```

**Advantages:**
- Built-in `StreamingZipWriter` for unknown-size streams
- Uses data descriptors (ZIP64 compatible)

---

## Troubleshooting Guide

### Common Issues and Solutions

#### Issue: "CRC32 mismatch" error when reading archives

**Symptoms:**
- `ZipCrcError` raised when reading entries
- Data appears corrupted or incomplete

**Possible causes:**
1. Archive file is corrupted or incomplete
2. Archive was modified after creation
3. File transfer issues (network, disk errors)

**Solutions:**
```python
# Option 1: Use warn mode to continue despite CRC errors
from dnzip import ZipReader

with ZipReader("archive.zip", crc_verification="warn") as z:
    data = z.open("file.txt").read()

# Option 2: Skip CRC verification (faster, but less safe)
with ZipReader("archive.zip", crc_verification="skip") as z:
    data = z.open("file.txt").read()

# Option 3: Verify archive integrity first
python -m dnzip test archive.zip
```

#### Issue: "Path traversal detected" error during extraction

**Symptoms:**
- `ZipError` or security error when extracting
- Entry names contain `../` or absolute paths

**Cause:**
- Archive contains malicious or incorrectly formatted entry names

**Solutions:**
```python
# Option 1: Allow absolute paths (use with caution)
from dnzip import ZipReader
from dnzip.utils import safe_extract_path

with ZipReader("archive.zip") as z:
    for entry in z.iter_entries():
        safe_path = safe_extract_path(
            entry.name,
            base_dir="/output",
            allow_absolute_paths=True  # Use carefully!
        )
        # Extract manually

# Option 2: Use CLI with security options
python -m dnzip extract archive.zip --allow-absolute-paths
```

#### Issue: Large archives consume too much memory

**Symptoms:**
- High memory usage when reading large ZIP files
- Out of memory errors

**Solutions:**
```python
# Enable memory-mapped I/O (default, but can be configured)
from dnzip import ZipReader

# For very large files, use streaming decompression
with ZipReader("large.zip", use_mmap=True, mmap_threshold=100*1024*1024) as z:
    # Memory-mapped I/O reduces memory usage
    for entry in z.iter_files():
        with z.open(entry.name) as f:
            # Process in chunks
            while True:
                chunk = f.read(1024*1024)  # 1MB chunks
                if not chunk:
                    break
                # Process chunk
```

#### Issue: Slow compression for many files

**Symptoms:**
- Creating archives with many files is slow
- CPU usage is low (single-threaded)

**Solutions:**
```python
# Use multi-threaded compression
from dnzip import ZipWriter

with ZipWriter("archive.zip", threads=4) as z:  # Use 4 threads
    z.add_file("file1.txt", "/path/to/file1.txt")
    z.add_file("file2.txt", "/path/to/file2.txt")
    # ... many files

# Or via CLI
python -m dnzip create archive.zip folder --threads 4
```

**Note:** Multi-threading is automatically disabled in update mode and with split archives.

#### Issue: "ZIP64 required" error

**Symptoms:**
- Error when creating archives >4GB or with >65535 entries
- Archive creation fails

**Cause:**
- Archive exceeds ZIP32 limits (4GB file size, 65535 entries)

**Solution:**
- DNZIP automatically uses ZIP64 when needed - no action required
- Ensure you're using a recent version of DNZIP

#### Issue: Cannot read archives created by other tools

**Symptoms:**
- `ZipFormatError` when reading archives from WinZip, 7-Zip, etc.
- Archive appears valid but DNZIP can't read it

**Possible causes:**
1. Archive uses unsupported compression method
2. Archive uses encryption (not yet supported)
3. Archive uses unsupported extra fields

**Solutions:**
```python
# Test archive compatibility
python -m dnzip test archive.zip

# Check archive structure
from dnzip.debug import dump_zip_structure
dump_zip_structure("archive.zip")

# Try reading with different CRC verification mode
with ZipReader("archive.zip", crc_verification="skip") as z:
    # May work if only CRC verification is the issue
```

#### Issue: Unicode filename issues

**Symptoms:**
- Filenames appear garbled or incorrect
- Encoding errors when reading entry names

**Solutions:**
```python
# DNZIP handles UTF-8 filenames automatically
# If issues persist, check extra fields for Unicode Path extra field

from dnzip import ZipReader

with ZipReader("archive.zip") as z:
    for entry in z.iter_entries():
        # Entry.name is already decoded
        print(entry.name)
        
        # Check for Unicode Path extra field
        if entry.extra_fields and entry.extra_fields.unicode_path:
            print(f"Unicode path: {entry.extra_fields.unicode_path.path}")
```

#### Issue: Performance issues with large files

**Symptoms:**
- Slow decompression of large compressed files
- High memory usage

**Solutions:**
```python
# Use streaming decompression with custom buffer size
from dnzip import ZipReader

with ZipReader("archive.zip", buffer_size=1024*1024) as z:  # 1MB buffer
    with z.open("large_file.txt") as f:
        # Process in chunks
        while True:
            chunk = f.read(1024*1024)
            if not chunk:
                break
            # Process chunk
```

---

## Performance Tuning Guide

### Compression Performance

#### Choosing the Right Compression Method

**DEFLATE (default):**
- **Best for:** General-purpose compression, balanced speed/size
- **Speed:** Fast compression and decompression
- **Ratio:** Good compression ratio for most data types
- **Use when:** You need a good balance of speed and compression

```python
with ZipWriter("archive.zip") as z:
    z.add_bytes("file.txt", data, compression="deflate", compression_level=6)
```

**BZIP2:**
- **Best for:** Text files, log files, source code
- **Speed:** Slower compression, moderate decompression
- **Ratio:** Better compression than DEFLATE for text
- **Use when:** File size is more important than compression speed

```python
with ZipWriter("archive.zip") as z:
    z.add_bytes("file.txt", data, compression="bzip2", compression_level=9)
```

**LZMA:**
- **Best for:** Maximum compression, archival storage
- **Speed:** Slow compression, moderate decompression
- **Ratio:** Best compression ratio
- **Use when:** Storage space is critical and compression time is acceptable

```python
with ZipWriter("archive.zip") as z:
    z.add_bytes("file.txt", data, compression="lzma", compression_level=6)
```

#### Compression Level Guidelines

**DEFLATE/LZMA (0-9):**
- **0-1:** Fastest compression, largest files (use for real-time applications)
- **6:** Default, good balance (recommended for most cases)
- **9:** Best compression, slowest (use for archival storage)

**BZIP2 (1-9):**
- **1:** Fastest compression, larger files
- **5:** Balanced compression
- **9:** Best compression, default (recommended)

**Performance vs. Size Trade-offs:**

```python
# Fast compression (for interactive applications)
with ZipWriter("fast.zip") as z:
    z.add_bytes("file.txt", data, compression_level=1)

# Balanced (default, recommended)
with ZipWriter("balanced.zip") as z:
    z.add_bytes("file.txt", data, compression_level=6)

# Maximum compression (for archival)
with ZipWriter("archive.zip") as z:
    z.add_bytes("file.txt", data, compression_level=9)
```

### Multi-threaded Compression

For archives with many files, use multi-threaded compression:

```python
import os
from dnzip import ZipWriter

# Use number of CPU cores
num_threads = os.cpu_count() or 4

with ZipWriter("archive.zip", threads=num_threads) as z:
    # Files are compressed in parallel
    for file_path in many_files:
        z.add_file(file_path, file_path)
```

**Guidelines:**
- Use 2-4 threads for most cases
- More threads help with many small files
- Single large file won't benefit from multi-threading
- Multi-threading is disabled automatically in update mode

### Memory Optimization

#### Memory-mapped I/O for Large Archives

For reading large ZIP files (>100MB), enable memory-mapped I/O:

```python
from dnzip import ZipReader

# Default: memory-mapped I/O enabled for files >100MB
with ZipReader("large.zip", use_mmap=True, mmap_threshold=100*1024*1024) as z:
    # Reduced memory usage
    for entry in z.iter_files():
        data = z.open(entry.name).read()
```

**Benefits:**
- Maps file contents to virtual memory instead of loading into RAM
- Significantly reduces memory usage for large archives
- Automatic fallback if mmap is unavailable

#### Streaming Decompression

For large compressed files, use streaming with appropriate buffer sizes:

```python
from dnzip import ZipReader

# Custom buffer size for large files
with ZipReader("archive.zip", buffer_size=1024*1024) as z:  # 1MB buffer
    with z.open("large_file.txt") as f:
        # Process in chunks to reduce memory usage
        while True:
            chunk = f.read(1024*1024)  # 1MB chunks
            if not chunk:
                break
            process_chunk(chunk)
```

**Buffer size recommendations:**
- Small files (<1MB): Default (64KB) is fine
- Medium files (1-100MB): 256KB buffer
- Large files (>100MB): 1MB buffer

### Reading Performance

#### Use Iterators Instead of Lists

For large archives, prefer iterators:

```python
# Efficient: uses iterator (lazy evaluation)
with ZipReader("archive.zip") as z:
    for entry in z.iter_entries():
        process(entry)

# Less efficient: loads all entries into memory
with ZipReader("archive.zip") as z:
    entries = z.list()  # Creates list of all names
    for name in entries:
        process(name)
```

#### Batch Processing

Process multiple entries efficiently:

```python
from dnzip import ZipReader

with ZipReader("archive.zip") as z:
    # Process files in batches
    batch = []
    for entry in z.iter_files():
        batch.append(entry)
        if len(batch) >= 100:
            process_batch(batch)
            batch = []
    if batch:
        process_batch(batch)
```

### Writing Performance

#### Pre-allocate Archive Size (if known)

For very large archives, consider split archives:

```python
from dnzip import ZipWriter

# Create split archive (64MB parts)
with ZipWriter("archive.zip", split_size=64*1024*1024) as z:
    # Automatically splits into .z01, .z02, ..., .zip files
    for file_path in many_files:
        z.add_file(file_path, file_path)
```

#### Avoid Repeated Small Writes

Batch small files together:

```python
# Efficient: batch small files
with ZipWriter("archive.zip") as z:
    for small_file in small_files:
        with open(small_file, "rb") as f:
            z.add_stream(small_file, f)

# Less efficient: reading all into memory first
with ZipWriter("archive.zip") as z:
    for small_file in small_files:
        data = open(small_file, "rb").read()
        z.add_bytes(small_file, data)
```

### Benchmarking

Test performance with your specific data:

```python
import time
from dnzip import ZipWriter, ZipReader

# Benchmark compression
data = b"test data" * 1000000  # 1MB of test data

start = time.time()
with ZipWriter("test.zip") as z:
    z.add_bytes("test.txt", data, compression="deflate", compression_level=6)
compress_time = time.time() - start

# Benchmark decompression
start = time.time()
with ZipReader("test.zip") as z:
    decompressed = z.open("test.txt").read()
decompress_time = time.time() - start

print(f"Compression: {compress_time:.2f}s")
print(f"Decompression: {decompress_time:.2f}s")
```

---

## Advanced Examples

### Example 1: Creating a Backup Archive with Metadata

```python
from dnzip import ZipWriter
from dnzip.structures import ExtraFields, UnixExtraField, ExtendedTimestampExtraField
from datetime import datetime
import os

def create_backup_archive(source_dir, archive_path):
    """Create a backup archive preserving file metadata."""
    with ZipWriter(archive_path, archive_comment=f"Backup created on {datetime.now()}") as z:
        for root, dirs, files in os.walk(source_dir):
            for file in files:
                source_path = os.path.join(root, file)
                archive_name = os.path.relpath(source_path, source_dir)
                
                # Get file metadata
                stat = os.stat(source_path)
                
                # Create extra fields with metadata
                extra_fields = ExtraFields()
                extra_fields.unix = UnixExtraField(
                    version=1,
                    uid=stat.st_uid,
                    gid=stat.st_gid
                )
                extra_fields.extended_timestamp = ExtendedTimestampExtraField(
                    mtime=datetime.fromtimestamp(stat.st_mtime)
                )
                
                # Add file with metadata
                z.add_file(
                    archive_name,
                    source_path,
                    compression="deflate",
                    compression_level=6,
                    extra_fields=extra_fields,
                    comment=f"Backed up: {datetime.fromtimestamp(stat.st_mtime)}"
                )

create_backup_archive("/path/to/source", "backup.zip")
```

### Example 2: Streaming Large Files with Progress

```python
from dnzip import ZipWriter
from dnzip.progress import ProgressCallback

def progress_callback(current, total, name):
    """Progress callback for archive operations."""
    if total > 0:
        percent = (current / total) * 100
        print(f"{name}: {percent:.1f}% ({current}/{total} bytes)")

def archive_large_directory(source_dir, archive_path):
    """Archive a large directory with progress reporting."""
    with ZipWriter(archive_path, progress_callback=progress_callback) as z:
        import os
        for root, dirs, files in os.walk(source_dir):
            for file in files:
                source_path = os.path.join(root, file)
                archive_name = os.path.relpath(source_path, source_dir)
                z.add_file(archive_name, source_path)

archive_large_directory("/large/directory", "archive.zip")
```

### Example 3: Selective Archive Extraction

```python
from dnzip import ZipReader
import os

def extract_filtered(archive_path, output_dir, filter_func):
    """Extract only entries matching a filter function."""
    os.makedirs(output_dir, exist_ok=True)
    
    with ZipReader(archive_path) as z:
        for entry in z.iter_files():
            if filter_func(entry):
                # Create output path
                output_path = os.path.join(output_dir, entry.name)
                os.makedirs(os.path.dirname(output_path), exist_ok=True)
                
                # Extract entry
                with z.open(entry.name) as src:
                    with open(output_path, "wb") as dst:
                        dst.write(src.read())
                
                print(f"Extracted: {entry.name}")

# Extract only Python files
extract_filtered(
    "archive.zip",
    "output",
    lambda entry: entry.name.endswith(".py")
)

# Extract only files larger than 1MB
extract_filtered(
    "archive.zip",
    "output",
    lambda entry: entry.uncompressed_size > 1024 * 1024
)
```

### Example 4: Archive Comparison and Synchronization

```python
from dnzip import ZipReader
from datetime import datetime

def compare_archives(archive1_path, archive2_path):
    """Compare two archives and report differences."""
    with ZipReader(archive1_path) as z1, ZipReader(archive2_path) as z2:
        entries1 = {entry.name: entry for entry in z1.iter_entries()}
        entries2 = {entry.name: entry for entry in z2.iter_entries()}
        
        # Find differences
        only_in_1 = set(entries1.keys()) - set(entries2.keys())
        only_in_2 = set(entries2.keys()) - set(entries1.keys())
        common = set(entries1.keys()) & set(entries2.keys())
        
        print(f"Only in {archive1_path}: {len(only_in_1)} entries")
        print(f"Only in {archive2_path}: {len(only_in_2)} entries")
        print(f"Common: {len(common)} entries")
        
        # Compare common entries
        different = []
        for name in common:
            e1, e2 = entries1[name], entries2[name]
            if (e1.uncompressed_size != e2.uncompressed_size or
                e1.date_time != e2.date_time):
                different.append(name)
        
        print(f"Different: {len(different)} entries")
        return {
            "only_in_1": only_in_1,
            "only_in_2": only_in_2,
            "different": different
        }

compare_archives("archive1.zip", "archive2.zip")
```

### Example 5: Archive Repair and Validation

```python
from dnzip import ZipReader
from dnzip.errors import ZipCrcError, ZipFormatError

def validate_and_repair(archive_path, output_path):
    """Validate archive and create repaired version if needed."""
    try:
        with ZipReader(archive_path, crc_verification="strict") as z:
            # Test all entries
            for entry in z.iter_files():
                try:
                    data = z.open(entry.name).read()
                    print(f"✓ {entry.name}: OK")
                except ZipCrcError:
                    print(f"✗ {entry.name}: CRC32 mismatch")
                    return False
        print("Archive is valid")
        return True
    except ZipFormatError as e:
        print(f"Archive format error: {e}")
        return False

def extract_valid_entries(archive_path, output_dir):
    """Extract only entries that pass CRC32 verification."""
    import os
    os.makedirs(output_dir, exist_ok=True)
    
    with ZipReader(archive_path, crc_verification="warn") as z:
        for entry in z.iter_files():
            try:
                with z.open(entry.name) as src:
                    output_path = os.path.join(output_dir, entry.name)
                    os.makedirs(os.path.dirname(output_path), exist_ok=True)
                    with open(output_path, "wb") as dst:
                        dst.write(src.read())
                print(f"Extracted: {entry.name}")
            except ZipCrcError:
                print(f"Skipped (CRC error): {entry.name}")

validate_and_repair("archive.zip", "repaired.zip")
```

### Example 6: Multi-format Archive Converter

```python
from dnzip import ZipReader, TarWriter, GzipWriter, Bzip2Writer, XzWriter
import os

def convert_zip_to_tar(zip_path, tar_path):
    """Convert ZIP archive to TAR archive."""
    with ZipReader(zip_path) as z, TarWriter(tar_path) as tar:
        for entry in z.iter_entries():
            if entry.is_dir:
                tar.add_directory(entry.name)
            else:
                data = z.open(entry.name).read()
                tar.add_bytes(entry.name, data)

def compress_with_multiple_formats(file_path):
    """Compress a file using multiple compression formats."""
    with open(file_path, "rb") as src:
        data = src.read()
    
    # GZIP
    with GzipWriter(f"{file_path}.gz") as gz:
        gz.write(data)
    
    # BZIP2
    with Bzip2Writer(f"{file_path}.bz2") as bz2:
        bz2.write(data)
    
    # XZ
    with XzWriter(f"{file_path}.xz") as xz:
        xz.write(data)
    
    print(f"Compressed {file_path} with GZIP, BZIP2, and XZ")

convert_zip_to_tar("archive.zip", "archive.tar")
compress_with_multiple_formats("large_file.txt")
```

### Example 7: Archive Statistics and Reporting

```python
from dnzip import ZipReader
from collections import defaultdict

def generate_archive_report(archive_path):
    """Generate a detailed report about an archive."""
    with ZipReader(archive_path) as z:
        # Basic statistics
        total_entries = z.get_entry_count()
        total_size = z.get_total_size()
        compressed_size = z.get_total_compressed_size()
        compression_ratio = z.get_compression_ratio()
        
        # Compression method breakdown
        methods = defaultdict(int)
        extensions = defaultdict(int)
        
        for entry in z.iter_files():
            method_name = {
                0: "STORED",
                8: "DEFLATE",
                12: "BZIP2",
                14: "LZMA"
            }.get(entry.compression_method, "UNKNOWN")
            methods[method_name] += 1
            
            ext = os.path.splitext(entry.name)[1].lower()
            extensions[ext] += 1
        
        # Print report
        print(f"Archive: {archive_path}")
        print(f"Total entries: {total_entries}")
        print(f"Total size: {total_size:,} bytes ({total_size / 1024 / 1024:.2f} MB)")
        print(f"Compressed size: {compressed_size:,} bytes ({compressed_size / 1024 / 1024:.2f} MB)")
        print(f"Compression ratio: {compression_ratio:.2%}")
        print(f"\nCompression methods:")
        for method, count in methods.items():
            print(f"  {method}: {count} files")
        print(f"\nFile extensions:")
        for ext, count in sorted(extensions.items(), key=lambda x: -x[1])[:10]:
            print(f"  {ext or '(no extension)'}: {count} files")

generate_archive_report("archive.zip")
```

---

## License
Apache License Version 2.0


