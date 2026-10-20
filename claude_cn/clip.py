"""Read-only clipboard bridge: sandbox shims fetch clipboard bytes over loopback."""
import argparse
import collections
import json
import logging
import os
import pwd
import re
import select
import signal
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path

LOG = logging.getLogger('claude-cn')

ADDRESS = ('127.0.0.1', 18081)
SOCKET = '/run/claude-cn-clip/clip.sock'
INSTALL_CONFIG = '/etc/claude-cn/install.json'
POLICY_CONFIG = '/etc/claude-cn/policy.json'
PROXY_USER = 'claude-cn-proxy'
XCLIP = '/usr/bin/xclip'
WLPASTE = '/usr/bin/wl-paste'
ALLOWED = re.compile(r'\A(?:image/(?:png|jpeg|jpg|gif|webp|bmp)|text/plain(?:;charset=utf-8)?|UTF8_STRING)\Z')
MAX_BYTES = 16 * 1024 * 1024
MAX_LINE = 256
TOOL_TIMEOUT = 5
RATE_LIMIT = 30
RATE_WINDOW = 60


class BridgeError(Exception):
    pass


def is_text(mime):
    return mime.startswith('text/') or mime == 'UTF8_STRING'


def request(line, connect=None):
    """Send one request line and return the payload bytes."""
    conn = (connect or (lambda: socket.create_connection(ADDRESS, timeout=5)))()
    try:
        conn.sendall(line.encode('ascii') + b'\n')
        conn.settimeout(TOOL_TIMEOUT + 5)
        with conn.makefile('rb') as stream:
            header = stream.readline(64)
            parts = header.split()
            if len(parts) == 2 and parts[0] == b'OK':
                try:
                    length = int(parts[1])
                except ValueError:
                    raise BridgeError('malformed response') from None
                data = stream.read(length)
                if len(data) != length:
                    raise BridgeError('truncated response')
                return data
            if parts and parts[0] == b'ERR':
                raise BridgeError(' '.join(part.decode('ascii', 'replace') for part in parts[1:]))
            raise BridgeError('malformed response')
    finally:
        conn.close()


def _emit(data, out):
    out.write(data)
    out.flush()
    return 0


def _bridge(line, connect, out):
    try:
        return _emit(request(line, connect), out)
    except (BridgeError, OSError, ValueError) as exc:
        print(f'claude-cn clip: {exc}', file=sys.stderr)
        return 1


def xclip_main(argv, connect=None, out=None):
    """Emulate the xclip read invocations Claude Code performs."""
    out = sys.stdout.buffer if out is None else out
    selection, mime, output = 'clipboard', None, False
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ('-selection', '-sel') and index + 1 < len(argv):
            selection = argv[index + 1]
            index += 2
        elif arg == '-t' and index + 1 < len(argv):
            mime = argv[index + 1]
            index += 2
        elif arg == '-o':
            output = True
            index += 1
        elif arg in ('-version', '-help'):
            return _emit(b'xclip 0.13 (claude-cn bridge)\n', out)
        else:
            return 1
    if not output or selection != 'clipboard':
        # The write direction and the primary selection stay outside the bridge.
        return 1
    if mime == 'TARGETS':
        return _bridge('TARGETS', connect, out)
    return _bridge(f'GET {mime}' if mime else 'GET text/plain', connect, out)


def wl_paste_main(argv, connect=None, out=None):
    """Emulate the wl-paste read invocations Claude Code performs."""
    out = sys.stdout.buffer if out is None else out
    mime, listing = None, False
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ('-l', '--list-types'):
            listing = True
            index += 1
        elif arg in ('-t', '--type') and index + 1 < len(argv):
            mime = argv[index + 1]
            index += 2
        elif arg in ('-n', '--no-newline'):
            index += 1
        elif arg in ('-v', '--version'):
            return _emit(b'wl-paste (claude-cn bridge)\n', out)
        else:
            # Includes --primary; the bridge never reads that selection.
            return 1
    if listing:
        return _bridge('TARGETS', connect, out)
    return _bridge(f'GET {mime}' if mime else 'GET text/plain', connect, out)


