import array
import contextlib
import json
import os
from pathlib import Path
import select
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from unittest.mock import patch

from claude_cn import broker, client


def request(**updates):
    data = {'cwd': '/tmp', 'env': {'TERM': 'xterm'}, 'args': [], 'tty': False}
    data.update(updates)
    return data


def fixture_runner(data, args, uid, fds, tty):
    # Exercises the real broker and client without requiring root or changing networks.
    os.setsid()
    for target, fd in enumerate(fds):
        os.dup2(fd, target)
    if tty:
        import fcntl
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    from claude_cn.guard import close_extra_fds
    close_extra_fds()
    os.execve(sys.executable, [sys.executable, '-c', args[0]], data['env'])


class BrokerTests(unittest.TestCase):
    def test_validate_untrusted_requests(self):
        broker.validate(request())
        for data in [request(cwd='relative'), request(env={'LD_PRELOAD': 'evil'}),
                     request(env={'LC_X\x00': 'bad'}), request(args=['bad\x00']),
                     request(args='bad'), request(tty=1), {**request(), 'uid': 0}]:
            with self.assertRaises(ValueError):
                broker.validate(data)

    def test_receive_rights_truncation_and_socket_rejection(self):
        with contextlib.ExitStack() as stack:
            left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            stack.enter_context(left)
            stack.enter_context(right)
            with open('/dev/null') as stream:
                for fds, packet in [([stream.fileno()]*3, json.dumps(request()).encode()),
                                    ([stream.fileno()]*2, json.dumps(request()).encode()),
                                    ([stream.fileno()]*3, b'x'*(broker.MAX_PACKET+1)),
                                    ([left.fileno()]*3, json.dumps(request()).encode())]:
                    left.sendmsg([packet], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', fds))])
                    if len(fds) == 3 and len(packet) < broker.MAX_PACKET and fds[0] == stream.fileno():
                        data, received = broker.receive(right)
                        self.assertEqual(data, request())
                        self.assertTrue(all(not os.get_inheritable(fd) for fd in received))
                        for fd in received:
                            os.close(fd)
                    else:
                        with self.assertRaises(ValueError):
                            broker.receive(right)

    def test_client_rejects_nonroot_server(self):
        with contextlib.ExitStack() as stack:
            a, b = socket.socketpair()
            stack.enter_context(a)
            stack.enter_context(b)
            if os.getuid() != 0:
                with self.assertRaises(SystemExit):
                    client.verify_server(a)

    def start_server(self, directory, allowed_uid=None):
        path = str(Path(directory)/'control.sock')
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        listener.bind(path)
        listener.listen()
        pid = os.fork()
        if pid == 0:
            os.setsid()
            try:
                broker.serve(listener, os.getuid() if allowed_uid is None else allowed_uid, fixture_runner)
            finally:
                os._exit(0)
        listener.close()
        self.addCleanup(self.stop_server, pid)
        return path

    @staticmethod
    def stop_server(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.waitpid(pid, 0)

    def client_command(self, path, script):
        return [sys.executable, '-c',
                'from unittest.mock import patch;from claude_cn.client import launch;'
                'import sys;'
                'p=patch("claude_cn.client.verify_server");p.start();'
                'launch([sys.argv[2]],address=sys.argv[1])', path, script]

    def test_pipes_and_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.start_server(tmp)
            script = 'import sys;print(sys.stdin.read().upper());print("stderr",file=sys.stderr);sys.exit(7)'
            result = subprocess.run(self.client_command(path, script), input='hello', text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 7)
            self.assertEqual(result.stdout, 'HELLO\n')
            self.assertEqual(result.stderr, 'stderr\n')

    def test_client_does_not_forward_credentials_or_external_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.start_server(tmp)
            script = 'import os;print(os.getenv("ANTHROPIC_AUTH_TOKEN"),os.getenv("HTTPS_PROXY"),os.getenv("LD_PRELOAD"))'
            with patch.dict(os.environ, {'ANTHROPIC_AUTH_TOKEN': 'secret-test-only', 'HTTPS_PROXY': 'http://untrusted'}):
                result = subprocess.run(self.client_command(path, script), capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), 'None None None')

    def test_signal_forwarding(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.start_server(tmp)
            marker = Path(tmp)/'ready'
            script = f'import signal,time;signal.signal(signal.SIGTERM,lambda *_:exit(42));open({str(marker)!r},"w").close();time.sleep(60)'
            process = subprocess.Popen(self.client_command(path, script), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic()+5
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(marker.exists())
                process.send_signal(signal.SIGTERM)
                self.assertEqual(process.wait(timeout=5), 42)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()

    def test_peer_uid_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.start_server(tmp, allowed_uid=os.getuid()+1)
            result = subprocess.run(self.client_command(path, 'print("must-not-run")'), capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(b'must-not-run', result.stdout)

    def test_terminal_controlling_tty_and_restoration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.start_server(tmp)
            outer_master, outer_slave = os.openpty()
            original = termios.tcgetattr(outer_slave)
            script = 'import os;f=os.open("/dev/tty",os.O_RDWR);print("TTY_OK",os.isatty(0),flush=True);os.close(f)'
            process = subprocess.Popen(self.client_command(path, script), stdin=outer_slave, stdout=outer_slave, stderr=outer_slave)
            try:
                process.wait(timeout=10)
                self.assertEqual(process.returncode, 0)
                self.assertEqual(termios.tcgetattr(outer_slave), original)
                self.assertTrue(select.select([outer_master], [], [], 2)[0])
                self.assertIn(b'TTY_OK True', os.read(outer_master, 65536))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                os.close(outer_master)
                os.close(outer_slave)

    def test_disconnect_kills_launched_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.start_server(tmp)
            pid_path = Path(tmp)/'child.pid'
            script = f'import os,time;open({str(pid_path)!r},"w").write(str(os.getpid()));time.sleep(60)'
            process = subprocess.Popen(self.client_command(path, script), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic()+5
                while not pid_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(pid_path.exists())
                child = int(pid_path.read_text())
                process.kill()
                process.wait()
                deadline = time.monotonic()+5
                while Path(f'/proc/{child}').exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(Path(f'/proc/{child}').exists())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()

    def test_missing_service_never_falls_back_to_sudo(self):
        with patch('claude_cn.client.os.execv') as execute:
            with self.assertRaises(SystemExit):
                client.launch([], address='/nonexistent/claude-cn.sock')
            execute.assert_not_called()
