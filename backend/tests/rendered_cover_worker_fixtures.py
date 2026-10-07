from __future__ import annotations

import socket
import time
from typing import Any


def stalled_listener_worker(
    sender: Any,
    _browser_channel: str,
    _timeout_seconds: float,
    _target: str,
    _expected_identity: str,
) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    sender.send((None, f"test-listener:{port}"))
    sender.close()
    time.sleep(30)


def containment_unavailable_worker(
    sender: Any,
    _browser_channel: str,
    _timeout_seconds: float,
    _target: str,
    _expected_identity: str,
) -> None:
    sender.send((None, "containment-unavailable"))
    sender.close()
