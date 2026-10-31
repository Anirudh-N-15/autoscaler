#!/usr/bin/python3
"""Small TCP load balancer for newline-delimited JSON requests."""

import json
import os
import socket
import socketserver
import threading

LISTEN_HOST = os.environ.get("LB_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LB_PORT", "9100"))
BACKENDS_FILE = os.environ.get(
    "BACKENDS_FILE", "/tmp/cloud-autoscaler-backends.json"
)
CONNECT_TIMEOUT = 2.0
RESPONSE_TIMEOUT = 30.0

_state_lock = threading.Lock()
_next_backend = 0


def get_backends():
    try:
        with open(BACKENDS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        backends = data.get("backends", [])
        return [
            (str(item["host"]), int(item.get("port", 9000)))
            for item in backends
            if item.get("host")
        ]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def choose_backend(backends):
    global _next_backend
    with _state_lock:
        backend = backends[_next_backend % len(backends)]
        _next_backend += 1
        return backend


def read_line(sock, limit=1024 * 1024):
    data = bytearray()
    while len(data) < limit:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data.extend(chunk)
        if b"\n" in data:
            return bytes(data.split(b"\n", 1)[0] + b"\n")
    if b"\n" in data:
        return bytes(data.split(b"\n", 1)[0] + b"\n")
    return bytes(data)


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(RESPONSE_TIMEOUT)
        try:
            request_line = read_line(self.request)
            if not request_line:
                return

            backends = get_backends()
            if not backends:
                self.request.sendall(
                    b'{"error":"No healthy backends configured"}\n'
                )
                return

            # Round-robin across backends configured by autoscaler.py.
            backend = choose_backend(backends)
            try:
                with socket.create_connection(
                    backend, timeout=CONNECT_TIMEOUT
                ) as upstream:
                    upstream.settimeout(RESPONSE_TIMEOUT)
                    upstream.sendall(request_line)
                    response = read_line(upstream)
                if response:
                    self.request.sendall(response)
                else:
                    self.request.sendall(
                        b'{"error":"Backend closed without a response"}\n'
                    )
                print(
                    f"{self.client_address[0]} -> "
                    f"{backend[0]}:{backend[1]}",
                    flush=True,
                )
            except OSError as exc:
                print(f"Backend {backend} failed: {exc}", flush=True)
                self.request.sendall(
                    b'{"error":"Selected backend unavailable; retry request"}\n'
                )
        except (OSError, TimeoutError) as exc:
            print(f"Client {self.client_address} failed: {exc}", flush=True)


class ThreadedTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 256


def main():
    print(f"Load balancer listening on {LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    print(f"Backend config: {BACKENDS_FILE}", flush=True)
    with ThreadedTCPServer((LISTEN_HOST, LISTEN_PORT), Handler) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