def discover_env(proc='/proc', uid=None):
    """Find the graphical session environment of the authorized user."""
    fallback = None
    try:
        names = os.listdir(proc)
    except OSError:
        return None
    for name in names:
        if not name.isdigit():
            continue
        base = Path(proc)/name
        try:
            if uid is not None and base.stat().st_uid != uid:
                continue
            raw = (base/'environ').read_bytes()
        except OSError:
            continue
        session = {}
        for item in raw.split(b'\0'):
            key, sep, value = item.partition(b'=')
            if sep:
                session[key.decode('ascii', 'replace')] = value.decode('utf-8', 'replace')
        if session.get('DISPLAY'):
            return session
        if fallback is None and session.get('WAYLAND_DISPLAY') and session.get('XDG_RUNTIME_DIR'):
            fallback = session
    return fallback


def tool_plan(mime, session, home):
    """Build (argv, env) for the real clipboard tool; mime None means TARGETS."""
    common = {'PATH': '/usr/bin:/bin', 'HOME': home}
    if session.get('DISPLAY') and os.access(XCLIP, os.X_OK):
        argv = [XCLIP, '-selection', 'clipboard', '-t', mime or 'TARGETS', '-o']
        env = {**common, 'DISPLAY': session['DISPLAY']}
        if session.get('XAUTHORITY'):
            env['XAUTHORITY'] = session['XAUTHORITY']
        return argv, env
    if session.get('WAYLAND_DISPLAY') and session.get('XDG_RUNTIME_DIR') and os.access(WLPASTE, os.X_OK):
        if mime is None:
            argv = [WLPASTE, '-l']
        elif is_text(mime):
            # Explicit type only: a bare wl-paste auto-selects whatever the
            # offer lists and could return content outside the whitelist.
            argv = [WLPASTE, '--no-newline', '--type', mime]
        else:
            argv = [WLPASTE, '--type', mime]
        return argv, {**common, 'WAYLAND_DISPLAY': session['WAYLAND_DISPLAY'],
                      'XDG_RUNTIME_DIR': session['XDG_RUNTIME_DIR']}
    raise BridgeError('no clipboard tool for this session')


def _wait_bounded(pid, deadline):
    """Poll for the child's exit status; None when still alive at the deadline."""
    while True:
        try:
            waited, status = os.waitpid(pid, os.WNOHANG)
        except OSError:
            return 0
        if waited:
            return status
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(remaining, 0.05))


def _terminate(pid):
    """Best-effort SIGKILL then a bounded reap; a stuck child is abandoned."""
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    if _wait_bounded(pid, time.monotonic() + 2) is None:
        LOG.error('clipboard tool pid %d survived SIGKILL; abandoning it', pid)


