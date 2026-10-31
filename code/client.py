#!/usr/bin/python3
"""Generate JSON/TCP workload through the load balancer."""

import argparse
import json
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed


def send_one(host, port, iterations, timeout):
    request = {"iterations": iterations}
    start = time.perf_counter()
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall((json.dumps(request) + "\n").encode())
        data = bytearray()
        while b"\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > 1024 * 1024:
                raise RuntimeError("Response exceeded 1 MiB")
    if not data:
        raise RuntimeError("No response from load balancer")
    response = json.loads(bytes(data).split(b"\n", 1)[0].decode())
    response["round_trip_seconds"] = round(time.perf_counter() - start, 4)
    return response


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1",
                        help="Debian host address running the load balancer")
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--iterations", type=int, default=1000000,
                        help="SHA-256 iterations per request (max 5000000)")
    parser.add_argument("--requests", type=int, default=100,
                        help="Total number of requests")
    parser.add_argument("--workers", type=int, default=8,
                        help="Concurrent client workers")
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--delay", type=float, default=0.0,
                        help="Optional delay between submitted requests")
    args = parser.parse_args()

    if args.requests < 1 or args.workers < 1:
        parser.error("--requests and --workers must be positive")
    iterations = max(1, min(args.iterations, 5_000_000))

    print(
        f"Sending {args.requests} requests to {args.host}:{args.port}; "
        f"iterations={iterations}, workers={args.workers}"
    )
    completed = 0
    failures = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = []
        for _ in range(args.requests):
            futures.append(pool.submit(
                send_one, args.host, args.port, iterations, args.timeout
            ))
            if args.delay:
                time.sleep(args.delay)

        for future in as_completed(futures):
            try:
                response = future.result()
                completed += 1
                print(
                    f"[{completed}/{args.requests}] "
                    f"server={response.get('server', '?')} "
                    f"pid={response.get('pid', '?')} "
                    f"server_time={response.get('elapsed_seconds', '?')}s "
                    f"round_trip={response.get('round_trip_seconds', '?')}s"
                )
                if "error" in response:
                    failures += 1
                    print(f"  Backend error: {response['error']}")
            except Exception as exc:
                failures += 1
                print(f"Request failed: {exc}")

    print(
        f"Finished: {completed} responses, {failures} failed/error requests."
    )


if __name__ == "__main__":
    main()
