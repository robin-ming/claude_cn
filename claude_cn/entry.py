"""Root-only entry point. User-controlled code runs only after dropping root."""
import ctypes
import fcntl
import termios
import os
from pathlib import Path
import pwd
import subprocess

from .guard import close_extra_fds, install as install_guard
from .runtime import expand_path, read_runtime

ROOT = Path('/usr/lib/claude-cn')
INSTALL_CONFIG = Path('/etc/claude-cn/install.json')
PROXY = 'http://127.0.0.1:18080'
SANDBOX_BIN = str(ROOT / 'sandbox-bin')
ENV_NAMES = {'PATH', 'TERM', 'COLORTERM', 'LANG', 'LANGUAGE', 'TZ', 'EDITOR',
             'VISUAL', 'NO_COLOR', 'FORCE_COLOR', 'TERM_PROGRAM', 'TERM_PROGRAM_VERSION'}


def fail(message):
    raise SystemExit('claude-cn: ' + message)


def enter(request, args, uid, stdio, tty=False):
    """Only called by the authenticated broker's disposable child."""
    if os.geteuid() != 0 or uid == 0:
        fail('invalid broker identity')
    cwd = request['cwd']
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        fail('working directory must be absolute')
    supplied = request['env']
    if any(not isinstance(k, str) or not isinstance(v, str) or '\x00' in v or
           (k not in ENV_NAMES and not k.startswith('LC_')) for k, v in supplied.items()):
        fail('unsupported environment variable')
    for unit in ('claude-cn-netns.service', 'claude-cn-proxy.service', 'claude-cn-relay.service'):
        if subprocess.run(['/usr/bin/systemctl', 'is-active', '--quiet', unit],
                          env={'PATH': '/usr/bin:/bin'}, close_fds=True).returncode:
            fail(unit + ' is not active; refusing unrestricted fallback')
    account = pwd.getpwuid(uid)
    runtime = read_runtime()
    for command in (["/usr/sbin/nft", 'list', 'table', 'inet', 'claude_cn'],
                    ['/usr/sbin/ip', 'netns', 'exec', 'claude-cn', '/usr/sbin/nft',
                     'list', 'table', 'inet', 'claude_cn']):
        if subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          env={'PATH': '/usr/sbin:/usr/bin:/bin'}, close_fds=True).returncode:
            fail('network boundary is missing; refusing to start')
    ns = '/run/netns/claude-cn'
    fd = os.open(ns, os.O_RDONLY | os.O_CLOEXEC)
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        if os.stat('/proc/self/ns/net').st_ino == os.fstat(fd).st_ino:
            fail('unexpected network namespace')
        if libc.setns(fd, 0x40000000):  # CLONE_NEWNET
            raise OSError(ctypes.get_errno(), 'setns')
    finally:
        os.close(fd)
    # Only this process tree gets the real permitted DNS servers. The host's
    # systemd-resolved stub at 127.0.0.53 is not available in a network namespace.
    if libc.unshare(0x00020000):  # CLONE_NEWNS
        raise OSError(ctypes.get_errno(), 'unshare mount namespace')
    libc.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
                           ctypes.c_ulong, ctypes.c_void_p]
    if libc.mount(None, b'/', None, (1 << 18) | (1 << 14), None):  # MS_PRIVATE | MS_REC
        raise OSError(ctypes.get_errno(), 'make mounts private')
    target = os.fsencode(os.path.realpath('/etc/resolv.conf'))
    if libc.mount(b'/etc/claude-cn/resolv.conf', target, None, 4096, None):  # MS_BIND
        raise OSError(ctypes.get_errno(), 'bind namespace resolv.conf')
    if libc.mount(None, target, None, 4096 | 32 | 1, None):  # MS_BIND|MS_REMOUNT|MS_RDONLY
        raise OSError(ctypes.get_errno(), 'protect namespace resolv.conf')
    # Discard caller credentials/proxy/loader variables; settings.json remains the
    # source of GLM credentials. Nothing supplied by the caller is executed as root.
    env = dict(supplied)
    env.update(HOME=account.pw_dir, USER=account.pw_name, LOGNAME=account.pw_name,
               CLAUDE_CN_ACTIVE='1', CLAUDE_CN_EXPECTED_NS=str(os.stat('/proc/self/ns/net').st_ino),
               CLAUDE_CN_SETTINGS=str(expand_path(runtime['settings_path'], account.pw_dir)),
               CLAUDE_CN_EXECUTABLE=runtime['claude_path'])
    env.setdefault('PATH', '/usr/local/bin:/usr/bin:/bin')
    # Clipboard shims must shadow any host xclip/wl-paste inside the sandbox.
    env['PATH'] = SANDBOX_BIN + ':' + env['PATH']
    env.setdefault('TERM', 'xterm-256color')
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
        env[key] = PROXY
    env.update(NO_PROXY='', no_proxy='')
    os.initgroups(account.pw_name, account.pw_gid)
    os.setgid(account.pw_gid)
    os.setuid(uid)
    if os.geteuid() != uid or os.getuid() != uid or uid == 0:
        fail('failed to drop privileges')
    os.setsid()
    for target, source in enumerate(stdio):
        os.dup2(source, target, inheritable=True)
    if tty:
        if not os.isatty(0):
            fail('expected terminal')
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.chdir(cwd)
    close_extra_fds()
    install_guard()
    os.execve('/usr/bin/python3', ['/usr/bin/python3', '-I', str(ROOT / 'payload.py'),
                                  *args], env)
