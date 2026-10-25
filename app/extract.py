"""
extract.py
- Turns any uploaded file into plain text ("raw data").
- Supported, with no extra programs to install:
    PDF                     -> PyMuPDF (and vision for image-only pages)
    Word (.docx)            -> python-docx
    PowerPoint (.pptx)      -> python-pptx
    Excel (.xlsx, .xlsm)    -> openpyxl
    CSV / TXT / Markdown    -> read directly
    Images (.png .jpg ...)  -> OpenAI vision reads the text
    Google Docs/Sheets/Slides -> use the file you exported them as (.docx/.xlsx/.pptx/.pdf)

Every step is logged: filename, type, characters extracted, or the full error.
"""
import io
import os
import shutil
import zipfile

import fitz  # PyMuPDF
from docx import Document as DocxDocument
from pptx import Presentation
from openpyxl import load_workbook

from . import ai, config
from . import ocr
from .logger import get_logger

log = get_logger()


def read_image_text(image_bytes: bytes, mime: str = "image/png") -> str:
    """Read text from an image. LOCAL Tesseract first (free, no rate limit);
    fall back to the cloud vision model only when local OCR is too weak.
    Never raises on a cloud error — keeps whatever local text we have."""
    # Very large images make Tesseract crawl; downscale (content preserved) before OCR.
    image_bytes = _downscale_for_ocr(image_bytes)
    local_text, conf, nwords = "", 0.0, 0
    if ocr.available():
        local_text, conf, nwords = ocr.ocr_image(image_bytes)
        log.info("Local OCR | conf=%.0f words=%d chars=%d", conf, nwords, len(local_text))

    # Good enough locally? Then we're done — no cloud call at all.
    good = (nwords >= 30) or (nwords >= 8 and conf >= 60)
    if good:
        return local_text

    # Local weak/unavailable -> try cloud vision, but keep local text as a safety net.
    try:
        v = (ai.read_text_from_image(image_bytes, mime) or "").strip()
        if len(v) >= len(local_text):
            log.info("Used vision fallback | %d chars", len(v))
            return v
    except Exception as e:
        log.warning("Vision fallback skipped (%s) | keeping local OCR text (%d chars)",
                    type(e).__name__, len(local_text))
    return local_text


def _downscale_for_ocr(image_bytes: bytes) -> bytes:
    """If an image is larger than config.MAX_IMAGE_PIXELS, shrink it (keeping aspect
    ratio) so OCR stays fast. Returns original bytes on any error."""
    try:
        from PIL import Image
        import io as _io
        im = Image.open(_io.BytesIO(image_bytes))
        w, h = im.size
        if w * h <= config.MAX_IMAGE_PIXELS:
            return image_bytes
        scale = (config.MAX_IMAGE_PIXELS / (w * h)) ** 0.5
        im = im.convert("RGB").resize((max(1, int(w * scale)), max(1, int(h * scale))))
        out = _io.BytesIO()
        im.save(out, format="PNG")
        log.info("Downscaled image for OCR | %dx%d -> %dx%d", w, h, im.size[0], im.size[1])
        return out.getvalue()
    except Exception as e:
        log.warning("Image downscale skipped (%s)", type(e).__name__)
        return image_bytes
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"}
ARCHIVE_EXTS = {".zip"}
# Newer/open formats we read natively (no external program needed).
OPENDOC_EXTS = {".odt", ".ods", ".odp"}   # LibreOffice / OpenOffice (via odfpy)
RTF_EXTS = {".rtf"}                        # WordPad / rich text (via striprtf)
# Legacy Microsoft Office (binary): can't be parsed by python-docx/pptx/openpyxl.
# We convert these to PDF via LibreOffice if it's installed; otherwise we tell the
# user to re-save as the modern .docx/.pptx/.xlsx.
LEGACY_OFFICE_EXTS = {".doc", ".ppt", ".xls"}
# File types we know how to read (used to filter junk out of archives).
SUPPORTED_EXTS = (
    {".pdf", ".docx", ".pptx", ".xlsx", ".xlsm", ".csv", ".txt", ".md", ".tsv"}
    | IMAGE_EXTS | OPENDOC_EXTS | RTF_EXTS | LEGACY_OFFICE_EXTS
)
# Binary/media types we cannot turn into study text — these are EXCLUDED (not failed),
# with a clear message, instead of being read as garbage plain text.
UNSUPPORTED_EXTS = {
    # video
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm", ".m4v", ".mpeg", ".mpg",
    # audio
    ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".wma",
    # other binaries that aren't class text
    ".exe", ".dmg", ".iso", ".app", ".bin", ".psd", ".ai", ".sketch",
    ".ttf", ".otf", ".woff", ".woff2",
}


