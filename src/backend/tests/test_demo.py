"""The offline demo is an acceptance check of the real pipeline; keep it working."""

import socket

import pytest

from insightforge_agent import main as cli_main
from insightforge_agent.demo import checks, main, render, run_demo


def test_every_demo_check_passes_on_the_real_pipeline():
    run = run_demo()
    failed = [text for text, ok in checks(run) if not ok]
    assert failed == []


def test_the_demo_report_shows_evidence_confidence_rules_contradictions_and_rejections():
    text = render(run_demo())
    for expected in ("independent_sources", "high_credibility_single_source",
                     "insufficient_credibility", "conflicting", "supported by",
                     "contradicted by", "value not in Passage", "no supported Passage",
                     "[PASS]"):
        assert expected in text
    assert "[FAIL]" not in text


def test_the_demo_makes_no_network_calls(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the demo must not open a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    assert main() == 0


def test_the_cli_runs_the_demo(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["insightforge-agent", "demo"])
    with pytest.raises(SystemExit) as stop:
        cli_main()
    assert stop.value.code == 0
    assert "OFFLINE DEMO" in capsys.readouterr().out


def test_the_cli_without_arguments_is_unchanged(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["insightforge-agent"])
    cli_main()
    assert "Hello" in capsys.readouterr().out
