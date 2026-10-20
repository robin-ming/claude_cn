"""Unprivileged broker client; preserve pipes or relay an interactive PTY."""
import array
import errno
import fcntl
import json
import os
import select
import signal
import socket
import sys
import termios
import tty

from .broker import MAX_PACKET, SIGNALS, SOCKET
from .entry import ENV_NAMES


def write_all(fd, data):
    while data:
        count = os.write(fd, data)
        data = data[count:]


def verify_server(conn):
    import struct
    if struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1] != 0:
        raise SystemExit('claude-cn: 启动服务身份校验失败')


def launch(args, address=SOCKET):
    request = {'cwd': os.getcwd(), 'env': {k: v for k, v in os.environ.items()
               if k in ENV_NAMES or k.startswith('LC_')}, 'args': args,
               'tty': all(os.isatty(fd) for fd in (0, 1, 2))}
    raw = json.dumps(request, ensure_ascii=True).encode()
    if len(raw) > MAX_PACKET:
        raise SystemExit('claude-cn: 启动参数过长')
    master = slave = None
    original = None
    handlers = {}
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as conn:
        try:
            conn.connect(address)
        except OSError:
            raise SystemExit('claude-cn: 启动服务不可用，请运行 claude-cn doctor 检查；不会绕过网络限制启动。') from None
        # The daemon must be root; do not hand descriptors to an impostor endpoint.
        verify_server(conn)
        try:
            if request['tty']:
                master, slave = os.openpty()
                fcntl.ioctl(master, termios.TIOCSWINSZ, fcntl.ioctl(0, termios.TIOCGWINSZ, bytes(8)))
                descriptors = [slave] * 3
            else:
                descriptors = [0, 1, 2]
            conn.sendmsg([raw], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', descriptors))])
            if slave is not None:
                os.close(slave)
                slave = None
            def forward(sig, frame):
                if sig == signal.SIGWINCH and master is not None:
                    fcntl.ioctl(master, termios.TIOCSWINSZ, fcntl.ioctl(0, termios.TIOCGWINSZ, bytes(8)))
                try:
                    conn.send(json.dumps({'signal': sig}).encode())
                except OSError:
                    pass
            for sig in SIGNALS:
                handlers[sig] = signal.signal(sig, forward)
            if master is not None:
                original = termios.tcgetattr(0)
                tty.setraw(0)
            reads = [conn] if master is None else [conn, master, 0]
            while True:
                ready, _, _ = select.select(reads, [], [])
                # Drain terminal data before accepting the exit message.
                if master is not None and master in ready:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError as exc:
                        if exc.errno != errno.EIO:
                            raise
                        chunk = b''
                    if chunk:
                        write_all(1, chunk)
                    else:
                        reads.remove(master)
                if 0 in ready:
                    chunk = os.read(0, 65536)
                    if chunk:
                        try:
                            write_all(master, chunk)
                        except OSError as exc:
                            if exc.errno != errno.EIO:
                                raise
                    else:
                        reads.remove(0)
                if conn in ready:
                    msg = conn.recv(MAX_PACKET)
                    if not msg:
                        raise SystemExit('claude-cn: 启动服务连接断开')
                    try:
                        response = json.loads(msg)
                    except ValueError:
                        raise SystemExit('claude-cn: 启动服务响应无法解析') from None
                    if not isinstance(response, dict):
                        raise SystemExit('claude-cn: 启动服务响应异常')
                    if 'error' in response:
                        raise SystemExit('claude-cn: 启动服务拒绝请求或启动失败')
                    if master is not None:
                        while select.select([master], [], [], 0)[0]:
                            try:
                                chunk = os.read(master, 65536)
                            except OSError as exc:
                                if exc.errno != errno.EIO:
                                    raise
                                break
                            if not chunk:
                                break
                            write_all(1, chunk)
                    if 'stopped' in response:
                        if original is not None:
                            termios.tcsetattr(0, termios.TCSADRAIN, original)
                        os.kill(os.getpid(), signal.SIGSTOP)
                        if original is not None:
                            tty.setraw(0)
                        forward(signal.SIGCONT, None)
                        continue
                    code = response.get('exit')
                    if type(code) is not int:
                        raise SystemExit('claude-cn: 启动服务响应异常')
                    if code == 125:
                        print('claude-cn: 隔离启动失败，请检查网络服务、配置及 Claude 路径。', file=sys.stderr)
                    raise SystemExit(code)
        finally:
            if original is not None:
                termios.tcsetattr(0, termios.TCSADRAIN, original)
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
            for fd in (slave, master):
                if fd is not None:
                    os.close(fd)
