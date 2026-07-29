#!/usr/bin/env python3
"""A bounded TCP broker for LEGOSim named-pipe payloads.

The upstream PipeComm API is directional and blocking.  This service preserves
those properties for remote containers: a writer deposits one payload under a
pipe name and a reader blocks until that payload is available.  The protocol is
newline-delimited JSON with base64 payloads so it is easy to inspect and test.
It is intentionally a correctness-first transport; performance experiments
must use the later native C++ adapter rather than this Python reference path.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
from collections import defaultdict, deque


class PipeBroker:
    def __init__(self, max_payload_bytes: int) -> None:
        self.max_payload_bytes = max_payload_bytes
        self.messages: dict[str, deque[bytes]] = defaultdict(deque)
        self.conditions: dict[str, asyncio.Condition] = defaultdict(asyncio.Condition)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            header = await reader.readline()
            if not header:
                return
            if not header.startswith(b"{"):
                await self.handle_native(header, reader, writer)
                return
            request = json.loads(header.decode("utf-8"))
            name = str(request["pipe"])
            operation = str(request["op"])
            if operation == "write":
                payload = base64.b64decode(request["payload"], validate=True)
                if len(payload) > self.max_payload_bytes:
                    raise ValueError("payload exceeds configured limit")
                async with self.conditions[name]:
                    self.messages[name].append(payload)
                    self.conditions[name].notify(1)
                response = {"ok": True, "bytes": len(payload)}
            elif operation == "read":
                expected = int(request["bytes"])
                if expected < 0 or expected > self.max_payload_bytes:
                    raise ValueError("invalid requested byte count")
                async with self.conditions[name]:
                    await self.conditions[name].wait_for(lambda: bool(self.messages[name]))
                    payload = self.messages[name].popleft()
                if len(payload) != expected:
                    raise ValueError(f"payload size mismatch: expected {expected}, got {len(payload)}")
                response = {"ok": True, "payload": base64.b64encode(payload).decode("ascii")}
            else:
                raise ValueError(f"unsupported operation: {operation}")
        except Exception as error:
            response = {"ok": False, "error": str(error)}
        writer.write((json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8"))
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def handle_native(
        self, header: bytes, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Serve the compact protocol used by the C++ PipeComm overlay.

        ``W name bytes`` is followed by exactly ``bytes`` payload bytes;
        ``R name bytes`` waits for one same-size payload. Pipe names contain no
        whitespace in the upstream protocol, so this framing is unambiguous.
        """
        try:
            operation, name, byte_count_text = header.decode("utf-8").rstrip("\n").split(" ", 2)
            byte_count = int(byte_count_text)
            if byte_count < 0 or byte_count > self.max_payload_bytes:
                raise ValueError("invalid payload size")
            if operation == "W":
                payload = await reader.readexactly(byte_count)
                async with self.conditions[name]:
                    self.messages[name].append(payload)
                    self.conditions[name].notify(1)
                writer.write(b"OK\n")
            elif operation == "R":
                async with self.conditions[name]:
                    await self.conditions[name].wait_for(lambda: bool(self.messages[name]))
                    payload = self.messages[name].popleft()
                if len(payload) != byte_count:
                    raise ValueError(f"payload size mismatch: expected {byte_count}, got {len(payload)}")
                writer.write(f"OK {len(payload)}\n".encode("utf-8"))
                writer.write(payload)
            else:
                raise ValueError(f"unsupported native operation: {operation}")
            await writer.drain()
        except Exception as error:
            writer.write((f"ERR {error}\n").encode("utf-8"))
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


async def main_async(port: int, max_payload_bytes: int) -> None:
    broker = PipeBroker(max_payload_bytes)
    server = await asyncio.start_server(broker.handle, "0.0.0.0", port)
    async with server:
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9400)
    parser.add_argument("--max-payload-bytes", type=int, default=64 * 1024 * 1024)
    arguments = parser.parse_args()
    asyncio.run(main_async(arguments.port, arguments.max_payload_bytes))


if __name__ == "__main__":
    main()
