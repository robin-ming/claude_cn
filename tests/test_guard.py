import subprocess
import sys
import unittest


class GuardTests(unittest.TestCase):
    def test_development_restores_unix_socket_communication_after_exec(self):
        setup = "from claude_cn.guard import install; import os,sys; install('development'); os.execv(sys.executable,[sys.executable,'-c',sys.argv[1]])"
        child = """
import socket, tempfile
from pathlib import Path
with tempfile.TemporaryDirectory() as tmp:
    with socket.socket(socket.AF_UNIX) as server, socket.socket(socket.AF_UNIX) as client:
        address = str(Path(tmp)/'socket')
        server.bind(address)
        server.listen()
        client.connect(address)
        conn, _ = server.accept()
        with conn:
            conn.sendall(b'development')
            assert client.recv(20) == b'development'
"""
        result = subprocess.run([sys.executable, '-c', setup, child], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_environment_policy_for_modes(self):
        from claude_cn.entry import allowed_env
        for name in ('SSH_AUTH_SOCK', 'DISPLAY', 'WAYLAND_DISPLAY', 'XDG_RUNTIME_DIR',
                     'VIRTUAL_ENV', 'CMAKE_PREFIX_PATH', 'GITHUB_TOKEN'):
            self.assertTrue(allowed_env(name, 'development'), name)
            self.assertFalse(allowed_env(name, 'strict'), name)
        for name in ('LD_PRELOAD', 'PYTHONPATH', 'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CN_MODE',
                     'HTTPS_PROXY', 'no_proxy', 'invalid=name', 'bad\x00'):
            self.assertFalse(allowed_env(name, 'development'), name)

    def test_authoritative_mode_controls_clipboard_path_and_environment(self):
        from types import SimpleNamespace
        from claude_cn.entry import session_env, SANDBOX_BIN, PROXY
        from claude_cn.runtime import DEFAULT_RUNTIME
        account = SimpleNamespace(pw_dir='/home/test', pw_name='test')
        supplied = {'PATH': '/usr/bin:/bin', 'DISPLAY': ':0', 'SSH_AUTH_SOCK': '/tmp/agent',
                    'CLAUDE_CN_MODE': 'development', 'HTTPS_PROXY': 'http://untrusted'}
        strict = session_env(supplied, {**DEFAULT_RUNTIME, 'mode': 'strict'}, account, 123)
        development = session_env(supplied, {**DEFAULT_RUNTIME, 'mode': 'development'}, account, 123)
        self.assertEqual(strict['PATH'], SANDBOX_BIN + ':/usr/bin:/bin')
        self.assertNotIn('DISPLAY', strict)
        self.assertNotIn('SSH_AUTH_SOCK', strict)
        self.assertEqual(strict['CLAUDE_CN_MODE'], 'strict')
        self.assertEqual(development['PATH'], '/usr/bin:/bin')
        self.assertEqual(development['DISPLAY'], ':0')
        self.assertEqual(development['SSH_AUTH_SOCK'], '/tmp/agent')
        self.assertEqual(development['CLAUDE_CN_MODE'], 'development')
        for env in (strict, development):
            self.assertEqual(env['HTTPS_PROXY'], PROXY)
            self.assertEqual(env['NO_PROXY'], '')
            self.assertEqual(env['CLAUDE_CN_EXPECTED_NS'], '123')

    def test_unix_socket_blocked_after_exec_but_inet_and_socketpair_work(self):
        child = '''
import errno, socket
try:
    socket.socket(socket.AF_UNIX)
except OSError as exc:
    assert exc.errno == errno.EPERM
else:
    raise AssertionError('Unix socket was allowed')
with socket.socket(socket.AF_INET): pass
with socket.socket(socket.AF_INET6): pass
a,b=socket.socketpair()
a.sendall(b'ok')
assert b.recv(2)==b'ok'
a.close(); b.close()
assert 'NoNewPrivs:\\t1' in open('/proc/self/status').read()
'''
        setup = 'from claude_cn.guard import install; import os,sys; install(); os.execv(sys.executable,[sys.executable,"-c",sys.argv[1]])'
        result = subprocess.run([sys.executable, '-c', setup, child], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