def run_tool(argv, env, uid, gid, timeout=TOOL_TIMEOUT):
    """Exec the tool as the user and capture stdout, bounded by size and time."""
    read_fd, write_fd = os.pipe()
    deadline = time.monotonic() + timeout
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        os.dup2(write_fd, 1)
        os.dup2(os.open(os.devnull, os.O_WRONLY), 2)
        try:
            if os.geteuid() == 0:
                os.setgroups([])
                os.setgid(gid)
                os.setuid(uid)
            elif os.getgid() != gid or os.getuid() != uid:
                os._exit(126)
            os.execve(argv[0], argv, env)
        except OSError:
            pass
        os._exit(127)
    os.close(write_fd)
    chunks, total, status = [], 0, None
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BridgeError('clipboard tool timed out')
            if not select.select([read_fd], [], [], remaining)[0]:
                continue
            chunk = os.read(read_fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BYTES:
                raise BridgeError('clipboard data exceeds size limit')
            chunks.append(chunk)
        # Reading and reaping share one deadline: EOF alone never blocks a slot.
        status = _wait_bounded(pid, deadline)
        if status is None:
            # Still alive: raise inside the guarded block so the child is killed.
            raise BridgeError('clipboard tool timed out')
    except BaseException:
        _terminate(pid)
        raise
    finally:
        os.close(read_fd)
    if status:
        raise BridgeError('clipboard tool failed')
    return b''.join(chunks)


class Context:
    def __init__(self, uid, gid, home, runner=run_tool, discover=discover_env, allow_text=False):
        self.uid, self.gid, self.home = uid, gid, home
        self.runner = runner
        self.discover = discover
        self.allow_text = allow_text
        self._session, self._found = None, 0.0
        self.history = collections.deque()

    def session_env(self):
        if time.monotonic() - self._found > 5:
            self._session = self.discover(uid=self.uid)
            self._found = time.monotonic()
        return self._session

    def allow(self):
        now = time.monotonic()
        while self.history and now - self.history[0] > RATE_WINDOW:
            self.history.popleft()
        if len(self.history) >= RATE_LIMIT:
            return False
        self.history.append(now)
        return True

    def retry_text(self, mime, argv, env, exc):
        """Fall back to the alternate plain-text target when the first is absent."""
        if str(exc) != 'clipboard tool failed':
            raise exc
        if mime == 'text/plain' and argv[0] == XCLIP:
            retry = [XCLIP, '-selection', 'clipboard', '-t', 'UTF8_STRING', '-o']
        elif mime == 'text/plain' and argv[0] == WLPASTE:
            retry = [WLPASTE, '--no-newline', '--type', 'text/plain;charset=utf-8']
        else:
            raise exc
        LOG.info('retrying clipboard text: %s', ' '.join(retry))
        return self.runner(retry, env, self.uid, self.gid)

    def handle(self, line):
        """Return the raw response bytes for one request line."""
        if line == 'TARGETS':
            mime = None
        else:
            # Split command from type first; the mime whitelist is the only gate.
            match = re.fullmatch(r'GET (\S+)', line)
            if not match:
                LOG.warning('malformed request %r', line)
                return b'ERR malformed request\n'
            mime = match.group(1)
            if not ALLOWED.match(mime):
                LOG.warning('refused mime %r', mime)
                return b'ERR mime not allowed\n'
            if is_text(mime) and not self.allow_text:
                LOG.warning('text clipboard disabled by policy')
                return b'ERR text disabled\n'
        if not self.allow():
            LOG.warning('rate limited')
            return b'ERR rate limited\n'
        session = self.session_env()
        if not session:
            return b'ERR no graphical session\n'
        try:
            argv, env = tool_plan(mime, session, self.home)
            try:
                data = self.runner(argv, env, self.uid, self.gid)
            except BridgeError as exc:
                data = self.retry_text(mime, argv, env, exc)
        except BridgeError as exc:
            LOG.warning('clip %s: %s', line, exc)
            return f'ERR {exc}\n'.encode('ascii', 'replace')
        LOG.info('clip %s -> %d bytes', line, len(data))
        return f'OK {len(data)}\n'.encode() + data


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            self.connection.settimeout(TOOL_TIMEOUT + 5)
            line = self.rfile.readline(MAX_LINE + 2)
        except OSError:
            return
        if not line.endswith(b'\n') or len(line) > MAX_LINE + 1:
            return
        try:
            text = line.decode('ascii').strip()
        except UnicodeDecodeError:
            return
        self.wfile.write(self.server.context.handle(text))


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, path, context):
        self.context = context
        self.slots = threading.BoundedSemaphore(8)
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


def grant_relay_access(path):
    """Hand the socket to the relay user; failure degrades, never crashes."""
    try:
        owner = pwd.getpwnam(PROXY_USER)
        os.chown(path, owner.pw_uid, owner.pw_gid)
    except (KeyError, OSError) as exc:
        LOG.warning('cannot hand the socket to %s (%s); the namespace relay cannot connect',
                    PROXY_USER, exc)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='claude-cn-clip')
    parser.add_argument('--socket', default=SOCKET)
    parser.add_argument('--install-config', default=INSTALL_CONFIG)
    parser.add_argument('--config', default=POLICY_CONFIG)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    data = json.loads(Path(args.install_config).read_text())
    uid = data.get('uid')
    if type(uid) is not int or uid <= 0:
        raise SystemExit('claude-cn: invalid install configuration')
    try:
        policy = json.loads(Path(args.config).read_text())
    except (OSError, ValueError):
        policy = {}
    allow_text = policy.get('allow_clipboard_text') is True
    account = pwd.getpwuid(uid)
    context = Context(uid, account.pw_gid, account.pw_dir, allow_text=allow_text)
    try:
        os.unlink(args.socket)
    except FileNotFoundError:
        pass
    os.umask(0o077)
    with Server(args.socket, context) as server:
        grant_relay_access(args.socket)
        LOG.info('claude-cn clipboard bridge ready (read-only, text %s)',
                 'enabled' if allow_text else 'disabled')
        server.serve_forever()
