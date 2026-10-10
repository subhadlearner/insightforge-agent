"""The CI tiers: offline tiers fail on network use, the live tier is marked and never ordinary CI."""

import contextlib
import socket

import pytest

from tests import network_guard
from tests.conftest import network_guard_applies

TIERS = ("baseline", "replay", "scripted", "live")


# ---- the guard -------------------------------------------------------------------------------

@contextlib.contextmanager
def own_guard():
    """A guard of this test's own, so a block provoked on purpose is not also charged to the
    autouse guard (which fails the test at teardown for any attempt it saw)."""
    attempts: list[str] = []
    patch = pytest.MonkeyPatch()
    network_guard.install(patch, attempts)
    try:
        yield attempts
    finally:
        patch.undo()


def test_an_outbound_connection_is_blocked_in_every_offline_test():
    with own_guard(), pytest.raises(network_guard.NetworkBlocked, match="93.184.216.34"):
        socket.create_connection(("93.184.216.34", 80), timeout=1)


def test_a_dns_lookup_of_a_public_name_is_blocked():
    with own_guard(), pytest.raises(network_guard.NetworkBlocked, match="example.com"):
        socket.getaddrinfo("example.com", 80)


def test_loopback_stays_open(monkeypatch):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        client = socket.create_connection(server.getsockname(), timeout=2)
        client.close()
    finally:
        server.close()


@pytest.mark.parametrize("host, loopback", [
    ("127.0.0.1", True), ("127.8.8.8", True), ("::1", True), ("[::1]", True),
    ("localhost", True), ("LOCALHOST", True), ("", True), (None, True),
    ("10.0.0.5", False), ("8.8.8.8", False), ("example.com", False), ("2001:db8::1", False),
])
def test_what_counts_as_loopback(host, loopback):
    assert network_guard.is_loopback(host) is loopback


def test_a_swallowed_block_is_still_remembered():
    with own_guard() as attempts:
        try:
            socket.create_connection(("93.184.216.34", 80), timeout=1)
        except Exception:  # noqa: BLE001 - what production code that swallows errors does
            pass
    assert attempts and "93.184.216.34" in attempts[0]


def test_only_a_live_test_is_exempt_from_the_guard():
    class Node:
        def __init__(self, markers):
            self._markers = markers

        def get_closest_marker(self, name):
            return name if name in self._markers else None

    assert network_guard_applies(Node({"baseline"})) and network_guard_applies(Node(set()))
    assert not network_guard_applies(Node({"live"}))


# ---- the tiers -------------------------------------------------------------------------------

def test_a_test_belongs_to_at_most_one_tier(request):
    for item in request.session.items:
        named = [t for t in TIERS if item.get_closest_marker(t)]
        assert len(named) <= 1, f"{item.nodeid} is in tiers {named}"


def test_live_tests_are_excluded_from_ordinary_runs(request):
    assert "not live" in request.config.getini("addopts")
    if "live" in (request.config.option.markexpr or "").replace("not live", ""):
        pytest.skip("this run selected live tests on purpose")
    live = [i.nodeid for i in request.session.items if i.get_closest_marker("live")]
    assert live == []


def test_everything_under_tests_live_is_marked_live(request):
    unmarked = [i.nodeid for i in request.session.items
                if "tests/live/" in i.nodeid.replace("\\", "/") and not i.get_closest_marker("live")]
    assert unmarked == []


def test_all_four_tier_markers_are_registered(request):
    registered = {line.split(":", 1)[0] for line in request.config.getini("markers")}
    assert set(TIERS) <= registered
