import sys


def main() -> None:
    """`insightforge-agent demo` runs the offline Extraction and synthesis demo;
    `insightforge-agent bench ...` runs the offline benchmark tools;
    `insightforge-agent models download` fetches the local embedding and Docling models once;
    `insightforge-agent serve [--host H] [--port P]` serves the API (Settings from the
    environment and `.env`)."""
    if sys.argv[1:2] == ["serve"]:
        import argparse

        from insightforge_agent.api.app import Runtime, serve
        from insightforge_agent.config import Settings

        args = argparse.ArgumentParser(prog="insightforge-agent serve")
        args.add_argument("--host", default="127.0.0.1")
        args.add_argument("--port", type=int, default=8000)
        opts = args.parse_args(sys.argv[2:])
        raise SystemExit(serve(Runtime.build(Settings()), opts.host, opts.port))
    if sys.argv[1:] == ["models", "download"]:
        from insightforge_agent.prefetch import download

        raise SystemExit(download())
    if sys.argv[1:] == ["demo"]:
        from insightforge_agent.demo import main as demo

        raise SystemExit(demo())
    if sys.argv[1:2] == ["bench"]:
        from insightforge_agent.benchmark.cli import main as bench

        raise SystemExit(bench(sys.argv[2:]))
    print("Hello from insightforge-agent!")
