# Entity extraction is its own stage between research and synthesis

The spec lists the Entity Extractor in the parallel Researcher pool, yet it must read all gathered content to flag cross-source conflicts. That conflicts with subagent isolation. We run Extraction as a separate stage after research and before synthesis, so researchers stay isolated and gather only. This deliberately adds a state (`EXTRACTING`) that the spec's state machine doesn't list.
