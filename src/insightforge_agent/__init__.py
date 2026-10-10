import sys


def main() -> None:
    """`insightforge-agent demo` runs the offline Extraction and synthesis demo;
    `insightforge-agent bench ...` runs the offline benchmark tools."""
    if sys.argv[1:] == ["demo"]:
        from insightforge_agent.demo import main as demo

        raise SystemExit(demo())
    if sys.argv[1:2] == ["bench"]:
        from insightforge_agent.benchmark.cli import main as bench

        raise SystemExit(bench(sys.argv[2:]))
    print("Hello from insightforge-agent!")
