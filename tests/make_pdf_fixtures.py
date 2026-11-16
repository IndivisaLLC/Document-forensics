"""
Regenerate the PDF test fixtures in tests/fixtures/.

These fixtures exercise every branch of docforensics.parse_pdf:
  text_simple.pdf     - single page with a text layer (pattern-rich)  -> OK
  text_multipage.pdf  - three pages with a text layer                 -> OK
  image_only.pdf      - rasterized page, no text layer (scanned)      -> NO_TEXT
  blank.pdf           - one empty page                                -> NO_TEXT
  encrypted.pdf       - password-protected                            -> FAILED
  malformed.pdf       - PDF header then garbage                       -> FAILED

Dev-only dependencies (NOT runtime deps of docforensics):
    pip install reportlab pillow img2pdf pypdf

Usage:
    python tests/make_pdf_fixtures.py [output_dir]      # default: tests/fixtures
"""

import os
import sys


def build(out_dir: str) -> None:
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import letter
    from PIL import Image, ImageDraw
    import img2pdf
    from pypdf import PdfReader, PdfWriter

    os.makedirs(out_dir, exist_ok=True)
    def p(n): return os.path.join(out_dir, n)

    # 1. single-page text, pattern-rich
    c = canvas.Canvas(p("text_simple.pdf"), pagesize=letter)
    for i, line in enumerate([
        "Acme Capital Partners LLC - Due Diligence Summary",
        "Contact: compliance@acme.com   Ref: https://sec.gov/x",
        "The $2.4 billion warehouse facility default on 03/03/2024",
        "Obligations under 15 U.S.C. 78m and 17 CFR 240.10b-5.",
    ]):
        c.drawString(72, 720 - i * 24, line)
    c.showPage(); c.save()

    # 2. multi-page text (3 pages)
    c = canvas.Canvas(p("text_multipage.pdf"), pagesize=letter)
    for pg in range(3):
        c.drawString(72, 720, f"Page {pg + 1}: facility default and impairment deferral noted.")
        c.showPage()
    c.save()

    # 3. image-only (scanned): text rasterized into a PNG, no text layer
    img = Image.new("RGB", (1200, 400), "white")
    ImageDraw.Draw(img).text((30, 30), "SCANNED PAGE - facility default - no text layer", fill="black")
    png = p("_scan.png"); img.save(png)
    with open(p("image_only.pdf"), "wb") as f:
        f.write(img2pdf.convert(png))
    os.remove(png)

    # 4. blank page, no text
    c = canvas.Canvas(p("blank.pdf"), pagesize=letter); c.showPage(); c.save()

    # 5. encrypted (user password), derived from the simple text pdf
    w = PdfWriter(); w.append(PdfReader(p("text_simple.pdf"))); w.encrypt("secret")
    with open(p("encrypted.pdf"), "wb") as f:
        w.write(f)

    # 6. malformed: PDF header then garbage, truncated
    with open(p("malformed.pdf"), "wb") as f:
        f.write(b"%PDF-1.4\n9 0 obj<< /broken garbage not a pdf \n%%EOF")

    for n in sorted(os.listdir(out_dir)):
        if n.endswith(".pdf"):
            print(f"{os.path.getsize(os.path.join(out_dir, n)):>8}  {n}")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "fixtures")
    build(out)
