"""Regenerates the sample uploaded documents (fictional companies; no real data).

    uv run python samples/documents/make_sample_documents.py

`tests/test_sample_library.py` checks that the committed PDFs match this script's output."""

from pathlib import Path

from insightforge_agent.pdf_writer import make_pdf

HERE = Path(__file__).resolve().parent

H, P = 16, 10  # heading and paragraph font sizes

NORTHWIND_2025 = [
    [(20, "Northwind Health Annual Report 2025"),
     (H, "Letter to shareholders"),
     (P, "Northwind Health revenue reached 612 million dollars in fiscal 2025."),
     (P, "Revenue grew 14 percent compared with fiscal 2024, led by the cardiology segment."),
     (P, "Operating margin improved to 11.5 percent in fiscal 2025."),
     (H, "Segment results"),
     (P, "Cardiology devices contributed 341 million dollars of revenue in fiscal 2025."),
     (P, "Orthopaedic implants contributed 187 million dollars of revenue in fiscal 2025."),
     (P, "Diagnostics contributed 84 million dollars of revenue in fiscal 2025.")],
    [(H, "Research and development"),
     (P, "Research and development spending was 71 million dollars in fiscal 2025."),
     (P, "The pipeline holds 6 programmes in clinical trials at the end of fiscal 2025."),
     (P, "The NW-200 heart valve completed its pivotal trial in September 2025."),
     (P, "Northwind expects regulatory approval for NW-200 in the first half of 2026."),
     (H, "Operations"),
     (P, "Northwind Health employed 2,140 people at the end of fiscal 2025."),
     (P, "A second manufacturing site in Cork, Ireland, began production in 2025.")],
    [(H, "Principal risks"),
     (P, "A single supplier provides the titanium used in orthopaedic implants."),
     (P, "Pricing pressure from hospital purchasing groups reduced average selling prices."),
     (P, "Delays to the NW-200 approval would defer expected cardiology revenue."),
     (H, "Outlook"),
     (P, "Northwind Health expects fiscal 2026 revenue growth of 9 to 11 percent.")],
]

DOCUMENTS = {"northwind-health-annual-report-2025.pdf":
             (NORTHWIND_2025, "Northwind Health Annual Report 2025 (fictional)")}


def render(name: str) -> bytes:
    pages, title = DOCUMENTS[name]
    return make_pdf(pages, title=title)


if __name__ == "__main__":
    for name in DOCUMENTS:
        (HERE / name).write_bytes(render(name))
        print(f"wrote {name}")
