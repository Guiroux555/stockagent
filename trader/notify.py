"""Talking to systemd, in standard library only.

An agent that spends 99% of its life asleep is indistinguishable, from the
outside, from an agent whose socket wedged three days ago. Both are a python
process using no CPU. `Restart=always` cannot help with the second one, because
nothing ever exits.

systemd already solves this: a service can be asked to check in, and killed and
restarted if it stops. That only needs a datagram on a unix socket whose path
systemd puts in the environment, which is why this file has no dependency and
does nothing at all when run outside systemd — the same `run` command works on
a laptop, where every function here is a no-op.

Protocol reference: `man sd_notify`.
"""

from __future__ import annotations

import os
import socket

__all__ = ["available", "ready", "status", "watchdog", "watchdog_interval"]


def _send(message: str) -> bool:
    """Best-effort datagram to the notify socket; False if there is nobody
    listening.

    Never raises. A supervision channel that can take the agent down with it is
    worse than no supervision channel.
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):  # abstract namespace
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as s:
            s.connect(addr)
            s.sendall(message.encode("utf-8"))
        return True
    except OSError:
        return False


def available() -> bool:
    """Whether this process was started by systemd with notifications on."""
    return bool(os.environ.get("NOTIFY_SOCKET"))


def ready(text: str | None = None) -> bool:
    """Declare the service up.

    Sent *before* the first tick, not after: a cold start syncs the whole
    universe and can take minutes, and a `Type=notify` unit that stays silent
    that long is killed on `TimeoutStartSec` before it ever trades.
    """
    message = "READY=1"
    if text:
        message += f"\nSTATUS={text}"
    return _send(message)


def status(text: str) -> bool:
    """One line of state, shown by `systemctl status`.

    This is the difference between "active (running)" and "active (running),
    next wake-up 16:01 UTC, 3 open, offline since 14:20" on a board with no
    screen.
    """
    return _send(f"STATUS={text}")


def watchdog() -> bool:
    """Check in. If these stop arriving for `WatchdogSec`, systemd restarts us."""
    return _send("WATCHDOG=1")


def watchdog_interval() -> float | None:
    """How often to check in — half the configured timeout, per `sd_notify`.

    `None` means no watchdog is configured, or one was configured for a
    different process (systemd sets `WATCHDOG_PID` when it matters).
    """
    usec = os.environ.get("WATCHDOG_USEC")
    if not usec:
        return None
    pid = os.environ.get("WATCHDOG_PID")
    if pid and pid.isdigit() and int(pid) != os.getpid():
        return None
    try:
        seconds = int(usec) / 1_000_000
    except ValueError:
        return None
    return seconds / 2 if seconds > 0 else None
