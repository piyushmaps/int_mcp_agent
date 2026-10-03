"""Bounded format extraction with locations; unreadable files are failures."""
from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TEXT_CHARS = 2_000_000
SUPPORTED_SUFFIXES = frozenset({".md", ".markdown", ".txt", ".html", ".htm", ".pdf", ".docx", ".csv", ".json"})


class ExtractionError(ValueError):
    pass


@dataclass
class Extraction:
    text: str
    title: str = ""
    blocks: list[dict[str, str]] = field(default_factory=list)


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.main_depth = 0
        self.parts: list[str] = []
        self.main: list[str] = []
        self.title_parts: list[str] = []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "nav", "footer", "noscript", "svg"}:
            self.hidden += 1
        if tag in {"main", "article"}:
            self.main_depth += 1
        if tag == "title":
            self.in_title = True
        if tag in {"p", "div", "br", "li", "tr", "section"} or re.fullmatch("h[1-6]", tag):
            self._append("\n\n" if tag != "br" else "\n")
        if re.fullmatch("h[1-6]", tag):
            self._append("#" * int(tag[1]) + " ")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "nav", "footer", "noscript", "svg"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"main", "article"}:
            self.main_depth = max(0, self.main_depth - 1)
        if tag == "title":
            self.in_title = False
        if tag in {"td", "th"}:
            self._append(" | ")
        if tag in {"p", "div", "li", "tr"} or re.fullmatch("h[1-6]", tag):
            self._append("\n")

    def _append(self, text):
        if not self.hidden:
            self.parts.append(text)
            if self.main_depth:
                self.main.append(text)

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)
        elif not self.hidden:
            self._append(data)


def extract_html(text: str) -> Extraction:
    parser = _HTMLText()
    parser.feed(text)
    body = "".join(parser.main or parser.parts)
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", body).strip()
    return _validated(Extraction(body, "".join(parser.title_parts).strip()))


def _validated(result: Extraction) -> Extraction:
    if not result.text.strip():
        raise ExtractionError("No readable text; scanned PDFs require a separately configured OCR service")
    if len(result.text) > MAX_TEXT_CHARS:
        raise ExtractionError("Extracted text exceeds the configured limit")
    return result


def extract_bytes(data: bytes, suffix: str, filename: str = "document") -> Extraction:
    if len(data) > MAX_FILE_BYTES:
        raise ExtractionError("File exceeds the configured byte limit")
    suffix = suffix.lower()
    if suffix in {".md", ".markdown", ".txt"}:
        body = data.decode("utf-8-sig")
        match = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
        return _validated(Extraction(body, match.group(1) if match else ""))
    if suffix in {".html", ".htm"}:
        return extract_html(data.decode("utf-8-sig"))
    if suffix == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise ExtractionError("Encrypted PDFs are unsupported")
        if len(reader.pages) > 500:
            raise ExtractionError("PDF exceeds the 500-page limit")
        blocks = []
        characters = 0
        for number, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            characters += len(text)
            if characters > MAX_TEXT_CHARS:
                raise ExtractionError("PDF extracted text exceeds the configured limit")
            if text.strip():
                blocks.append({"text": text, "location": f"page {number}"})
        return _validated(Extraction("\n\n".join(b["text"] for b in blocks), blocks=blocks))
    if suffix == ".docx":
        from docx import Document as WordDocument
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > 2000 or sum(info.file_size for info in infos) > 80 * 1024 * 1024:
                raise ExtractionError("Word package exceeds decompression limits")
        document = WordDocument(io.BytesIO(data))
        blocks = []
        heading = ""
        title = ""
        # iter_inner_content retains tables' actual position in the source.
        from docx.table import Table
        for position, item in enumerate(document.iter_inner_content(), 1):
            if isinstance(item, Table):
                rows = [[cell.text.strip() for cell in row.cells] for row in item.rows]
                if rows:
                    headers = rows[0]
                    for number, row in enumerate(rows[1:] or rows, 2):
                        value = "; ".join(f"{headers[i] if i < len(headers) else i}: {cell}" for i, cell in enumerate(row))
                        blocks.append({"text": value, "location": f"{heading + ', ' if heading else ''}table {position}, row {number}"})
            elif item.text.strip():
                if item.style and item.style.name.startswith("Heading"):
                    heading = item.text.strip()
                    if not title:
                        title = heading
                blocks.append({"text": item.text, "location": heading or f"paragraph {position}"})
        return _validated(Extraction("\n\n".join(b["text"] for b in blocks), title, blocks))
    if suffix == ".csv":
        blocks = []
        reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
        for index, row in enumerate(reader, 2):
            if index > 10002:
                raise ExtractionError("CSV exceeds 10,000 rows; partition it before ingestion")
            blocks.append({"text": "; ".join(f"{key}: {value}" for key, value in row.items()), "location": f"row {index}"})
        return _validated(Extraction("\n".join(b["text"] for b in blocks), blocks=blocks))
    if suffix == ".json":
        value = json.loads(data.decode("utf-8-sig"))
        return _validated(Extraction(json.dumps(value, ensure_ascii=False, indent=2)))
    raise ExtractionError(f"Unsupported input format: {suffix}")


def extract_file(path: str | Path) -> Extraction:
    path = Path(path)
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ExtractionError("File exceeds the configured byte limit")
    with path.open("rb") as stream:
        data = stream.read(MAX_FILE_BYTES + 1)
    return extract_bytes(data, path.suffix, path.name)


def extract_file_isolated(path: str | Path, timeout: float = 10) -> Extraction:
    """Parse in a separate process with a deadline and bounded result file.

    This is process fault isolation, not a complete OS security sandbox. Deploy
    untrusted parsers in an OS/container sandbox before accepting public uploads.
    """
    import os
    import subprocess
    import sys
    import tempfile
    path = Path(path).resolve(strict=True)
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ExtractionError("File exceeds the configured byte limit")
    project_root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="atlas-parser-") as directory:
        result_path = Path(directory) / "extraction.json"
        try:
            process = subprocess.run(
                [sys.executable, "-m", "src.ingestion.parser_worker", str(path), str(result_path)],
                cwd=project_root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=timeout, check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except subprocess.TimeoutExpired as exc:
            raise ExtractionError("Parser exceeded its deadline") from exc
        if process.returncode or not result_path.is_file():
            raise ExtractionError("Parser could not extract this file")
        if result_path.stat().st_size > 20 * 1024 * 1024:
            raise ExtractionError("Parser result exceeds the byte limit")
        value = json.loads(result_path.read_text(encoding="utf-8"))
        return _validated(Extraction(**value))