class UnsupportedFile(Exception):
    """Raised for media/binary files we deliberately skip (video, audio, etc.)."""


def is_unsupported(filename: str) -> bool:
    return os.path.splitext(filename)[1].lower() in UNSUPPORTED_EXTS

MIME_BY_EXT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".tiff": "image/tiff",
}


def is_archive(filename: str) -> bool:
    return os.path.splitext(filename)[1].lower() in ARCHIVE_EXTS


def expand_upload(filename: str, data: bytes, _depth: int = 0) -> list:
    """
    Turn one upload into a flat list of (name, bytes) files.
    - A normal file -> [(filename, data)].
    - A .zip -> every supported file inside it, folders flattened, junk skipped.
      Nested zips are expanded too (up to 3 levels deep).
    """
    if not is_archive(filename) or _depth > 3:
        return [(filename, data)]
    out = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                inner = info.filename
                base = os.path.basename(inner)
                if not base or base.startswith(".") or "__MACOSX" in inner:
                    continue
                ext = os.path.splitext(base)[1].lower()
                if ext in ARCHIVE_EXTS:
                    out.extend(expand_upload(base, zf.read(info), _depth + 1))
                elif ext in SUPPORTED_EXTS:
                    out.append((base, zf.read(info)))
                else:
                    log.info("Archive skip (unsupported) | %s", inner)
        log.info("Archive expanded | %s -> %d files", filename, len(out))
    except Exception:
        log.exception("Archive expand FAILED | %s", filename)
        raise
    return out


def extract_text(filename: str, data: bytes) -> str:
    """Main entry: pick a method by file extension and return the text."""
    ext = os.path.splitext(filename)[1].lower()
    if ext in UNSUPPORTED_EXTS:
        raise UnsupportedFile(f"{ext} files can't be turned into study text")
    log.info("Extract start | file=%s | ext=%s | %d bytes", filename, ext, len(data))
    try:
        if ext == ".pdf":
            text = _from_pdf(data)
        elif ext == ".docx":
            text = _from_docx(data)
        elif ext == ".pptx":
            text = _from_pptx(data)
        elif ext in (".xlsx", ".xlsm"):
            text = _from_xlsx(data)
        elif ext in (".csv", ".txt", ".md", ".tsv"):
            text = _from_plain(data)
        elif ext in RTF_EXTS:
            text = _from_rtf(data)
        elif ext in OPENDOC_EXTS:
            text = _from_opendoc(data)
        elif ext in LEGACY_OFFICE_EXTS:
            text = _from_legacy_office(data, ext)
        elif ext in IMAGE_EXTS:
            text = read_image_text(data, MIME_BY_EXT.get(ext, "image/png"))
        elif ext in (".gdoc", ".gsheet", ".gslides"):
            raise ValueError(
                "This is a Google shortcut file, not the document itself. "
                "In Google Docs/Sheets choose File > Download > Word/Excel/PDF, then upload that."
            )
        else:
            # Last resort: try to read it as plain text.
            text = _from_plain(data)
        text = (text or "").strip()
        log.info("Extract done  | file=%s | %d chars", filename, len(text))
        if not text:
            log.warning("Extract empty | file=%s produced no text", filename)
        return text
    except Exception:
        log.exception("Extract FAILED | file=%s", filename)
        raise


def _from_pdf(data: bytes) -> str:
    parts = []
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        for i, page in enumerate(doc):
            page_text = page.get_text().strip()
            if len(page_text) >= 15:
                parts.append(page_text)
            else:
                # Page has little/no selectable text -> likely scanned. OCR it (local first).
                log.info("PDF page %d looks scanned, running OCR", i + 1)
                pix = page.get_pixmap(dpi=200)
                img_bytes = pix.tobytes("png")
                parts.append(read_image_text(img_bytes, "image/png"))
    finally:
        doc.close()
    return "\n\n".join(p for p in parts if p)


