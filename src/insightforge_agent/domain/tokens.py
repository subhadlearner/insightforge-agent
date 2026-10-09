def estimate_tokens(text: str) -> int:
    """A cheap, deterministic stand-in for the provider's tokenizer (about 4 characters a token).

    Used for the per-call Passage cap and the Evidence budget, so tests do not depend on one
    provider. It is an estimate; design.md section 12 still lists checking it as open."""
    return len(text) // 4 + 1
