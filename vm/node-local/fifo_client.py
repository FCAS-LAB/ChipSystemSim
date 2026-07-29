#!/usr/bin/env python3
"""Small reference client used to verify the remote FIFO broker protocol."""
from __future__ import annotations

import argparse
import asyncio
import base64
import json


async def call(host: str, port: int, request: dict[str, object]) -> dict[str, object]:
    reader, writer = await asyncio.open_connection(host, port)
    writer.write((json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8"))
    await writer.drain()
    response = json.loads((await reader.readline()).decode("utf-8"))
    writer.close()
    await writer.wait_closed()
    if not response.get("ok"):
        raise RuntimeError(str(response["error"]))
    return response


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("read", "write"))
    parser.add_argument("pipe")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9400)
    parser.add_argument("--data")
    parser.add_argument("--bytes", type=int)
    arguments = parser.parse_args()
    if arguments.operation == "write":
        if arguments.data is None:
            parser.error("--data is required for write")
        request: dict[str, object] = {"op": "write", "pipe": arguments.pipe,
                                      "payload": base64.b64encode(arguments.data.encode()).decode("ascii")}
    else:
        if arguments.bytes is None:
            parser.error("--bytes is required for read")
        request = {"op": "read", "pipe": arguments.pipe, "bytes": arguments.bytes}
    response = asyncio.run(call(arguments.host, arguments.port, request))
    if arguments.operation == "read":
        print(base64.b64decode(str(response["payload"])).decode("utf-8"))


if __name__ == "__main__":
    main()
