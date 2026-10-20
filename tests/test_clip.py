import contextlib
import io
import os
import socket
import socketserver
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from claude_cn import clip


class BridgeFake(socketserver.StreamRequestHandler):
    def handle(self):
        self.server.received = self.rfile.readline(256)
        self.wfile.write(self.server.response)


class FakeBridge(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, response):
        self.response = response
        self.received = None
        super().__init__(('127.0.0.1', 0), BridgeFake)


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


class XclipShimTests(unittest.TestCase):
    def connector(self, server):
        address = server.server_address
        return lambda: socket.create_connection(address, timeout=3)

    def test_targets_query_over_tcp(self):
        payload = b'TARGETS\nimage/png\ntext/plain\n'
        with running(FakeBridge(b'OK %d\n' % len(payload) + payload)) as server:
            out = io.BytesIO()
            code = clip.xclip_main(['-selection', 'clipboard', '-t', 'TARGETS', '-o'],
                                   connect=self.connector(server), out=out)
        self.assertEqual((code, out.getvalue(), server.received), (0, payload, b'TARGETS\n'))

    def test_binary_payload_passthrough(self):
        payload = bytes(range(256)) * 4
        with running(FakeBridge(b'OK %d\n' % len(payload) + payload)) as server:
            out = io.BytesIO()
            code = clip.xclip_main(['-selection', 'clipboard', '-t', 'image/png', '-o'],
                                   connect=self.connector(server), out=out)
        self.assertEqual((code, out.getvalue()), (0, payload))
        self.assertEqual(server.received, b'GET image/png\n')

    def test_plain_read_defaults_to_text(self):
        with running(FakeBridge(b'OK 2\nhi')) as server:
            out = io.BytesIO()
            code = clip.xclip_main(['-o'], connect=self.connector(server), out=out)
        self.assertEqual((code, out.getvalue(), server.received), (0, b'hi', b'GET text/plain\n'))

    def test_write_direction_primary_and_unknown_flags_refused_without_sending(self):
        with running(FakeBridge(b'OK 1\nx')) as server:
            connect = self.connector(server)
            for argv in (['-selection', 'clipboard'],
                         ['-selection', 'primary', '-t', 'text/plain', '-o'],
                         ['-loops', '5'],
                         ['-t']):
                self.assertEqual(clip.xclip_main(argv, connect=connect, out=io.BytesIO()), 1, argv)
        self.assertIsNone(server.received)

    def test_version_probe_succeeds(self):
        out = io.BytesIO()
        self.assertEqual(clip.xclip_main(['-version'], connect=lambda: None, out=out), 0)
        self.assertIn(b'claude-cn bridge', out.getvalue())


class WlPasteShimTests(unittest.TestCase):
    def connector(self, server):
        address = server.server_address
        return lambda: socket.create_connection(address, timeout=3)

    def test_variants(self):
        for argv, sent, response in ((['-l'], b'TARGETS\n', b'OK 1\nx'),
                                     (['--list-types'], b'TARGETS\n', b'OK 1\nx'),
                                     (['--type', 'image/png'], b'GET image/png\n', b'OK 1\nx'),
                                     (['--no-newline'], b'GET text/plain\n', b'OK 5\nhello')):
            with running(FakeBridge(response)) as server:
                out = io.BytesIO()
                code = clip.wl_paste_main(argv, connect=self.connector(server), out=out)
            self.assertEqual((code, server.received), (0, sent), argv)

    def test_primary_and_unknown_refused(self):
        with running(FakeBridge(b'OK 1\nx')) as server:
            connect = self.connector(server)
            for argv in (['--primary'], ['-o'], ['--watch']):
                self.assertEqual(clip.wl_paste_main(argv, connect=connect, out=io.BytesIO()), 1, argv)
        self.assertIsNone(server.received)


