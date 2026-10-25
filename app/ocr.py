# -*- coding: utf-8 -*-
"""Local OCR via Tesseract. Free, offline, unlimited — used FIRST for images and
scanned PDF pages. The cloud vision model is only a fallback for the few files
Tesseract can't read well (messy handwriting, very blurry photos).

On Windows, Tesseract is a separate install. If it isn't on PATH, we auto-detect
the standard install locations so the app works without the user editing PATH.
"""
import io
import os
import shutil

from PIL import Image, ImageOps, ImageFilter

from .logger import get_logger

log = get_logger()

_configured = False
_available = None

# Standard Tesseract locations to try when it isn't on PATH (Windows mainly).
_WIN_CANDIDATES = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expanduser(r"~\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"),
    os.path.expanduser(r"~\AppData\Local\Tesseract-OCR\tesseract.exe"),
]


def _configure():
    global _configured
    if _configured:
        return
    try:
        import pytesseract  # noqa
    except Exception:
        log.warning("pytesseract not installed; local OCR disabled")
        _configured = True
        return
    if shutil.which("tesseract") is None:
        for c in _WIN_CANDIDATES:
            if os.path.exists(c):
                import pytesseract
                pytesseract.pytesseract.tesseract_cmd = c
                log.info("Tesseract auto-detected at %s", c)
                break
        else:
            log.warning("Tesseract binary not found on PATH or standard locations")
    _configured = True


def available() -> bool:
    """True if local Tesseract OCR can be used."""
    global _available
    if _available is not None:
        return _available
    _configure()
    try:
        import pytesseract
        v = pytesseract.get_tesseract_version()
        log.info("Local OCR ready | Tesseract %s", v)
        _available = True
    except Exception:
        log.warning("Local OCR unavailable (Tesseract not callable)")
        _available = False
    return _available


def _prep(img: Image.Image) -> Image.Image:
    """Light preprocessing to help Tesseract: grayscale, upscale small images, autocontrast."""
    img = img.convert("L")
    w, h = img.size
    if max(w, h) < 1600:
        scale = 1600 / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    img = ImageOps.autocontrast(img)
    return img


def ocr_image(image_bytes: bytes, lang: str = "eng"):
    """OCR an image. Returns (text, mean_confidence[0-100], word_count).
    On any failure returns ('', 0, 0)."""
    if not available():
        return "", 0.0, 0
    import pytesseract
    from pytesseract import Output
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = _prep(img)
        data = pytesseract.image_to_data(img, lang=lang, output_type=Output.DICT)
        words, confs = [], []
        for word, conf in zip(data.get("text", []), data.get("conf", [])):
            if word and word.strip():
                try:
                    c = float(conf)
                except (TypeError, ValueError):
                    c = -1
                if c >= 0:
                    words.append(word.strip())
                    confs.append(c)
        text = " ".join(words) if words else pytesseract.image_to_string(img, lang=lang).strip()
        mean_conf = (sum(confs) / len(confs)) if confs else 0.0
        return text.strip(), mean_conf, len(words)
    except Exception:
        log.exception("Local OCR error")
        return "", 0.0, 0
