"""A real HTTP server for tests, on a free port of this computer."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import uvicorn
from starlette.types import ASGIApp


@contextmanager
def running(app: ASGIApp | None = None, *, build: Callable[[str], ASGIApp] | None = None) -> Iterator[str]:
    """Serve `app`, or `build(mcp_url)` when the app needs to know its own address."""
    # The socket is bound here and handed to uvicorn, so no other server can take the port
    # between choosing it and listening on it.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}/mcp"
    app = build(url) if build else app
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("server did not start")
        time.sleep(0.05)
    try:
        yield url
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive(), "the test server did not stop"
