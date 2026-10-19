"""HTTP/CONNECT proxy on a filesystem Unix socket; no TLS interception."""
import argparse
import json
import logging
import os
import socket
import socketserver
import threading
from urllib.parse import urlsplit

from .policy import Policy, hostname

LOG = logging.getLogger('claude-cn')


def authority(value, default=None):
    parsed = urlsplit('//' + value)
    if parsed.username is not None or parsed.password is not None or parsed.path or parsed.query or parsed.fragment:
        raise ValueError('invalid authority')
    host = hostname(parsed.hostname or '')
    port = parsed.port or default
    if port is None or not 1 <= port <= 65535:
        raise ValueError('invalid port')
    return host, port


def pump(source, target):
    try:
        while True:
            data = source.recv(65536)
            if not data:
                break
            target.sendall(data)
    except OSError:
        pass
    finally:
        try:
            target.shutdown(socket.SHUT_WR)
        except OSError:
            pass


class Handler(socketserver.StreamRequestHandler):
    # Do not prefetch any TLS bytes or request body before switching to a tunnel.
    rbufsize = 0

    def response(self, status, text):
        body = (text + '\n').encode()
        self.wfile.write(f'HTTP/1.1 {status}\r\nConnection: close\r\nContent-Length: {len(body)}\r\n\r\n'.encode() + body)

    def handle(self):
        host, port = '', 0
        established = False
        try:
            self.connection.settimeout(self.server.policy.idle_timeout)
            line = self.rfile.readline(8193)
            if not line:
                return
            if len(line) > 8192:
                raise ValueError('request-line-too-long')
            method, target, version = line.decode('ascii').strip().split()
            if version not in ('HTTP/1.0', 'HTTP/1.1') or not method.isalpha():
                raise ValueError('invalid-request')
            headers, size = [], 0
            while True:
                line = self.rfile.readline(8193)
                size += len(line)
                if not line or len(line) > 8192 or size > 65536:
                    raise ValueError('invalid-headers')
                if line == b'\r\n':
                    break
                name, sep, value = line.partition(b':')
                if not sep or not name or any(c not in b"!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ" for c in name):
                    raise ValueError('invalid-header')
                if b'\r' in value[:-2] or b'\n' in value[:-2] or not value.endswith(b'\r\n'):
                    raise ValueError('invalid-header-value')
                headers.append((name, value.strip()))
            if method == 'CONNECT':
                host, port = authority(target)
                initial = None
            else:
                parsed = urlsplit(target)
                if parsed.scheme != 'http' or parsed.fragment:
                    raise ValueError('use-CONNECT-for-https')
                host, port = authority(parsed.netloc, 80)
                path = parsed.path or '/'
                if parsed.query:
                    path += '?' + parsed.query
                host_header = f'[{host}]' if ':' in host else host
                if port != 80:
                    host_header += f':{port}'
                initial = f'{method} {path} {version}\r\nHost: {host_header}\r\nConnection: close\r\n'.encode('ascii')
                for name, value in headers:
                    if name.lower() not in (b'host', b'connection', b'proxy-connection', b'proxy-authorization'):
                        initial += name + b': ' + value + b'\r\n'
                initial += b'\r\n'
            upstream, connected_ip, reason = self.server.policy.connect(host, port)
            with upstream:
                LOG.info(json.dumps({'event': 'allow', 'host': host, 'port': port,
                                     'ip': connected_ip, 'reason': reason}))
                if initial is None:
                    self.wfile.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                else:
                    upstream.sendall(initial)
                established = True
                forward = threading.Thread(target=pump, args=(self.connection, upstream), daemon=True)
                forward.start()
                pump(upstream, self.connection)
                # Closing downstream in finish() also ends a lingering upload thread.
        except PermissionError as exc:
            LOG.warning(json.dumps({'event': 'deny', 'host': host, 'port': port, 'reason': str(exc)}))
            if not established:
                self.response('403 Forbidden', 'claude-cn: destination denied by whitelist')
        except (ValueError, UnicodeError) as exc:
            if not established:
                self.response('400 Bad Request', 'claude-cn: invalid proxy request')
        except OSError:
            LOG.warning(json.dumps({'event': 'unreachable', 'host': host, 'port': port}))
            if not established:
                try:
                    self.response('502 Bad Gateway', 'claude-cn: connection failed')
                except OSError:
                    pass


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, path, policy):
        self.policy = policy
        self.slots = threading.BoundedSemaphore(64)
        super().__init__(path, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--country', required=True)
    parser.add_argument('--socket', required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    policy = Policy.load(args.config, args.country)
    try:
        os.unlink(args.socket)
    except FileNotFoundError:
        pass
    os.umask(0o077)
    with Server(args.socket, policy) as server:
        LOG.info('claude-cn proxy ready')
        server.serve_forever()


if __name__ == '__main__':
    main()
