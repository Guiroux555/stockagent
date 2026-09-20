"""The systemd notification channel, checked against a real socket.

Mocking the socket would test the mock. These bind an actual `AF_UNIX`
datagram socket, which is exactly what systemd does, and read what arrives.
"""

from __future__ import annotations

import socket

import pytest

from trader import notify


@pytest.fixture
def listener(tmp_path, monkeypatch):
    path = str(tmp_path / "notify.sock")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(path)
    sock.settimeout(2)
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    yield sock
    sock.close()


def test_nothing_is_sent_outside_systemd(monkeypatch):
    """The same `run` command has to work on a laptop, where there is nobody
    to notify."""
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert notify.available() is False
    assert notify.ready() is False
    assert notify.watchdog() is False
    assert notify.status("anything") is False


def test_ready_carries_a_status(listener):
    assert notify.ready("starting") is True
    assert listener.recv(4096) == b"READY=1\nSTATUS=starting"


def test_the_watchdog_ping_is_the_documented_datagram(listener):
    assert notify.watchdog() is True
    assert listener.recv(4096) == b"WATCHDOG=1"


def test_a_dead_socket_is_not_fatal(tmp_path, monkeypatch):
    """A supervision channel that can take the agent down with it is worse
    than no supervision channel."""
    monkeypatch.setenv("NOTIFY_SOCKET", str(tmp_path / "nobody-is-listening.sock"))
    assert notify.status("hello") is False


def test_the_ping_interval_is_half_the_timeout(monkeypatch):
    monkeypatch.setenv("WATCHDOG_USEC", "600000000")
    monkeypatch.delenv("WATCHDOG_PID", raising=False)
    assert notify.watchdog_interval() == 300.0


def test_a_watchdog_meant_for_another_process_is_ignored(monkeypatch):
    """systemd sets WATCHDOG_PID so an inherited environment does not make a
    child think it is the one being watched."""
    monkeypatch.setenv("WATCHDOG_USEC", "600000000")
    monkeypatch.setenv("WATCHDOG_PID", "1")
    assert notify.watchdog_interval() is None


def test_no_watchdog_configured(monkeypatch):
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    assert notify.watchdog_interval() is None
