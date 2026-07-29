#!/usr/bin/env python3
"""Expose a localhost Docker registry to VMware VMnet8 guests.

Docker Desktop's published registry port is reachable from Windows localhost,
but not directly from the VMnet8 adapter.  This small transparent TCP relay
binds that adapter address and forwards each connection to the local registry.
"""
from __future__ import annotations

import argparse
import socket
import threading


def relay(source: socket.socket, destination: socket.socket) -> None:
    """Copy bytes in one direction until either side closes."""
    try:
        while data := source.recv(1024 * 1024):
            destination.sendall(data)
    finally:
        try:
            destination.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def handle(client: socket.socket, target_host: str, target_port: int) -> None:
    try:
        upstream = socket.create_connection((target_host, target_port), timeout=20)
        first = threading.Thread(target=relay, args=(client, upstream), daemon=True)
        second = threading.Thread(target=relay, args=(upstream, client), daemon=True)
        first.start()
        second.start()
        first.join()
        second.join()
        upstream.close()
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-host", default="192.168.244.1")
    parser.add_argument("--listen-port", type=int, default=5000)
    parser.add_argument("--target-host", default="127.0.0.1")
    parser.add_argument("--target-port", type=int, default=5000)
    arguments = parser.parse_args()

    listener = socket.create_server((arguments.listen_host, arguments.listen_port), reuse_port=False)
    listener.listen()
    print(f"registry relay listening on {arguments.listen_host}:{arguments.listen_port}", flush=True)
    while True:
        client, _ = listener.accept()
        threading.Thread(
            target=handle,
            args=(client, arguments.target_host, arguments.target_port),
            daemon=True,
        ).start()


if __name__ == "__main__":
    main()
