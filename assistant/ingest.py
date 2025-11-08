# assistant/ingest.py
import os
import re
import unicodedata
import pathlib
from typing import List
from pdfminer.high_level import extract_pages
from pdfminer.layout import LTTextContainer, LTTextLineHorizontal

"""
Ingest per page:
- Extract text per PDF page with pdfminer (no poppler needed)
- Optional: render PNGs (requires Poppler) and OCR empty pages (requires Tesseract)
- Normalize/clean text
- Write one TXT per page => media/knowledge_txt/<source>_pNNN.txt
- Optionally write PNG per page => static/page_images/<source>_pNNN.png
"""

# ===== Config =====
GENERATE_IMAGES = False       # True only if poppler (pdftoppm/pdfinfo) is installed
OCR_EMPTY_PAGES = False       # True only if Tesseract is installed
DPI = 200                     # PNG resolution for image/OCR path
TEXT_MIN_LEN_FOR_SKIP_OCR = 60  # if extracted text shorter than this, try OCR (when enabled)
# ==================

BASE = pathlib.Path(__file__).resolve().parents[1]
PDF_DIR = BASE / "media" / "knowledge"
TXT_DIR = BASE / "media" / "knowledge_txt"
IMG_DIR = BASE / "static" / "page_images"

TXT_DIR.mkdir(parents=True, exist_ok=True)
IMG_DIR.mkdir(parents=True, exist_ok=True)


# ----------------------- helpers -----------------------
def slugify(s: str) -> str:
    """Permissive slug: keep letters/digits/._- and replace others with _."""
    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", s)
    return s.strip("_")


def _norm_text(s: str) -> str:
    """Normalize diacritics, whitespace, and tidy repeated blanks."""
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("\u00A0", " ")  # NBSP -> space
    # de-duplicate hyphenation at EOL (common in catalogs): e.g. "che-\nie"
    s = re.sub(r"(\w)-\n(\w)", r"\1\2", s)
    # collapse excessive spaces and clean blank lines
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\r\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _remove_page_noise(s: str) -> str:
    """
    Remove common headers/footers (very conservative).
    Add project-specific rules here if needed.
    """
    lines = [ln.strip() for ln in s.splitlines()]
    # Remove page-only lines like "Page 7", "Pag. 7", or numeric-only header/footer
    cleaned = []
    for ln in lines:
        if re.fullmatch(r"(pag(ina)?\.?\s*)?\d{1,4}", ln.lower()):
            continue
        if re.fullmatch(r"\d{1,4}", ln):
            continue
        cleaned.append(ln)
    return "\n".join(cleaned).strip()


def extract_per_page_text(pdf_path: pathlib.Path) -> List[str]:
    """Extract text per page using pdfminer (no Poppler)."""
    pages = []
    for page_layout in extract_pages(str(pdf_path)):
        buf = []
        for element in page_layout:
            if isinstance(element, LTTextContainer):
                for text_line in element:
                    if isinstance(text_line, LTTextLineHorizontal):
                        buf.append(text_line.get_text())
        pages.append("".join(buf))
    return pages


def render_page_images(pdf_path: pathlib.Path):
    """
    Render PNGs per page **only** if GENERATE_IMAGES=True and Poppler is available.
    Returns list of PIL.Image (or []).
    """
    if not GENERATE_IMAGES:
        return []

    try:
        from pdf2image import convert_from_path  # type: ignore
    except Exception as e:
        print("Image generation skipped: cannot import pdf2image:", e)
        return []

    try:
        images = convert_from_path(str(pdf_path), fmt="png", dpi=DPI)
        return images
    except Exception as e:
        print("Image generation skipped: pdf2image/Poppler error:", e)
        return []


def ocr_image_to_text(pil_img) -> str:
    """OCR a PIL image if Tesseract is installed; else return ''."""
    if not OCR_EMPTY_PAGES:
        return ""
    try:
        import pytesseract  # type: ignore
        txt = pytesseract.image_to_string(pil_img, lang="ron+eng")
        return txt or ""
    except Exception as e:
        print("OCR skipped (tesseract not available?):", e)
        return ""


# ----------------------- pipeline -----------------------
def main():
    pdfs = sorted(PDF_DIR.glob("*.pdf"))
    if not pdfs:
        print("Put PDF files into media/knowledge/ first.")
        return

    for pdf in pdfs:
        base = slugify(pdf.stem)
        print(f"Processing {pdf.name}")

        # 1) extract text per page
        try:
            raw_pages = extract_per_page_text(pdf)
        except Exception as e:
            print("pdfminer error:", e)
            raw_pages = []

        # 2) optional: render images (useful for thumbnails and OCR fallback)
        images = render_page_images(pdf)

        num_pages = max(len(raw_pages), len(images))
        if num_pages == 0:
            print(" -> no pages extracted (check PDF integrity).")
            continue

        for idx in range(num_pages):
            pno = idx + 1
            tag = f"p{pno:03d}"

            # text for page
            txt = raw_pages[idx] if idx < len(raw_pages) else ""
            txt = _norm_text(txt)
            txt = _remove_page_noise(txt)

            # OCR fallback if very short and we have image
            if OCR_EMPTY_PAGES and len(txt) < TEXT_MIN_LEN_FOR_SKIP_OCR and idx < len(images):
                ocr_txt = _norm_text(ocr_image_to_text(images[idx]))
                if len(ocr_txt) > len(txt):
                    txt = ocr_txt

            header = f"[SOURCE:{base}] [PAGE:{pno}]\n"
            out_txt = (TXT_DIR / f"{base}_{tag}.txt")
            out_txt.write_text(header + txt, encoding="utf-8")

            # optional page PNG
            if idx < len(images):
                img_path = IMG_DIR / f"{base}_{tag}.png"
                try:
                    images[idx].save(img_path)
                except Exception as e:
                    print(f"Could not save image for page {pno}:", e)

        print(f" -> wrote {num_pages} pages")

    print("Done. Run: python manage.py runserver")


if __name__ == "__main__":
    main()
