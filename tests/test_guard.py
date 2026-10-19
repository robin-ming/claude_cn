import subprocess
import sys
import unittest


class GuardTests(unittest.TestCase):
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
