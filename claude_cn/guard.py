"""Inherited seccomp rules: block local socket relays and host-process injection."""
import ctypes
import ctypes.util
import errno
import os
import socket


class Comparison(ctypes.Structure):
    _fields_ = [('arg', ctypes.c_uint), ('op', ctypes.c_uint),
                ('datum_a', ctypes.c_uint64), ('datum_b', ctypes.c_uint64)]


def install(mode='strict'):
    if mode not in ('strict', 'development'):
        raise ValueError('invalid runtime mode')
    if mode == 'development':
        # Network namespace/nftables remain enforced by entry. Host IPC and
        # debugging are intentionally available; no anti-bypass guarantee.
        return
    library = ctypes.util.find_library('seccomp')
    if not library:
        raise RuntimeError('libseccomp is required; refusing to run without it')
    lib = ctypes.CDLL(library, use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int,
                                         ctypes.c_uint, ctypes.POINTER(Comparison)]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0):  # PR_SET_NO_NEW_PRIVS
        raise OSError(ctypes.get_errno(), 'PR_SET_NO_NEW_PRIVS')
    context = lib.seccomp_init(0x7fff0000)  # SCMP_ACT_ALLOW
    if not context:
        raise RuntimeError('seccomp_init failed')

    def deny(name, conditions=()):
        number = lib.seccomp_syscall_resolve_name(name.encode())
        if number < 0:
            return
        array = (Comparison * len(conditions))(*conditions)
        result = lib.seccomp_rule_add_array(context, 0x00050000 | errno.EPERM,
                                           number, len(conditions), array)
        if result < 0:
            raise OSError(-result, 'seccomp rule ' + name)

    try:
        # AF_UNIX socketpair remains usable for in-process IPC, but socket() cannot
        # connect to Docker, D-Bus, SSH agents, or host proxy filesystem sockets.
        for family in range(0, 64):
            if family not in (socket.AF_INET, socket.AF_INET6):
                deny('socket', [Comparison(0, 4, family, 0)])  # SCMP_CMP_EQ
        for name in ('io_uring_setup', 'io_uring_enter', 'io_uring_register',
                     'ptrace', 'process_vm_writev', 'process_vm_readv', 'pidfd_getfd',
                     'setns', 'unshare', 'bpf', 'mount', 'umount2'):
            deny(name)
        # Do not allow an alternate syscall ABI to bypass these native rules.
        # libseccomp's default bad-architecture action kills foreign-ABI calls.
        result = lib.seccomp_load(context)
        if result < 0:
            raise OSError(-result, 'seccomp_load')
    finally:
        lib.seccomp_release(context)


def close_extra_fds():
    for name in os.listdir('/proc/self/fd'):
        fd = int(name)
        if fd > 2:
            try:
                os.close(fd)
            except OSError:
                pass
