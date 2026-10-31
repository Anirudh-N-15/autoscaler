import socket
import json
import hashlib
import time
import os
from concurrent.futures import ThreadPoolExecutor

HOST = "0.0.0.0"
PORT = 9000
MAX_WORKERS = 8


def handle_client(conn, addr):
    try:
        data = b""

        while b"\n" not in data and len(data) < 4096:
            chunk = conn.recv(4096)
            if not chunk:
                return
            data += chunk

        request = json.loads(data.split(b"\n", 1)[0].decode())
        iterations = max(1, min(int(request.get("iterations", 100000)), 5_000_000))

        start = time.perf_counter()
        result = b"cloud-autoscaler"

        for _ in range(iterations):
            result = hashlib.sha256(result).digest()

        elapsed = time.perf_counter() - start

        response = {
            "server": socket.gethostname(),
            "pid": os.getpid(),
            "iterations": iterations,
            "elapsed_seconds": round(elapsed, 4),
            "result": result.hex()
        }

        conn.sendall((json.dumps(response) + "\n").encode())

    except Exception as exc:
        print(f"Request from {addr} failed: {exc}", flush=True)

    finally:
        conn.close()


def main():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((HOST, PORT))
        server.listen(128)

        print(f"Server listening on {HOST}:{PORT}", flush=True)

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            while True:
                conn, addr = server.accept()
                pool.submit(handle_client, conn, addr)


if __name__ == "__main__":
    main()