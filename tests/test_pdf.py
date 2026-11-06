"""PDF attachments: text extracted page by page, read and searched like text files."""

from nova.files import Workspace, kind_of


def make_pdf(pages: list[str]) -> bytes:
    """A minimal valid PDF with one line of Helvetica text per page."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", ""]
    kids = []
    font = 3 + 2 * len(pages)
    for i, text in enumerate(pages):
        page, content = 3 + 2 * i, 4 + 2 * i
        kids.append(f"{page} 0 R")
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                       f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {content} 0 R >>")
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET"
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>"
    objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out, offsets = b"%PDF-1.4\n", []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def test_pdf_is_its_own_kind():
    assert kind_of("doctrine.pdf") == "pdf"
    assert kind_of("download", "application/pdf") == "pdf"


def test_small_pdf_goes_into_the_prompt_with_page_markers(tmp_path):
    ws = Workspace(tmp_path)
    saved = ws.save("doctrine.pdf", make_pdf(["Ferox fleet doctrine", "Bring 2 warp disruptors"]))
    text = ws.describe(saved)
    assert "--- page 1 ---" in text and "Ferox fleet doctrine" in text
    assert "--- page 2 ---" in text and "Bring 2 warp disruptors" in text


def test_pdf_can_be_searched_and_read(tmp_path):
    ws = Workspace(tmp_path)
    ws.save("rules.pdf", make_pdf([f"Rule number {n}" for n in range(1, 4)]))
    assert "Rule number 3" in ws.search("rules", "number 3")
    assert "--- page 2 ---" in ws.read("rules.pdf", 1, 10)


def test_pdf_without_text_says_so(tmp_path):
    ws = Workspace(tmp_path)
    saved = ws.save("scan.pdf", make_pdf(["", ""]))
    assert "no text layer" in ws.describe(saved)


def test_broken_pdf_is_reported_not_raised(tmp_path):
    ws = Workspace(tmp_path)
    saved = ws.save("broken.pdf", b"%PDF-1.4 this is not really a pdf")
    assert "broken.pdf" in ws.describe(saved)
