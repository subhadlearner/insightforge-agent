"""A minimal, dependency-free PDF writer: the sample documents, test fixtures and the model
prefetch (`insightforge-agent models download`) use it.

Each page is a list of `(font_size, text)` lines drawn top to bottom in Helvetica. Larger lines
read as headings. Only printable ASCII is supported, which is all the fixtures need."""

Page = list[tuple[int, str]]


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: list[Page], title: str = "Fixture") -> bytes:
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")  # filled in once the page tree exists
    tree = add(b"")
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    info = add(f"<< /Title ({_escape(title)}) >>".encode())
    kids = []
    for lines in pages:
        y, ops = 760, ["BT"]
        for size, text in lines:
            y -= int(size * 1.6)
            ops.append(f"/F1 {size} Tf 1 0 0 1 72 {y} Tm ({_escape(text)}) Tj")
        ops.append("ET")
        stream = "\n".join(ops).encode()
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        kids.append(add(
            f"<< /Type /Page /Parent {tree} 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {content} 0 R >>".encode()))
    objects[catalog - 1] = f"<< /Type /Catalog /Pages {tree} 0 R >>".encode()
    objects[tree - 1] = (f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] "
                         f"/Count {len(kids)} >>").encode()

    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for n, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R /Info {info} 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)
