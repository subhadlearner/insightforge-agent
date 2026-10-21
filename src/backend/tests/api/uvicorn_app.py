"""The real app with scripted models, for serving under a real uvicorn process in tests. Its
files live in $T10_SMOKE_DIR. Offline: scripted models and fixture search.

- `python -m tests.api.uvicorn_app --port P`: through `insightforge_agent.api.app.serve`, as
  `insightforge-agent serve` runs it.
- `uvicorn tests.api.uvicorn_app:app --factory`: plain uvicorn over the same app."""

import argparse
import logging
import os
from pathlib import Path

from fastapi import FastAPI

from insightforge_agent.api.app import Runtime, create_app, serve
from tests.api.support import Gate, scripted_deps, settings
from tests.pipeline import fakes


def runtime() -> Runtime:
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    here = Path(os.environ["T10_SMOKE_DIR"])
    return Runtime.build(settings(here), scripted_deps(Gate(fakes.FixtureSearch())))


def app() -> FastAPI:
    return create_app(runtime())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    serve(runtime(), "127.0.0.1", parser.parse_args().port)
