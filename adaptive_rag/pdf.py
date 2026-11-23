"""PDF-to-Markdown adapters. Cloud upload is an explicit parser choice.

The hosted MinerU v4 API differs from MinerU's self-hosted v1 Tasks API.
Only the hosted protocol is implemented here. No credentials are loaded here.
"""

from dataclasses import dataclass, field
from contextlib import closing
from hashlib import sha256
from io import BytesIO
import ipaddress
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
from time import monotonic, sleep
from typing import Any
from urllib.parse import urlsplit
from zipfile import BadZipFile, ZipFile
import zlib

import httpx

from .domain import CapabilityError, ValidationError
from .figures import ARCHIVE_REFERENCE, Figure
from .mineru_formats import ARCHIVE_INPUT_LIMIT, ORIGINAL_MIME, read_source, validate_office

# Pictures MinerU exports next to full.md. Over the limits a picture is skipped, not the document.
MAX_FIGURES = 200
MAX_FIGURE_BYTES = 10_000_000
MAX_FIGURE_TOTAL_BYTES = 60_000_000
_CONTENT_LIST_BYTES = 8_000_000
_ARCHIVE_READ_ERRORS = (BadZipFile, OSError, RuntimeError, EOFError, zlib.error)


@dataclass(frozen=True)
class PdfSettings:
    api_key: str = field(default="", repr=False)
    base_url: str = "https://mineru.net/api/v4"
    timeout_seconds: float = 180.0
    poll_interval_seconds: float = 2.0
    max_wait_seconds: float = 300.0
    model_version: str = "vlm"
    language: str = "ch"
    max_input_bytes: int = 50_000_000
    max_download_bytes: int = 80_000_000
    max_text_bytes: int = 16_000_000
    max_pages: int = 200
    storage_domains: tuple[str, ...] = (
        "aliyuncs.com", "openxlab.org.cn", "mineru.net", "shlab.tech",
    )

    def __post_init__(self):
        if not isinstance(self.api_key, str) or any(c.isspace() for c in self.api_key):
            raise ValidationError("MinerU API key must be a string without whitespace")
        _https_url(self.base_url)
        parts = urlsplit(self.base_url)
        if parts.query or parts.fragment:
            raise ValidationError("MinerU base URL cannot contain a query or fragment")
        if self.model_version not in ("vlm", "pipeline"):
            raise ValidationError("PDF model_version must be vlm or pipeline")
        if not isinstance(self.language, str) or not re.fullmatch(r"[A-Za-z_-]{1,30}", self.language):
            raise ValidationError("invalid PDF language")
        for name in ("timeout_seconds", "poll_interval_seconds", "max_wait_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 3600:
                raise ValidationError(f"{name} must be finite and between 0 and 3600")
        for name in ("max_input_bytes", "max_download_bytes", "max_text_bytes", "max_pages"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValidationError(f"{name} must be a positive integer")
        if not isinstance(self.storage_domains, tuple) or not self.storage_domains or any(
            not isinstance(domain, str) or not re.fullmatch(r"[a-z0-9.-]+", domain) or domain.startswith(".")
            for domain in self.storage_domains
        ):
            raise ValidationError("storage_domains must contain trusted DNS suffixes")


@dataclass(frozen=True)
class ParsedPdf:
    markdown: str
    pages: int | None
    backend: str
    duration_seconds: float
    metadata: dict[str, Any] = field(default_factory=dict)
    # Pictures the Markdown refers to. Only the MinerU backend exports any.
    figures: tuple[Figure, ...] = ()


def _checkpoint_error(action: str, operation: str, error: OSError) -> CapabilityError:
    # OSError text can contain sensitive paths. Only numeric platform codes
    # and a fixed operation name are safe to expose for local I/O diagnosis.
    codes = []
    for name in ("errno", "winerror"):
        value = getattr(error, name, None)
        if type(value) is int:
            codes.append(f"{name}={value}")
    detail = ", ".join(codes) if codes else "no system error code"
    return CapabilityError(f"cannot {action} MinerU checkpoint during {operation} ({detail}); automatic progress stopped")


class _MinerUCheckpoint:
    """Optional single-caller recovery record; never stores keys or signed URLs."""

    def __init__(self, path, source: Path, digest: str, settings: PdfSettings):
        self.path = Path(path) if path is not None else None
        self.binding = {
            "version": 1, "backend": "mineru", "source_sha256": digest,
            "file_name": source.name, "base_url": settings.base_url.rstrip("/"),
            "model_version": settings.model_version, "language": settings.language,
        }
        if self.path is not None and self.path.resolve() == source.resolve():
            raise ValidationError("MinerU checkpoint cannot replace the PDF source")

    def load(self):
        if self.path is None:
            return None
        try:
            with self.path.open("rb") as handle:
                raw = handle.read(16_385)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise _checkpoint_error("read", "read", error) from None
        try:
            value = json.loads(raw) if len(raw) <= 16_384 else None
        except (ValueError, UnicodeError):
            value = None
        if (not isinstance(value, dict)
                or set(value) != set(self.binding) | {"phase", "batch_id"}
                or type(value.get("version")) is not int
                or any(value.get(name) != expected for name, expected in self.binding.items())
                or value.get("phase") not in ("submitting", "allocated", "uploaded", "complete")):
            raise ValidationError("MinerU checkpoint is invalid or does not match the PDF and parser settings")
        batch_id = value.get("batch_id")
        if value["phase"] == "submitting":
            if batch_id is not None:
                raise ValidationError("MinerU checkpoint has an invalid submission phase")
            raise CapabilityError("MinerU submission outcome is unknown; checkpoint retained and automatic resubmission disabled")
        if not isinstance(batch_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", batch_id):
            raise ValidationError("MinerU checkpoint has an invalid batch ID")
        return value

    def save(self, phase: str, batch_id: str | None = None):
        if self.path is None:
            return
        temporary = None
        operation = "mkdir"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            operation = "create temporary file"
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent,
                    prefix=self.path.name + ".", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                operation = "write"
                json.dump({**self.binding, "phase": phase, "batch_id": batch_id}, handle)
                operation = "flush"
                handle.flush()
                operation = "fsync"
                os.fsync(handle.fileno())
                operation = "close temporary file"
            operation = "replace"
            os.replace(temporary, self.path)
        except OSError as error:
            raise _checkpoint_error("save", operation, error) from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


def _https_url(value: str) -> str:
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        raise ValidationError("invalid remote document URL")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ValidationError("invalid remote document URL") from None
    host = parts.hostname
    if parts.scheme != "https" or not host or parts.username or parts.password or port not in (None, 443):
        raise ValidationError("remote document URL requires HTTPS without credentials")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if (address is not None and not address.is_global) or host == "localhost" or host.endswith((".localhost", ".local")):
        raise ValidationError("remote document URL cannot target a local address")
    return value


def _input(path: str | Path, settings: PdfSettings) -> tuple[Path, bytes]:
    source = Path(path)
    if source.suffix.lower() != ".pdf":
        raise ValidationError("source must be an existing PDF file")
    return source, read_source(source, settings.max_input_bytes, "PDF")


def _reader(raw: bytes, settings: PdfSettings):
    if not raw[:1024].lstrip().startswith(b"%PDF-"):
        raise ValidationError("invalid PDF header or input size")
    try:
        from pypdf import PdfReader
    except ImportError:
        raise CapabilityError("PDF parsing requires the pypdf dependency") from None
    try:
        reader = PdfReader(BytesIO(raw), strict=False)
        if reader.is_encrypted:
            raise ValidationError("encrypted PDFs are not supported")
        if not 1 <= len(reader.pages) <= settings.max_pages:
            raise ValidationError("PDF exceeds the configured page limit or contains no pages")
        return reader
    except ValidationError:
        raise
    except Exception:
        raise ValidationError("cannot decode PDF structure") from None


class LocalPdfParser:
    """Extract embedded text page by page. No OCR or semantic table recovery."""

    def __init__(self, settings: PdfSettings | None = None):
        self.settings = settings or PdfSettings()

    def parse(self, path: str | Path) -> ParsedPdf:
        started = monotonic()
        _, raw = _input(path, self.settings)
        reader = _reader(raw, self.settings)
        sections, counts = [], []
        total = 0
        try:
            for number, page in enumerate(reader.pages, 1):
                text = (page.extract_text(extraction_mode="layout") or "").strip()
                total += len(text.encode("utf-8"))
                if total > self.settings.max_text_bytes:
                    raise ValidationError("PDF extracted text exceeds the configured size limit")
                counts.append({"page": number, "characters": len(text)})
                sections.append(f"## Page {number}\n\n{text}")
        except ValidationError:
            raise
        except Exception:
            raise ValidationError("PDF text extraction failed") from None
        if not any(item["characters"] for item in counts):
            raise CapabilityError("PDF has no extractable text; choose an OCR-capable parser")
        return ParsedPdf("\n\n".join(sections).strip() + "\n", len(counts), "local",
                         round(monotonic() - started, 4), {
                             "source_sha256": sha256(raw).hexdigest(), "page_text": counts,
                             "ocr": False, "formula_structure": False, "table_structure": False,
                         })


class MinerUPdfParser:
    """Explicit cloud upload, bounded polling, in-memory ZIP Markdown reading.

    A timeout after upload may leave a remote job running. This adapter never
    automatically retries submission, and does not claim remote exactly-once.
    Signed URLs and provider response bodies are excluded from errors/results.
    """

    def __init__(self, settings: PdfSettings, *, client: httpx.Client | None = None,
                 clock=monotonic, sleeper=sleep):
        self.settings, self.client = settings, client
        self.clock, self.sleeper = clock, sleeper

    def _storage_url(self, value: str) -> str:
        _https_url(value)
        host = urlsplit(value).hostname.lower()
        if not any(host == domain or host.endswith("." + domain) for domain in self.settings.storage_domains):
            raise ValidationError("MinerU returned an untrusted storage domain")
        return value

    def parse(self, path: str | Path, *, checkpoint_path: str | Path | None = None) -> ParsedPdf:
        started = self.clock()
        source, raw = _input(path, self.settings)
        pages = len(_reader(raw, self.settings).pages)
        return self._parse_validated(source, raw, pages, started, "mineru", checkpoint_path)

    def parse_original(self, path: str | Path) -> tuple[ParsedPdf, bytes]:
        """Parse a supported original and return the exact validated bytes uploaded."""
        started = self.clock()
        source = Path(path)
        ext = source.suffix[1:].lower()
        if ext not in ORIGINAL_MIME:
            raise ValidationError("unsupported MinerU source format")
        raw = read_source(source, min(self.settings.max_input_bytes, ARCHIVE_INPUT_LIMIT), ext.upper())
        if ext == "pdf":
            pages, parser = len(_reader(raw, self.settings).pages), "mineru"
        else:
            validate_office(raw, ext)
            pages, parser = None, f"mineru:{ext}"
        # v4 uses the extension to select conversion. Don't disclose the local filename.
        return self._parse_validated(Path(f"upload.{ext}"), raw, pages, started, parser), raw

    def _parse_validated(self, source, raw, pages, started, parser, checkpoint_path=None):
        deadline = started + self.settings.max_wait_seconds
        if not self.settings.api_key:
            raise CapabilityError("MinerU API key is not configured")
        data_id = sha256(raw).hexdigest()
        checkpoint = _MinerUCheckpoint(checkpoint_path, source, data_id, self.settings)
        saved = checkpoint.load()
        owns_client = self.client is None
        client = self.client or httpx.Client(follow_redirects=False, trust_env=False)
        headers = {"Authorization": f"Bearer {self.settings.api_key}"}
        base = self.settings.base_url.rstrip("/")
        calls = 0

        def remaining():
            budget = deadline - self.clock()
            if budget <= 0:
                raise CapabilityError("MinerU parsing timed out; a submitted remote job may still be running")
            return min(self.settings.timeout_seconds, budget)

        def request(method, url, *, limit=2_000_000, **kwargs):
            nonlocal calls
            timeout = remaining()
            calls += 1
            try:
                prepared = client.build_request(method, url, timeout=timeout, **kwargs)
                if "Authorization" not in kwargs.get("headers", {}):
                    prepared.headers.pop("Authorization", None)
                with closing(client.send(prepared, stream=True, auth=None, follow_redirects=False)) as response:
                    if not 200 <= response.status_code < 300:
                        if response.status_code in (401, 403):
                            raise CapabilityError("MinerU authentication failed")
                        raise CapabilityError(f"MinerU request failed with HTTP {response.status_code}")
                    body = bytearray()
                    for part in response.iter_bytes(chunk_size=65536):
                        remaining()
                        if len(body) + len(part) > limit:
                            raise CapabilityError("MinerU response exceeds the configured size limit")
                        body.extend(part)
                    result = httpx.Response(response.status_code, content=bytes(body),
                                            headers=response.headers, request=prepared)
            except httpx.HTTPError:
                raise CapabilityError("MinerU network request failed") from None
            remaining()
            return result

        def api(method, suffix, **kwargs):
            response = request(method, base + suffix, headers=headers, **kwargs)
            if len(response.content) > 2_000_000:
                raise CapabilityError("MinerU returned an oversized API response")
            try:
                value = response.json()
            except (ValueError, UnicodeError):
                raise CapabilityError("MinerU returned invalid JSON") from None
            if not isinstance(value, dict) or type(value.get("code")) is not int or value["code"] != 0:
                raise CapabilityError("MinerU rejected the parsing request")
            data = value.get("data")
            if not isinstance(data, dict):
                raise CapabilityError("MinerU returned invalid response data")
            return data

        try:
            if saved is None:
                # Record uncertainty before the effect: a lost POST response must
                # never silently become a second remote submission on restart.
                checkpoint.save("submitting")
                data = api("POST", "/file-urls/batch", json={
                    "files": [{"name": source.name, "data_id": data_id}],
                    "model_version": self.settings.model_version, "enable_formula": True,
                    "enable_table": True, "language": self.settings.language,
                })
                batch_id, urls = data.get("batch_id"), data.get("file_urls")
                if not isinstance(batch_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", batch_id) or not isinstance(urls, list) or len(urls) != 1:
                    raise CapabilityError("MinerU returned invalid upload instructions")
                checkpoint.save("allocated", batch_id)
                upload_url = self._storage_url(urls[0])
                # Signed storage requests intentionally receive no API bearer header.
                request("PUT", upload_url, content=raw)
                checkpoint.save("uploaded", batch_id)
            else:
                batch_id = saved["batch_id"]
            states = []
            while True:
                value = api("GET", f"/extract-results/batch/{batch_id}")
                entries = value.get("extract_result")
                if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
                    raise CapabilityError("MinerU returned invalid task results")
                item = entries[0]
                if item.get("data_id") not in (None, data_id) or item.get("file_name") not in (None, source.name):
                    raise CapabilityError("MinerU returned a mismatched task result")
                state = item.get("state")
                if state not in ("done", "failed", "pending", "running", "waiting-file", "converting"):
                    raise CapabilityError("MinerU returned an unknown task state")
                if not states or states[-1] != state:
                    states.append(state)
                if state == "failed":
                    raise CapabilityError("MinerU document parsing failed")
                if saved is not None and saved["phase"] == "allocated" and state == "waiting-file":
                    raise CapabilityError("MinerU batch is waiting for an unconfirmed upload; checkpoint retained and automatic resubmission disabled")
                if state == "done":
                    archive_url = self._storage_url(item.get("full_zip_url"))
                    break
                delay = min(self.settings.poll_interval_seconds, remaining())
                self.sleeper(delay)
            try:
                archive = request("GET", archive_url, limit=self.settings.max_download_bytes).content
            except CapabilityError:
                raise CapabilityError("MinerU result download failed or archive exceeds the configured size limit") from None
            markdown, figures, archive_metadata = self._markdown(archive)
            checkpoint.save("complete", batch_id)
            return ParsedPdf(markdown, pages, parser, round(self.clock() - started, 4), {
                "source_sha256": data_id, "model_version": self.settings.model_version,
                "batch_id": batch_id, "resumed_from_checkpoint": saved is not None,
                "states": states, "network_calls": calls,
                "formula_recognition_requested": True, "table_recognition_requested": True,
                **archive_metadata,
            }, figures)
        finally:
            if owns_client:
                client.close()

    def _markdown(self, raw: bytes) -> tuple[str, tuple[Figure, ...], dict]:
        try:
            with ZipFile(BytesIO(raw)) as archive:
                infos = archive.infolist()
                if len(infos) > 5000:
                    raise CapabilityError("MinerU archive contains too many entries")
                names = set()
                for info in infos:
                    path = PurePosixPath(info.filename)
                    mode = info.external_attr >> 16
                    if (not info.filename or chr(92) in info.filename or path.is_absolute() or ".." in path.parts
                            or "\x00" in info.filename or re.match(r"^[A-Za-z]:", info.filename)
                            or stat.S_ISLNK(mode) or info.flag_bits & 1 or info.filename in names):
                        raise CapabilityError("MinerU archive contains unsafe entries")
                    names.add(info.filename)
                markdown_files = [info for info in infos if PurePosixPath(info.filename).name == "full.md" and not info.is_dir()]
                if len(markdown_files) != 1:
                    raise CapabilityError("MinerU archive must contain exactly one full.md")
                selected = markdown_files[0]
                if selected.file_size > self.settings.max_text_bytes:
                    raise CapabilityError("MinerU Markdown exceeds the configured size limit")
                with archive.open(selected) as handle:
                    text = handle.read(self.settings.max_text_bytes + 1)
                if len(text) > self.settings.max_text_bytes:
                    raise CapabilityError("MinerU Markdown exceeds the configured size limit")
                markdown = text.decode("utf-8-sig")
                removed_nuls = markdown.count("\x00")
                markdown = markdown.replace("\x00", "").strip()
                if not markdown:
                    raise CapabilityError("MinerU returned empty Markdown")
                # Markdown is inert data: no asset fetching, HTML execution or ZIP extraction.
                # Pictures are read into memory under fixed limits, never written to disk.
                figures, figure_metadata = _figures(
                    archive, {info.filename: info for info in infos if not info.is_dir()},
                    selected.filename, markdown)
                return markdown + "\n", figures, {"archive_entries": len(infos), "markdown_bytes": len(text),
                    "removed_nul_characters": removed_nuls,
                    "formula_blocks": len(re.findall(r"\$\$[\s\S]*?\$\$", markdown)),
                    "html_tables": len(re.findall(r"<table(?:\s|>)", markdown, re.I)),
                    **figure_metadata}
        except (UnicodeError, *_ARCHIVE_READ_ERRORS):
            raise CapabilityError("MinerU returned an invalid result archive") from None


def _figure_details(archive: ZipFile, entries: dict, folder: PurePosixPath) -> dict[str, tuple[str, int | None]]:
    """Caption and page per picture path, from MinerU's `*_content_list.json`; empty when it is absent.

    Only `image` and `chart` items count. Table screenshots are skipped on
    purpose: their HTML is already in the Markdown. MinerU puts a chart's
    caption in its footnote field, so captions are tried before footnotes.
    """
    names = [name for name in entries
             if name.endswith("_content_list.json") and PurePosixPath(name).parent == folder]
    if len(names) != 1 or entries[names[0]].file_size > _CONTENT_LIST_BYTES:
        return {}
    try:
        items = json.loads(archive.read(entries[names[0]]))
    except (ValueError, *_ARCHIVE_READ_ERRORS):
        return {}
    details: dict[str, tuple[str, int | None]] = {}
    for item in items if isinstance(items, list) else ():
        kind = item.get("type") if isinstance(item, dict) else None
        path = item.get("img_path") if kind in ("image", "chart") else None
        if not isinstance(path, str) or path in details:
            continue
        caption = ""
        for name in (f"{kind}_caption", f"{kind}_footnote", "content"):
            value = item.get(name)
            parts = value if isinstance(value, list) else [value]
            caption = " ".join(part.strip() for part in parts if isinstance(part, str) and part.strip())
            if caption:
                break
        index = item.get("page_idx")
        details[path] = (caption, index + 1 if type(index) is int and 0 <= index < 99_999 else None)
    return details


def _figures(archive: ZipFile, entries: dict, markdown_name: str, markdown: str) -> tuple[tuple[Figure, ...], dict]:
    """The pictures `full.md` points at, read under count and size limits; unreadable ones are left out."""
    folder = PurePosixPath(markdown_name).parent
    referenced = list(dict.fromkeys(match["path"] for match in ARCHIVE_REFERENCE.finditer(markdown)))
    details = _figure_details(archive, entries, folder) if referenced else {}
    figures, total = [], 0
    for path in referenced:
        info = entries.get((folder / path).as_posix())
        if (info is None or len(figures) >= MAX_FIGURES or info.file_size > MAX_FIGURE_BYTES
                or total + info.file_size > MAX_FIGURE_TOTAL_BYTES):
            continue
        try:
            with archive.open(info) as handle:
                content = handle.read(MAX_FIGURE_BYTES + 1)
        except _ARCHIVE_READ_ERRORS:
            continue
        if not content or len(content) > MAX_FIGURE_BYTES:
            continue
        total += len(content)
        caption, page = details.get(path, ("", None))
        figures.append(Figure(path, content, caption, page))
    return tuple(figures), {"figures_referenced": len(referenced), "figures_extracted": len(figures)}


def parse_pdf(path: str | Path, backend: str = "local", settings: PdfSettings | None = None,
              *, client: httpx.Client | None = None, checkpoint_path: str | Path | None = None) -> ParsedPdf:
    """Parse explicitly with local embedded-text extraction or cloud MinerU."""
    settings = settings or PdfSettings()
    if backend == "local":
        if checkpoint_path is not None:
            raise ValidationError("PDF checkpoint_path requires the mineru backend")
        return LocalPdfParser(settings).parse(path)
    if backend == "mineru":
        return MinerUPdfParser(settings, client=client).parse(path, checkpoint_path=checkpoint_path)
    raise ValidationError("PDF backend must be local or mineru")
