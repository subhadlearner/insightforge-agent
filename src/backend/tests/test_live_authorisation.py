"""`-m live` alone must not start real provider calls (tests/live/conftest.py). Nothing here runs
a live test: the guard is exercised on stand-in items."""

from pathlib import Path

import pytest

from tests.live import conftest as live

LIVE_DIR = Path(live.__file__).parent


class Item:
    def __init__(self, path):
        self.path = path
        self.marks = []

    def add_marker(self, mark):
        self.marks.append(mark)


def skip_reasons(item):
    return [m.kwargs.get("reason") for m in item.marks if m.name == "skip"]


@pytest.mark.parametrize("environ", [{}, {live.ALLOW_ENV: ""}, {live.ALLOW_ENV: "0"},
                                     {live.ALLOW_ENV: "true"}])
def test_without_the_explicit_opt_in_live_is_not_allowed(environ):
    assert live.live_allowed(environ) is False


def test_only_the_exact_opt_in_allows_live():
    assert live.live_allowed({live.ALLOW_ENV: "1"}) is True


def test_live_tests_are_skipped_before_any_fixture_without_the_opt_in(monkeypatch):
    monkeypatch.delenv(live.ALLOW_ENV, raising=False)
    live_item, other = Item(LIVE_DIR / "test_planner_thread.py"), Item(LIVE_DIR.parent / "test_x.py")
    live.pytest_collection_modifyitems([live_item, other])
    assert skip_reasons(live_item) and live.ALLOW_ENV in skip_reasons(live_item)[0]
    assert skip_reasons(other) == []


def test_with_the_opt_in_live_tests_are_left_to_run(monkeypatch):
    monkeypatch.setenv(live.ALLOW_ENV, "1")
    item = Item(LIVE_DIR / "test_planner_thread.py")
    live.pytest_collection_modifyitems([item])
    assert skip_reasons(item) == []