def _from_docx(data: bytes) -> str:
    doc = DocxDocument(io.BytesIO(data))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _from_pptx(data: bytes) -> str:
    prs = Presentation(io.BytesIO(data))
    parts = []
    for idx, slide in enumerate(prs.slides, 1):
        parts.append(f"[Slide {idx}]")
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    line = "".join(r.text for r in para.runs).strip()
                    if line:
                        parts.append(line)
    return "\n".join(parts)


def _from_xlsx(data: bytes) -> str:
    wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    parts = []
    for ws in wb.worksheets:
        parts.append(f"[Sheet: {ws.title}]")
        for row in ws.iter_rows(values_only=True):
            cells = ["" if v is None else str(v) for v in row]
            if any(c.strip() for c in cells):
                parts.append(" | ".join(cells))
    wb.close()
    return "\n".join(parts)


def _from_rtf(data: bytes) -> str:
    """Rich Text Format (.rtf) -> plain text via striprtf (pure Python, no external tool)."""
    try:
        from striprtf.striprtf import rtf_to_text
    except Exception:
        raise ValueError(
            "Reading .rtf needs the 'striprtf' package. It should install automatically on "
            "first run; if not, run: pip install striprtf")
    raw = _from_plain(data)          # rtf is text-based; decode bytes first
    return rtf_to_text(raw)


def _from_opendoc(data: bytes) -> str:
    """OpenDocument text/spreadsheet/presentation (.odt/.ods/.odp) via odfpy.
    We pull every text paragraph, which covers body text, table cells and slide text."""
    try:
        from odf.opendocument import load as _odf_load
        from odf import text as _odf_text, teletype as _odf_teletype
    except Exception:
        raise ValueError(
            "Reading .odt/.ods/.odp needs the 'odfpy' package. It should install automatically "
            "on first run; if not, run: pip install odfpy")
    doc = _odf_load(io.BytesIO(data))
    parts = []
    for p in doc.getElementsByType(_odf_text.P):
        t = _odf_teletype.extractText(p).strip()
        if t:
            parts.append(t)
    return "\n".join(parts)


def _soffice_bin():
    """Find a LibreOffice/OpenOffice binary on PATH or in the standard Windows locations."""
    for name in ("soffice", "libreoffice"):
        p = shutil.which(name)
        if p:
            return p
    for c in (
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    ):
        if os.path.exists(c):
            return c
    return None


def _from_legacy_office(data: bytes, ext: str) -> str:
    """Old binary Office (.doc/.ppt/.xls). Python can't parse these directly, so if
    LibreOffice is installed we convert to PDF and reuse the PDF reader; otherwise we
    raise a clear message telling the user to re-save as the modern format."""
    exe = _soffice_bin()
    if not exe:
        raise ValueError(
            f"'{ext}' is an old Office format this app can't read directly. Open it in "
            "Word / PowerPoint / Excel (or Google Docs) and use Save As / Download to save it "
            "as .docx / .pptx / .xlsx, then upload that. (Or install LibreOffice — free — and "
            "the app will read old files automatically.)")
    import tempfile
    import subprocess
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "input" + ext)
        with open(src, "wb") as f:
            f.write(data)
        log.info("Legacy office %s -> converting to PDF via LibreOffice", ext)
        try:
            subprocess.run(
                [exe, "--headless", "--convert-to", "pdf", "--outdir", td, src],
                check=True, timeout=180,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            raise ValueError(
                f"LibreOffice failed to convert this {ext} file ({type(e).__name__}). "
                "Please re-save it as .docx / .pptx / .xlsx and upload that.")
        pdf_path = os.path.join(td, "input.pdf")
        if not os.path.exists(pdf_path):
            raise ValueError(
                f"Could not convert {ext} to a readable form. "
                "Please re-save it as .docx / .pptx / .xlsx and upload that.")
        with open(pdf_path, "rb") as f:
            return _from_pdf(f.read())


def _from_plain(data: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "cp949", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")
