"""Hosted MinerU source types. A filename alone is not provenance."""

from io import BytesIO
from pathlib import Path
from struct import unpack_from
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile
import zlib

from .domain import ValidationError


ORIGINAL_MIME = {
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
ARCHIVE_INPUT_LIMIT = 50_000_000
_MAIN_PART = {"docx": "word/document.xml", "xlsx": "xl/workbook.xml", "pptx": "ppt/presentation.xml"}
_OLE_STREAMS = {"doc": {"WordDocument"}, "xls": {"Workbook", "Book"}, "ppt": {"PowerPoint Document"}}


def read_source(path: Path, limit: int, label: str) -> bytes:
    try:
        if not path.is_file():
            raise ValidationError(f"source must be an existing {label} file")
        size = path.stat().st_size
        if not 0 < size <= limit:
            raise ValidationError(f"{label} exceeds the configured input size limit")
        with path.open("rb") as stream:
            raw = stream.read(size + 1)
    except OSError:
        raise ValidationError(f"cannot read {label} source") from None
    if len(raw) != size:
        raise ValidationError(f"{label} source changed during read")
    return raw


class _ManifestTree(ElementTree.TreeBuilder):
    def doctype(self, *args):
        raise ValueError("unexpected DTD in package manifest")


def _ooxml_matches(raw: bytes, ext: str) -> bool:
    # Bound the central directory before ZipFile allocates one object per entry.
    if not raw.startswith(b"PK\x03\x04") or raw.count(b"PK\x01\x02") > 20_000:
        return False
    with ZipFile(BytesIO(raw)) as archive:
        entries = archive.infolist()
        if (len(set(archive.namelist())) != len(entries) or any(info.flag_bits & 1 for info in entries)
                or sum(info.file_size for info in entries) > 512_000_000):
            return False
        manifest = archive.getinfo("[Content_Types].xml")
        main = _MAIN_PART[ext]
        if not archive.getinfo(main).file_size or manifest.file_size > 512_000:
            return False
        with archive.open(manifest) as stream:
            xml = stream.read(512_001)
        root = ElementTree.fromstring(xml, parser=ElementTree.XMLParser(target=_ManifestTree()))
        ns = "{http://schemas.openxmlformats.org/package/2006/content-types}"
        if root.tag != ns + "Types":
            return False
        # OPC resolves an explicit part override before the extension default.
        main_types = {ORIGINAL_MIME[kind] + ".main+xml" for kind in _MAIN_PART}
        overrides = []
        for entry in root.iterfind(ns + "Override"):
            part, mime = entry.get("PartName"), entry.get("ContentType")
            if part == "/" + main:
                overrides.append(mime)
            elif mime in main_types:
                return False
        defaults = [entry.get("ContentType") for entry in root.iterfind(ns + "Default")
                    if entry.get("Extension", "").lower() == "xml"]
        return (overrides or defaults) == [ORIGINAL_MIME[ext] + ".main+xml"]


def _ole_matches(raw: bytes, ext: str) -> bool:
    # MS-CFB: inspect only the root's directory tree, not stream contents or
    # embedded objects (a Word document may itself contain an Excel workbook).
    if len(raw) < 512 or raw[:8] != bytes.fromhex("d0cf11e0a1b11ae1"):
        return False
    major, order, shift = unpack_from("<HHH", raw, 26)
    if order != 0xfffe or (major, shift) not in ((3, 9), (4, 12)):
        return False
    size = 1 << shift
    if len(raw) % size:
        return False
    count = len(raw) // size - 1

    def offset(sector):
        if sector >= count:
            raise ValueError
        return (sector + 1) * size

    # Collect FAT sector locations from the header and any extension sectors.
    fat_count, directory = unpack_from("<II", raw, 44)
    difat, extensions = unpack_from("<II", raw, 68)
    per_sector = size // 4
    if not 0 < fat_count <= count or extensions != (max(0, fat_count - 109) + per_sector - 2) // (per_sector - 1):
        return False
    fat = list(unpack_from("<109I", raw, 76))
    for _ in range(extensions):
        words = unpack_from(f"<{per_sector}I", raw, offset(difat))
        fat.extend(words[:-1])
        difat = words[-1]

    directories, seen = [], set()
    while directory != 0xfffffffe:
        if directory in seen or len(seen) * size >= 4_194_304:
            return False
        seen.add(directory)
        directories.append(offset(directory))
        index, word = divmod(directory, per_sector)
        if index >= fat_count:
            return False
        directory = unpack_from("<I", raw, offset(fat[index]) + word * 4)[0]

    if not directories or raw[directories[0] + 66] != 5:
        return False
    todo = [unpack_from("<I", raw, directories[0] + 76)[0]]
    seen.clear()
    matches = set()
    while todo:
        entry = todo.pop()
        if entry == 0xffffffff:
            continue
        sector, slot = divmod(entry, size // 128)
        if sector >= len(directories) or entry in seen:
            return False
        seen.add(entry)
        start = directories[sector] + slot * 128
        length, kind = unpack_from("<HB", raw, start + 64)
        if kind not in (1, 2) or not 2 <= length <= 64 or length % 2:
            return False
        if kind == 2:
            matches.add(raw[start:start + length - 2].decode("utf-16le"))
        todo.extend(unpack_from("<II", raw, start + 68))
    return [kind for kind, names in _OLE_STREAMS.items() if matches & names] == [ext]


def validate_office(raw: bytes, ext: str) -> None:
    try:
        matches = _ooxml_matches(raw, ext) if ext in _MAIN_PART else _ole_matches(raw, ext)
    except (BadZipFile, LookupError, ValueError, RuntimeError, OSError, EOFError, ElementTree.ParseError, zlib.error):
        matches = False
    if not matches:
        raise ValidationError(f"invalid or mismatched {ext.upper()} Office source")
