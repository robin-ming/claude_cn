"""Run with claude-cn self-test after installation; uses no API credits."""
import errno
import json
import os
import socket
import subprocess
import urllib.error
import urllib.request


def main():
    failed = []

    def record(name, ok, detail=''):
        print(f'{"PASS" if ok else "FAIL"} {name}' + (f': {detail}' if detail else ''), flush=True)
        if not ok:
            failed.append(name)

    development = os.environ.get('CLAUDE_CN_MODE') == 'development'
    print('mode:', 'development' if development else 'strict')
    print('namespace:', os.readlink('/proc/self/ns/net'))
    for family, target in ((socket.AF_INET, ('223.5.5.5', 53)),
                           (socket.AF_INET6, ('2400:3200::1', 53, 0, 0))):
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.settimeout(5)
            try:
                sock.connect(target)
                record(f'mainland direct IPv{4 if family == socket.AF_INET else 6}', True)
            except OSError as exc:
                record(f'mainland direct IPv{4 if family == socket.AF_INET else 6}', False, type(exc).__name__)
    # Numeric DNS query: no host resolver, proxy, API Key, or model call involved.
    query = b'\x43\x4e\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x03www\x05baidu\x03com\x00\x00\x01\x00\x01'
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(5)
        try:
            sock.sendto(query, ('223.5.5.5', 53))
            reply, _ = sock.recvfrom(4096)
            record('mainland direct UDP/DNS', reply[:2] == query[:2] and len(reply) >= 12)
        except OSError as exc:
            record('mainland direct UDP/DNS', False, type(exc).__name__)
    result = subprocess.run(['/usr/bin/ping', '-n', '-c', '1', '-W', '3', '223.5.5.5'], capture_output=True)
    record('mainland direct ping', result.returncode == 0,
           'ICMP response required; remote network may filter ping' if result.returncode else '')
    for family, target in ((socket.AF_INET, ('1.1.1.1', 443)),
                           (socket.AF_INET6, ('2606:4700:4700::1111', 443, 0, 0))):
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.settimeout(3)
            try:
                sock.connect(target)
                record(f'direct IPv{4 if family == socket.AF_INET else 6} blocked', False)
            except OSError:
                record(f'direct IPv{4 if family == socket.AF_INET else 6} blocked', True)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(2)
        try:
            sock.connect(('1.1.1.1', 53))
            sock.sendall(query)
            sock.recv(4096)
        except OSError as exc:
            # A timeout alone cannot prove blocking. The root-only lab and nft
            # counters provide stronger evidence than an unreachable remote host.
            record('foreign direct UDP rejected', not isinstance(exc, TimeoutError), type(exc).__name__)
        else:
            record('foreign direct UDP rejected', False)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM):
            record('Unix socket mode policy', development)
    except OSError as exc:
        record('Unix socket mode policy', not development and exc.errno == errno.EPERM)
    with socket.socket() as sock:
        sock.settimeout(5)
        try:
            sock.connect(('127.0.0.1', 18080))
            sock.sendall(b'CONNECT 1.1.1.1:443 HTTP/1.1\r\nHost: 1.1.1.1:443\r\n\r\n')
            record('foreign IP denied by proxy', sock.recv(4096).startswith(b'HTTP/1.1 403'))
        except OSError as exc:
            record('foreign IP denied by proxy', False, type(exc).__name__)
    for label, url in (('mainland website', 'https://www.baidu.com/'),
                       ('GitHub whitelist', 'https://github.com/')):
        try:
            with urllib.request.urlopen(url, timeout=20) as response:
                record(label, True, f'HTTP {response.status}')
        except urllib.error.HTTPError as exc:
            record(label, exc.code != 403, f'HTTP {exc.code}')
        except OSError as exc:
            record(label, False, type(exc).__name__)
    print('Provider configuration path:', os.environ.get('CLAUDE_CN_SETTINGS'))
    raise SystemExit(1 if failed else 0)
