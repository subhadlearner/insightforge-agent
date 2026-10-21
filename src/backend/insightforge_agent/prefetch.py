"""`insightforge-agent models download`: fetch the local model files once (no API key, no cost).

The embedding model (fastembed) and Docling's layout model are downloaded on first use. This
command does it up front, so later runs and the integration tests work offline."""

import os

from insightforge_agent.config import Settings
from insightforge_agent.pdf_writer import make_pdf


def download() -> int:
    from insightforge_agent.agents.doc_reader import DoclingChunker
    from insightforge_agent.embeddings import FastEmbedEmbedder

    model = os.environ.get("EMBEDDING_MODEL", Settings.model_fields["embedding_model"].default)
    print(f"embedding model {model}: ", end="", flush=True)
    print(f"{len(FastEmbedEmbedder(model).embed(['warm up'])[0])} dimensions")
    print("Docling layout model: ", end="", flush=True)
    page = make_pdf([[(16, "Warm-up"), (10, "Docling reads this page once to fetch its models.")]])
    print(f"{len(DoclingChunker().chunk('warm-up.pdf', page))} chunk(s) from a test page")
    return 0
