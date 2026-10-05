import io
import struct
import unittest
import zlib

from dnzip import ZipReader
from dnzip.errors import ZipFormatError


def _single_file_archive(filename: bytes, flags: int) -> bytes:
    payload = b"payload"
    checksum = zlib.crc32(payload) & 0xFFFFFFFF

    local_header = struct.pack(
        "<IHHHHHIIIHH",
        0x04034B50,
        20,
        flags,
        0,
        0,
        0,
        checksum,
        len(payload),
        len(payload),
        len(filename),
        0,
    )
    local_record = local_header + filename + payload

    central_header = struct.pack(
        "<IHHHHHHIIIHHHHHII",
        0x02014B50,
        20,
        20,
        flags,
        0,
        0,
        0,
        checksum,
        len(payload),
        len(payload),
        len(filename),
        0,
        0,
        0,
        0,
        0,
        0,
    )
    central_record = central_header + filename

    eocd = struct.pack(
        "<IHHHHIIH",
        0x06054B50,
        0,
        0,
        1,
        1,
        len(central_record),
        len(local_record),
        0,
    )
    return local_record + central_record + eocd


class TestFilenameEncoding(unittest.TestCase):
    def test_legacy_filename_uses_cp437(self) -> None:
        archive_bytes = _single_file_archive(b"caf\x82.txt", flags=0)

        with ZipReader(io.BytesIO(archive_bytes)) as archive:
            self.assertEqual(archive.list(), ["café.txt"])
            with archive.open("café.txt") as entry:
                self.assertEqual(entry.read(), b"payload")

    def test_utf8_flag_uses_utf8(self) -> None:
        name = "café.txt".encode("utf-8")
        archive_bytes = _single_file_archive(name, flags=0x0800)

        with ZipReader(io.BytesIO(archive_bytes)) as archive:
            self.assertEqual(archive.list(), ["café.txt"])

    def test_invalid_utf8_name_is_rejected(self) -> None:
        archive_bytes = _single_file_archive(b"caf\x82.txt", flags=0x0800)

        with self.assertRaises(ZipFormatError):
            ZipReader(io.BytesIO(archive_bytes))


if __name__ == "__main__":
    unittest.main()
