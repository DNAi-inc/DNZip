## DNZIP - ZIP64 Python Library

A pure Python ZIP/ZIP64 engine with a strongly tested core, no external dependencies, and a small built-in CLI.

- **Language**: Python 3.8+
- **Package name**: `dnzip` (previously `zip64py`)
- **License**: Apache License 2.0
- **Status**: Production-ready (195/195 tests passing)

---

## Features

- **Full ZIP/ZIP64 support** (local headers, central directory, ZIP64 EOCD, data descriptors)
- **Read and write** standard ZIP and ZIP64 archives
- **Compression methods**: STORED (no compression) and DEFLATE (zlib-based)
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

    # Read entry contents
    with z.open("docs/readme.txt") as f:
        data = f.read()
        print(data.decode("utf-8", errors="replace"))
```

### Writing an archive

```python
from dnzip import ZipWriter

archive_path = "archive.zip"

with ZipWriter(archive_path) as z:
    # Add data from bytes (DEFLATE compression by default)
    z.add_bytes("hello.txt", b"Hello, World!")

    # Add an existing file from disk
    z.add_file("docs/manual.pdf", "/path/to/manual.pdf")
```

### Writing with explicit compression

```python
from dnzip import ZipWriter

with ZipWriter("mixed.zip") as z:
    # Store without compression
    z.add_bytes("raw.bin", b"\x00\x01\x02", compression="stored")

    # Compress using DEFLATE
    z.add_bytes("text.txt", b"Highly compressible text" * 100, compression="deflate")
```

---

## Python API Overview

### ZipReader

**Constructor**

- `ZipReader(path_or_file)`  
  - `path_or_file`: `str | pathlib.Path | BinaryIO`

**Core methods**

- `list() -> list[str]`  
  **List all entry names** in the archive.

- `get_info(name: str) -> ZipEntry | None`  
  **Return metadata** for a specific entry, or `None` if it does not exist.

- `open(name: str) -> BinaryIO`  
  **Open an entry** for reading decompressed data.  
  - Raises `KeyError` if the entry does not exist.
  - Validates CRC32 on read.

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

- `ZipWriter(path_or_file, mode: str = "w")`  
  - `path_or_file`: `str | pathlib.Path | BinaryIO`  
  - `mode`: currently `"w"` (write new archive)

**Core methods**

- `add_bytes(name: str, data: bytes, compression: str = "deflate") -> None`  
  Add an entry with the given `name` and in-memory `data`.

- `add_file(name_in_zip: str, source_path: str | Path, compression: str = "deflate") -> None`  
  Add an entry whose contents are read from `source_path` on disk.

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
```

- DNZIP preserves the directory tree.
- Extraction is **safe by default**: paths are validated to prevent extracting
  outside the target directory (no `../` traversal).

### Creating an archive

```bash
# Create archive.zip from a directory tree
python -m dnzip create archive.zip data_folder

# Create archive.zip from multiple paths
python -m dnzip create archive.zip file1.txt dir2 another/file3.bin

# Use STORED (no compression)
python -m dnzip create archive.zip data_folder -c stored
```

- Directory trees are preserved inside the archive.
- By default, DNZIP uses `"deflate"` compression; `"stored"` is also supported.

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

## License

Apache License 2.0. See `LICENSE` (or the license section in `pyproject.toml`) for details.