class RequestTests(unittest.TestCase):
    def test_error_response_raises(self):
        with running(FakeBridge(b'ERR no graphical session\n')) as server:
            address = server.server_address
            with self.assertRaises(clip.BridgeError):
                clip.request('GET text/plain', connect=lambda: socket.create_connection(address, timeout=3))

    def test_truncated_response_raises(self):
        with running(FakeBridge(b'OK 100\nshort')) as server:
            address = server.server_address
            with self.assertRaises(clip.BridgeError):
                clip.request('GET text/plain', connect=lambda: socket.create_connection(address, timeout=3))


class ContextTests(unittest.TestCase):
    def context(self, runner=lambda *a: (_ for _ in ()).throw(AssertionError('runner must not run'))):
        return clip.Context(1000, 1000, '/home/u', runner=runner,
                            discover=lambda uid: (_ for _ in ()).throw(AssertionError('discover must not run')))

    def test_mime_gate(self):
        context = self.context()
        for line, expected in (('GET text/html', b'ERR mime not allowed\n'),
                               ('GET image/tiff', b'ERR mime not allowed\n'),
                               ('GET ../etc/passwd', b'ERR mime not allowed\n'),
                               ('GET image/png/extra', b'ERR mime not allowed\n'),
                               ('FOO', b'ERR malformed request\n'),
                               ('', b'ERR malformed request\n')):
            self.assertEqual(context.handle(line), expected, line)

    def test_serves_targets_and_mimes_via_x11(self):
        calls = []

        def runner(argv, env, uid, gid):
            calls.append((argv, env, uid, gid))
            return b'hello'

        context = clip.Context(1000, 1000, '/home/u', runner=runner,
                               discover=lambda uid: {'DISPLAY': ':0', 'XAUTHORITY': '/home/u/.Xauthority'})
        with mock.patch.object(clip, 'XCLIP', '/bin/true'):
            self.assertEqual(context.handle('TARGETS'), b'OK 5\nhello')
            self.assertEqual(context.handle('GET text/plain'), b'OK 5\nhello')
        argv, env, uid, gid = calls[0]
        self.assertEqual(argv, ['/bin/true', '-selection', 'clipboard', '-t', 'TARGETS', '-o'])
        self.assertEqual(env, {'PATH': '/usr/bin:/bin', 'HOME': '/home/u', 'DISPLAY': ':0',
                               'XAUTHORITY': '/home/u/.Xauthority'})
        self.assertEqual((uid, gid), (1000, 1000))

    def test_wayland_plan(self):
        calls = []

        def runner(argv, env, uid, gid):
            calls.append(argv)
            return b'x'

        context = clip.Context(1000, 1000, '/home/w', runner=runner,
                               discover=lambda uid: {'WAYLAND_DISPLAY': 'wayland-0',
                                                     'XDG_RUNTIME_DIR': '/run/user/1000'})
        with mock.patch.object(clip, 'XCLIP', '/nonexistent/xclip'), mock.patch.object(clip, 'WLPASTE', '/bin/true'):
            for line in ('TARGETS', 'GET text/plain', 'GET image/png'):
                self.assertTrue(context.handle(line).startswith(b'OK 1\n'), line)
        self.assertEqual(calls, [['/bin/true', '-l'], ['/bin/true'], ['/bin/true', '--type', 'image/png']])

    def test_no_tool_available(self):
        context = clip.Context(1000, 1000, '/home/u', runner=lambda *a: b'x',
                               discover=lambda uid: {'DISPLAY': ':0'})
        with mock.patch.object(clip, 'XCLIP', '/nonexistent/xclip'), mock.patch.object(clip, 'WLPASTE', '/nonexistent/wl-paste'):
            self.assertEqual(context.handle('GET text/plain'), b'ERR no clipboard tool for this session\n')

    def test_no_graphical_session(self):
        context = clip.Context(1000, 1000, '/home/u', runner=lambda *a: b'x', discover=lambda uid: None)
        self.assertEqual(context.handle('GET text/plain'), b'ERR no graphical session\n')

    def test_rate_limit(self):
        context = clip.Context(1000, 1000, '/home/u', runner=lambda *a: b'', discover=lambda uid: {'DISPLAY': ':0'})
        for _ in range(clip.RATE_LIMIT):
            self.assertEqual(context.handle('GET text/plain'), b'OK 0\n')
        self.assertEqual(context.handle('GET text/plain'), b'ERR rate limited\n')


