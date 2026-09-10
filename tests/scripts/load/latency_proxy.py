"""A TCP proxy that adds latency in front of Postgres, so the harness can run
at a production-like round trip (`02` step 5: "one additional run at
production-like RTT … so F1/F5 are not decided on Docker fsync"). Every
chunk in each direction waits `delay_s` before it is forwarded, so the
database's RTT grows by about twice that. `LOAD_HARNESS_DB_DELAY_MS` turns it
on; the report names the number."""

from __future__ import annotations

import asyncio
import socket
import threading
from typing import Optional


class LatencyProxy:
    def __init__(self, upstream_host: str, upstream_port: int, *, delay_s: float):
        self.upstream = (upstream_host, upstream_port)
        self.delay_s = delay_s
        self.port = _free_port()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._server: Optional[asyncio.AbstractServer] = None
        self._ready = threading.Event()

    async def _pipe(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                await asyncio.sleep(self.delay_s)
                writer.write(chunk)
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001 — teardown
                pass

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            up_reader, up_writer = await asyncio.open_connection(*self.upstream)
        except OSError:
            writer.close()
            return
        await asyncio.gather(
            self._pipe(reader, up_writer), self._pipe(up_reader, writer)
        )

    async def _serve(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
        self._ready.set()
        async with self._server:
            await self._server.serve_forever()

    def start(self) -> "LatencyProxy":
        def run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(self._serve())
            except asyncio.CancelledError:
                pass
            finally:
                self._loop.close()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            raise RuntimeError("latency proxy did not start")
        return self

    def stop(self) -> None:
        if self._loop is not None and self._server is not None:
            self._loop.call_soon_threadsafe(self._server.close)
        if self._thread is not None:
            self._thread.join(timeout=2)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def through_proxy(dsn: str, proxy: LatencyProxy) -> str:
    """The same DSN, pointed at the proxy."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(dsn)
    auth = f"{parts.username}:{parts.password}@" if parts.username else ""
    netloc = f"{auth}127.0.0.1:{proxy.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
