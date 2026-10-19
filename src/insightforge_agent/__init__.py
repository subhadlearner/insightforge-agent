import sys


def main() -> None:
    """`insightforge-agent demo` runs the offline Extraction and synthesis demo;
    `insightforge-agent bench ...` runs the offline benchmark tools;
    `insightforge-agent eval ...` runs the sample briefs per provider and writes the evaluation
    report (real model and search calls);
    `insightforge-agent models download` fetches the local embedding and Docling models once;
    `insightforge-agent serve [--host H] [--port P]` serves the API (Settings from the
    environment and `.env`)."""
    if sys.argv[1:2] == ["serve"]:
        import argparse

        from insightforge_agent.api.app import Runtime, serve
        from insightforge_agent.config import Settings

        parser = argparse.ArgumentParser(prog="insightforge-agent serve")
        parser.add_argument("--host", default="127.0.0.1")
        parser.add_argument("--port", type=int, default=8000)
        opts = parser.parse_args(sys.argv[2:])
        serve(Runtime.build(Settings()), opts.host, opts.port)
        return
    if sys.argv[1:] == ["models", "download"]:
        from insightforge_agent.prefetch import download

        raise SystemExit(download())
    if sys.argv[1:] == ["demo"]:
        from insightforge_agent.demo import main as demo

        raise SystemExit(demo())
    if sys.argv[1:2] == ["bench"]:
        from insightforge_agent.benchmark.cli import main as bench

        raise SystemExit(bench(sys.argv[2:]))
    if sys.argv[1:2] == ["eval-judge"]:
        from insightforge_agent.evaluation.judge_only import main as judge_only

        raise SystemExit(judge_only(sys.argv[2:]))
    if sys.argv[1:2] == ["eval"]:
        from insightforge_agent.evaluation.cli import main as evaluate

        raise SystemExit(evaluate(sys.argv[2:]))
    print("Hello from insightforge-agent!")