class DiscoverEnvTests(unittest.TestCase):
    def test_scans_fake_proc(self):
        with tempfile.TemporaryDirectory() as tmp:
            x11 = Path(tmp)/'42'
            x11.mkdir()
            (x11/'environ').write_bytes(b'PATH=/bin\0DISPLAY=:0\0XAUTHORITY=/home/u/.Xauthority\0')
            found = clip.discover_env(tmp, uid=os.getuid())
            self.assertEqual((found.get('DISPLAY'), found.get('XAUTHORITY')), (':0', '/home/u/.Xauthority'))
            self.assertIsNone(clip.discover_env(tmp, uid=65534))

    def test_wayland_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            wayland = Path(tmp)/'7'
            wayland.mkdir()
            (wayland/'environ').write_bytes(b'WAYLAND_DISPLAY=wayland-0\0XDG_RUNTIME_DIR=/run/user/1000\0')
            found = clip.discover_env(tmp, uid=os.getuid())
            self.assertEqual(found.get('WAYLAND_DISPLAY'), 'wayland-0')

    def test_empty_proc(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(clip.discover_env(tmp, uid=os.getuid()))


class RunToolTests(unittest.TestCase):
    def test_captures_stdout(self):
        output = clip.run_tool(['/bin/echo', '-n', 'clip-ok'], {'PATH': '/usr/bin:/bin'},
                               os.getuid(), os.getgid())
        self.assertEqual(output, b'clip-ok')

    def test_reports_failure(self):
        with self.assertRaises(clip.BridgeError):
            clip.run_tool(['/bin/false'], {'PATH': '/usr/bin:/bin'}, os.getuid(), os.getgid())

    def test_timeout_kills_tool(self):
        with self.assertRaises(clip.BridgeError):
            clip.run_tool(['/bin/sleep', '30'], {'PATH': '/usr/bin:/bin'},
                          os.getuid(), os.getgid(), timeout=0.5)


class GrantRelayTests(unittest.TestCase):
    def test_missing_user_and_failed_chown_do_not_raise(self):
        with mock.patch.object(clip.pwd, 'getpwnam', side_effect=KeyError):
            clip.grant_relay_access('/nonexistent/clip.sock')
        with mock.patch.object(clip.pwd, 'getpwnam', side_effect=PermissionError('EPERM')):
            clip.grant_relay_access('/nonexistent/clip.sock')

    def test_chown_called_with_relay_owner(self):
        owner = mock.Mock(pw_uid=900, pw_gid=900)
        with mock.patch.object(clip.pwd, 'getpwnam', return_value=owner) as getpwnam, \
             mock.patch.object(clip.os, 'chown') as chown:
            clip.grant_relay_access('/run/claude-cn-clip/clip.sock')
        getpwnam.assert_called_once_with('claude-cn-proxy')
        chown.assert_called_once_with('/run/claude-cn-clip/clip.sock', 900, 900)


class DaemonEndToEnd(unittest.TestCase):
    def test_unix_socket_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp)/'clip.sock')
            context = clip.Context(1000, 1000, '/home/u', runner=lambda *a: b'data',
                                   discover=lambda uid: {'DISPLAY': ':0'})
            with running(clip.Server(path, context)):
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(3)
                    client.connect(path)
                    client.sendall(b'GET text/plain\n')
                    data = b''
                    while True:
                        part = client.recv(4096)
                        if not part:
                            break
                        data += part
        self.assertEqual(data, b'OK 4\ndata')


if __name__ == '__main__':
    unittest.main()
