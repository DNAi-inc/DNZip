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
Streaming ZIP write support with data descriptors.

This module provides support for streaming write operations where the size
of the data is not known in advance, using data descriptors.
"""

from typing import BinaryIO

from .writer import ZipWriter


class StreamingZipWriter(ZipWriter):
    """Streaming writer for ZIP archives with data descriptors.

    This class extends ZipWriter to provide streaming capabilities where
    the size of data is not known in advance. It uses data descriptors
    to write the actual sizes after the compressed data.

    Example:
        with StreamingZipWriter("archive.zip") as z:
            with open("large_file.bin", "rb") as f:
                z.add_stream("large.bin", f)
    """

    def add_stream(
        self, name: str, stream: BinaryIO, compression: str = "deflate"
    ) -> None:
        """Add an entry from a stream (unknown size).

        This method reads data from the stream and adds it to the archive
        using data descriptors. The local file header is written with zero
        sizes, and a data descriptor is written after the compressed data
        with the actual sizes and CRC32.

        Args:
            name: Entry name (path within ZIP archive).
            stream: Binary file-like object to read from.
            compression: Compression method ("stored", "deflate", etc.).

        Raises:
            ZipFormatError: If the archive is closed.
            ZipUnsupportedFeature: If compression method is not supported.
        """
        # Read all data from stream
        # Note: True streaming with on-the-fly compression would require
        # more complex implementation. For now, we read all data first.
        data = stream.read()
        self.add_bytes(name, data, compression, use_data_descriptor=True)
