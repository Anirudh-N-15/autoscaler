import socket
import json

host = "192.168.122.144"
port = 9000

with socket.create_connection((host, port), timeout=5) as s:
    s.settimeout(30)
    request = {"iterations": 100000}
    s.sendall((json.dumps(request) + "\n").encode())

    response = b""
    while b"\n" not in response:
        chunk = s.recv(4096)
        if not chunk:
            break
        response += chunk

    print(response.decode())
