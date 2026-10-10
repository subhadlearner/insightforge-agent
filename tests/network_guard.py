"""Fail any test that opens an outbound network connection (tiers 1-3 are offline).

A connection to a non-loopback address, or a DNS lookup of a non-loopback name, raises
`NetworkBlocked` and is also remembered, so a caller that swallows the error (several production
paths catch `Exception`) still fails the test at teardown. Loopback stays open: asyncio on
Windows builds its self-pipe with a loopback socket pair."""

import ipaddress
import socket

LOOPBACK_NAMES = frozenset({"localhost", "ip6-localhost"})


class NetworkBlocked(RuntimeError):
    pass


def is_loopback(host: object) -> bool:
    if host in (None, ""):
        return True  # an unspecified bind address is not an outbound connection
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    name = str(host).strip("[]").lower()
    if name in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(name.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def install(monkeypatch, attempts: list[str]) -> None:
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def check(target: object, what: str) -> None:
        host = target[0] if isinstance(target, tuple) and target else target
        if isinstance(target, str):  # an AF_UNIX path: local by construction
            return
        if not is_loopback(host):
            attempts.append(f"{what} {host!r}")
            raise NetworkBlocked(f"blocked outbound network use: {what} {host!r}")

    def connect(self, address):
        check(address, "connect to")
        return real_connect(self, address)

    def connect_ex(self, address):
        check(address, "connect to")
        return real_connect_ex(self, address)

    def getaddrinfo(host, *args, **kwargs):
        check((host,), "DNS lookup of")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
