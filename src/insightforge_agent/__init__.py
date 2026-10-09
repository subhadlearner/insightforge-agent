import sys


def main() -> None:
    """`insightforge-agent demo` runs the offline Extraction and synthesis demo."""
    if sys.argv[1:] == ["demo"]:
        from insightforge_agent.demo import main as demo

        raise SystemExit(demo())
    print("Hello from insightforge-agent!")
