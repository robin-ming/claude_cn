import contextlib
import socket
import socketserver
import tempfile
import threading
import unittest
from pathlib import Path

from claude_cn.policy import Policy
from claude_cn.proxy import Server, authority


class Origin(socketserver.StreamRequestHandler):
    def handle(self):
        first = self.rfile.readline()
        headers = []
        while True:
            line = self.rfile.readline()
            if line == b'\r\n' or not line:
                break
            headers.append(line)
        self.server.received = first + b''.join(headers)
        body = b'origin-ok'
        self.wfile.write(b'HTTP/1.1 200 OK\r\nContent-Length: 9\r\nConnection: close\r\n\r\n' + body)


@contextlib.contextmanager
def running(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class ProxyTests(unittest.TestCase):
    def request(self, server, request):
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(3)
            sock.connect(server.server_address)
            sock.sendall(request)
            data = b''
            while True:
                part = sock.recv(4096)
                if not part:
                    return data
                data += part

    def test_absolute_http_host_rewrite_and_credentials_removed(self):
        with running(socketserver.ThreadingTCPServer(('127.0.0.1', 0), Origin)) as origin:
            port = origin.server_address[1]
            policy = Policy({'ports': [port], 'allow_cidrs': ['127.0.0.1/32']}, [])
            with tempfile.TemporaryDirectory() as tmp, running(Server(str(Path(tmp)/'p.sock'), policy)) as proxy:
                result = self.request(proxy, f'GET http://127.0.0.1:{port}/test HTTP/1.1\r\nHost: evil.example\r\nProxy-Authorization: secret\r\n\r\n'.encode())
            self.assertIn(b'origin-ok', result)
            self.assertIn(f'Host: 127.0.0.1:{port}'.encode(), origin.received)
            self.assertNotIn(b'evil.example', origin.received)
            self.assertNotIn(b'secret', origin.received)

    def test_connect_tunnel_and_half_close(self):
        with running(socketserver.ThreadingTCPServer(('127.0.0.1', 0), Origin)) as origin:
            port = origin.server_address[1]
            policy = Policy({'ports': [port], 'allow_cidrs': ['127.0.0.1/32']}, [])
            with tempfile.TemporaryDirectory() as tmp, running(Server(str(Path(tmp)/'p.sock'), policy)) as proxy:
                with socket.socket(socket.AF_UNIX) as sock:
                    sock.settimeout(3)
                    sock.connect(proxy.server_address)
                    sock.sendall(f'CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\n'.encode())
                    self.assertIn(b'200 Connection Established', sock.recv(4096))
                    sock.sendall(b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n')
                    sock.shutdown(socket.SHUT_WR)
                    result = b''
                    while part := sock.recv(4096):
                        result += part
                    self.assertIn(b'origin-ok', result)

    def test_denied_foreign_ip_and_private_domain(self):
        with tempfile.TemporaryDirectory() as tmp, running(Server(str(Path(tmp)/'p.sock'), Policy({}, []))) as proxy:
            for host in ('1.1.1.1:443', '127.0.0.1:443', '[::1]:443'):
                result = self.request(proxy, f'CONNECT {host} HTTP/1.1\r\n\r\n'.encode())
                self.assertTrue(result.startswith(b'HTTP/1.1 403'), result)

    def test_malformed_authority_rejected(self):
        for value in ('user:password@example.com:443', 'host:443/path', 'host:99999', 'host'):
            with self.assertRaises(ValueError):
                authority(value)


if __name__ == '__main__':
    unittest.main()
