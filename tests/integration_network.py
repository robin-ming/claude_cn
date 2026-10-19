#!/usr/bin/python3
"""Root-only isolated router/client/server lab; never edits host firewall/sysctls.

Run: sudo python3 tests/integration_network.py
No model requests, Internet access, or installed Claude services are required.
"""
import ctypes
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import uuid

ROOT = (Path('/usr/lib/claude-cn') if Path(__file__).resolve().parent == Path('/usr/share/claude-cn')
        else Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT))


def call(args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, **kwargs)


def echo():
    servers = []
    for family in (socket.AF_INET, socket.AF_INET6):
        for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
            sock = socket.socket(family, kind)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            sock.bind(('::' if family == socket.AF_INET6 else '0.0.0.0', 29876))
            if kind == socket.SOCK_STREAM:
                sock.listen(10)
            servers.append(sock)

            def serve(s=sock, k=kind):
                while True:
                    if k == socket.SOCK_DGRAM:
                        data, peer = s.recvfrom(1024)
                        s.sendto(data, peer)
                    else:
                        conn, _ = s.accept()
                        with conn:
                            conn.sendall(conn.recv(1024))
            threading.Thread(target=serve, daemon=True).start()
    print('READY', flush=True)
    signal.pause()


def client():
    from claude_cn.guard import install
    os.setgroups([])
    os.setgid(65534)
    os.setuid(65534)
    install()
    for ip in ('223.5.5.5', '240e::5', '10.23.4.5'):
        family = socket.AF_INET6 if ':' in ip else socket.AF_INET
        for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
            with socket.socket(family, kind) as sock:
                sock.settimeout(3)
                sock.connect((ip, 29876))
                sock.sendall(b'claude-cn-ok')
                assert sock.recv(1024) == b'claude-cn-ok', (ip, kind)
            print('PASS direct', ip, 'TCP' if kind == socket.SOCK_STREAM else 'UDP', flush=True)
        result = subprocess.run(['/usr/bin/ping', '-n', '-c', '1', '-W', '2', ip], capture_output=True)
        assert result.returncode == 0, (ip, result.stderr.decode())
        print('PASS ping', ip, flush=True)
    for ip in ('1.1.1.1', '2606:4700::1111'):
        family = socket.AF_INET6 if ':' in ip else socket.AF_INET
        for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
            with socket.socket(family, kind) as sock:
                sock.settimeout(2)
                try:
                    sock.connect((ip, 29876))
                    sock.sendall(b'must-not-arrive')
                    sock.recv(1024)
                except OSError:
                    pass
                else:
                    raise AssertionError(('foreign packet escaped', ip, kind))
            print('PASS blocked', ip, kind, flush=True)
        result = subprocess.run(['/usr/bin/ping', '-n', '-c', '1', '-W', '1', ip], capture_output=True)
        assert result.returncode != 0, ip
        print('PASS blocked foreign ping', ip, flush=True)


def inside_router(tmp, server, client_ns):
    from claude_cn import network
    network.NS = client_ns
    network.STATE = Path(tmp)/'state.json'
    network.POLICY = str(Path(tmp)/'policy.json')
    network.COUNTRY = str(Path(tmp)/'country.json')
    network.RESOLV = Path(tmp)/'resolv.conf'
    Path(network.POLICY).write_text(json.dumps({'allow_domains': ['github.com'], 'allow_cidrs': ['10.0.0.0/8']}))
    Path(network.COUNTRY).write_text(json.dumps({'schema': 1, 'networks': ['223.5.5.0/24', '240e::/20']}))
    # Prove that the "foreign" destinations exist and reply before testing denial.
    for ip in ('1.1.1.1', '2606:4700::1111'):
        call(['/usr/bin/ping', '-n', '-c', '1', '-W', '2', ip])
    try:
        network.up()
        result = call(['ip', 'netns', 'exec', client_ns, sys.executable, str(Path(__file__).resolve()), '--client'])
        print(result.stdout, end='', flush=True)
        # Reload must preserve connectivity and a closed foreign boundary.
        network.reload_rules()
        result = call(['ip', 'netns', 'exec', client_ns, sys.executable, str(Path(__file__).resolve()), '--client'])
        print('After atomic reload:\n' + result.stdout, end='', flush=True)
    finally:
        network.down()


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run with sudo python3 tests/integration_network.py')
    if len(sys.argv) > 1:
        if sys.argv[1] == '--echo':
            return echo()
        if sys.argv[1] == '--client':
            return client()
        if sys.argv[1] == '--router':
            return inside_router(*sys.argv[2:])
        raise SystemExit('unknown mode')
    suffix = uuid.uuid4().hex[:8]
    router, server, client_ns = ['cc-test-' + s + '-' + suffix for s in ('r', 's', 'c')]
    namespaces = []
    proc = None
    try:
        for ns in (router, server):
            call(['ip', 'netns', 'add', ns])
            namespaces.append(ns)
            call(['ip', '-n', ns, 'link', 'set', 'lo', 'up'])
        call(['ip', '-n', router, 'link', 'add', 'lab-r', 'type', 'veth', 'peer', 'name', 'lab-s'])
        call(['ip', '-n', router, 'link', 'set', 'lab-s', 'netns', server])
        for ns, iface, addresses in ((router, 'lab-r', ['223.5.5.1/24', '1.1.1.254/24', '10.23.4.1/24', '240e::1/64', '2606:4700::1/64']),
                                      (server, 'lab-s', ['223.5.5.5/24', '1.1.1.1/24', '10.23.4.5/24', '240e::5/64', '2606:4700::1111/64'])):
            for ip in addresses:
                options = ['-6'] if ':' in ip else []
                call(['ip', '-n', ns, *options, 'addr', 'add', ip, 'dev', iface, *(['nodad'] if options else [])])
            call(['ip', '-n', ns, 'link', 'set', iface, 'up'])
        proc = subprocess.Popen(['ip', 'netns', 'exec', server, sys.executable, str(Path(__file__).resolve()), '--echo'],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert proc.stdout.readline().strip() == 'READY', 'echo server failed'
        with tempfile.TemporaryDirectory(prefix='claude-cn-lab-') as tmp:
            result = subprocess.run(['ip', 'netns', 'exec', router, sys.executable,
                                     str(Path(__file__).resolve()), '--router', tmp, server, client_ns],
                                    text=True, capture_output=True, timeout=90)
            print(result.stdout, end='')
            if result.returncode:
                raise RuntimeError(result.stderr)
        print('PASS isolated routed-network integration')
    finally:
        if proc is not None:
            proc.terminate()
            proc.wait(timeout=5)
        for ns in [client_ns, *reversed(namespaces)]:
            subprocess.run(['ip', 'netns', 'delete', ns], capture_output=True)


if __name__ == '__main__':
    main()
