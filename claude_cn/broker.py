"""Root launch broker. Kernel peer credentials, bounded packets, stdio-only FDs."""
import array
import json
import os
from pathlib import Path
import select
import signal
import socket
import stat
import struct
import time

from .entry import allowed_env, INSTALL_CONFIG, enter

SOCKET = '/run/claude-cn-launch/control.sock'
MAX_PACKET = 65536
MAX_SESSIONS = 32
SIGNALS = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT,
           signal.SIGWINCH, signal.SIGCONT, signal.SIGTSTP}


def validate(request):
    if not isinstance(request, dict) or set(request) != {'cwd', 'env', 'args', 'tty'}:
        raise ValueError('invalid launch request')
    if not isinstance(request['cwd'], str) or not request['cwd'].startswith('/') or '\x00' in request['cwd']:
        raise ValueError('invalid working directory')
    env = request['env']
    if not isinstance(env, dict) or any(not isinstance(k, str) or not isinstance(v, str) or
            '\x00' in k or '\x00' in v or '=' in k or
            not allowed_env(k, 'development') for k, v in env.items()):
        raise ValueError('invalid environment')
    if not isinstance(request['args'], list) or any(not isinstance(a, str) or '\x00' in a for a in request['args']):
        raise ValueError('invalid arguments')
    if type(request['tty']) is not bool:
        raise ValueError('invalid terminal flag')


def receive(conn):
    fds = []
    try:
        raw, ancillary, flags, _ = conn.recvmsg(MAX_PACKET, socket.CMSG_SPACE(16 * array.array('i').itemsize),
                                               socket.MSG_CMSG_CLOEXEC)
        for level, kind, data in ancillary:
            if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                raise ValueError('unexpected ancillary data')
            values = array.array('i')
            values.frombytes(data[:len(data) - len(data) % values.itemsize])
            fds.extend(values)
        if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or len(fds) != 3:
            raise ValueError('expected three stdio descriptors')
        # Inherited host sockets must never be handed through the network boundary.
        if any(stat.S_ISSOCK(os.fstat(fd).st_mode) for fd in fds):
            raise ValueError('socket stdio is not permitted; use pipes')
        request = json.loads(raw)
        validate(request)
        return request, fds
    except BaseException:
        for fd in fds:
            os.close(fd)
        raise


def terminate(pid):
    for sig in (signal.SIGHUP, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            pass
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass
        if sig == signal.SIGHUP:
            time.sleep(0.1)
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


def session(conn, uid, runner=enter):
    """Each session has a separate supervisor; no shared root worker threads."""
    pid = None
    fds = []
    try:
        conn.settimeout(10)
        request, fds = receive(conn)
        pid = os.fork()
        if pid == 0:
            try:
                conn.close()
                runner(request, request['args'], uid, fds, request['tty'])
            except BaseException:
                # Never print arbitrary user input, credentials or root exceptions.
                os._exit(125)
            os._exit(0)
        for fd in fds:
            os.close(fd)
        fds = []
        conn.settimeout(None)
        while True:
            done, status = os.waitpid(pid, os.WNOHANG | os.WUNTRACED | os.WCONTINUED)
            if done and os.WIFSTOPPED(status):
                conn.send(json.dumps({'stopped': os.WSTOPSIG(status)}).encode())
                continue
            if done and os.WIFCONTINUED(status):
                continue
            if done:
                pid = None
                code = os.waitstatus_to_exitcode(status)
                conn.send(json.dumps({'exit': code if code >= 0 else 128 - code}).encode())
                return
            if select.select([conn], [], [], 0.1)[0]:
                raw = conn.recv(128)
                if not raw:
                    return
                msg = json.loads(raw)
                if not isinstance(msg, dict) or set(msg) != {'signal'} or type(msg['signal']) is not int or msg['signal'] not in SIGNALS:
                    raise ValueError('invalid signal')
                try:
                    os.killpg(pid, msg['signal'])
                except ProcessLookupError:
                    pass
    except (ValueError, OSError, TypeError):
        try:
            conn.send(b'{"error":"launch rejected or failed"}')
        except OSError:
            pass
    finally:
        for fd in fds:
            os.close(fd)
        if pid is not None:
            terminate(pid)
        conn.close()


def serve(listener, uid, runner=enter):
    children = set()

    def collect():
        for pid in list(children):
            if os.waitpid(pid, os.WNOHANG)[0]:
                children.remove(pid)

    # Sessions that end while the broker waits in accept() must not stay
    # counted against MAX_SESSIONS; poll briefly instead of blocking forever.
    listener.settimeout(1)
    while True:
        try:
            conn, _ = listener.accept()
        except socket.timeout:
            collect()
            continue
        collect()
        _, peer_uid, _ = struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if peer_uid != uid or len(children) >= MAX_SESSIONS:
            conn.close()
            continue
        pid = os.fork()
        if pid == 0:
            listener.close()
            try:
                session(conn, uid, runner)
            finally:
                os._exit(0)
        children.add(pid)
        conn.close()


def main():
    if os.geteuid() != 0:
        raise SystemExit('broker requires root')
    uid = json.loads(INSTALL_CONFIG.read_text())['uid']
    if type(uid) is not int or uid <= 0:
        raise SystemExit('invalid configured user')
    path = Path(SOCKET)
    path.unlink(missing_ok=True)
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener:
        listener.bind(SOCKET)
        os.chown(SOCKET, uid, -1)
        os.chmod(SOCKET, 0o600)
        listener.listen(32)
        notify = os.environ.get('NOTIFY_SOCKET')
        if notify:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
                channel.connect('\0' + notify[1:] if notify.startswith('@') else notify)
                channel.send(b'READY=1')
        serve(listener, uid)
