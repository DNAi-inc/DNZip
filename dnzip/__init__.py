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
DNZIP - ZIP64 Python Library - Pure Python ZIP engine with full ZIP64 support.

This library provides a pure Python implementation for reading and writing
standard ZIP and ZIP64 archives, using only Python standard library modules.
"""

from .reader import ZipReader
from .writer import ZipWriter
from .stream import StreamingZipWriter

__all__ = ["ZipReader", "ZipWriter", "StreamingZipWriter"]

__version__ = "0.1.2"

