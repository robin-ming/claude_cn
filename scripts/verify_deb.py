#!/usr/bin/python3
"""Non-root package validation; never prints credentials or changes the host."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT/'VERSION').read_text().strip()


def archive(path, option):
    raw = subprocess.check_output(['dpkg-deb', option, str(path)])
    return tarfile.open(fileobj=io.BytesIO(raw), mode='r:')


def verify(path, private, token):
    with archive(path, '--fsys-tarfile') as tar:
        files = {p.name.removeprefix('./'): p for p in tar.getmembers() if p.isfile()}
        required = ['usr/bin/claude-cn', 'usr/sbin/claude-cn-admin', 'usr/lib/systemd/system/claude-cn-launch.service',
                    'usr/lib/systemd/system/claude-cn-clip.service', 'usr/lib/systemd/system/claude-cn-clip-relay.service',
                    'usr/lib/claude-cn/payload.py', 'usr/lib/claude-cn/sandbox-bin/xclip', 'usr/lib/claude-cn/sandbox-bin/wl-paste',
                    'etc/claude-cn/runtime.json',
                    'etc/claude-cn/provider.json', 'usr/share/claude-cn/cn-networks.json']
        assert all(p in files for p in required), 'missing runtime files'
        assert 'usr/lib/claude-cn/enter' not in files, 'obsolete sudo helper shipped'
        assert files['etc/claude-cn/provider.json'].mode == 0o600, 'seed mode must be 0600'
        assert all(p.uid == 0 and p.gid == 0 for p in files.values()), 'package must install as root owner'
        assert not any('claude-glm' in p or '.local.json' in p for p in files), 'old wrapper/private source leaked'
        assert not any(p.startswith('usr/local/') for p in files), 'Debian files must not use /usr/local'
        provider = json.loads(tar.extractfile(files['etc/claude-cn/provider.json']).read())
        configured = provider['env'].get('ANTHROPIC_AUTH_TOKEN', '')
        assert configured == (token if private else ''), 'wrong credential variant'
        if token:
            occurrences = [name for name, member in files.items()
                           if token.encode() in tar.extractfile(member).read()]
            assert occurrences == (['etc/claude-cn/provider.json'] if private else []), 'unexpected credential occurrence'
        runtime = json.loads(tar.extractfile(files['etc/claude-cn/runtime.json']).read())
        assert runtime['claude_path'] == 'auto'
    with archive(path, '--ctrl-tarfile') as tar:
        members = {p.name.removeprefix('./'): p for p in tar.getmembers()}
        conffiles = tar.extractfile(members['conffiles']).read().decode().splitlines()
        assert '/etc/claude-cn/provider.json' in conffiles
        assert '/etc/claude-cn/runtime.json' in conffiles
        for name in ('postinst', 'prerm', 'postrm'):
            assert members[name].mode & 0o111, 'maintainer script is not executable'
            result = subprocess.run(['/bin/sh', '-n'], input=tar.extractfile(members[name]).read(), capture_output=True)
            assert result.returncode == 0, 'invalid maintainer shell syntax'
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(['dpkg-deb', '-x', str(path), tmp], check=True)
        lib = str(Path(tmp)/'usr/lib/claude-cn')
        result = subprocess.run([sys.executable, '-I', '-c',
                                 'import sys;sys.path.insert(0,sys.argv[1]);'
                                 'from claude_cn.cli import main;sys.argv=["claude-cn","version"];main()', lib],
                                text=True, capture_output=True, check=True)
        assert result.stdout.strip() == 'claude-cn ' + VERSION
        print('PASS', path.name, 'metadata, permissions, isolated imports, credential variant, maintainer syntax')


def main():
    token = json.loads((ROOT/'config/zhipu.local.json').read_text())['env']['ANTHROPIC_AUTH_TOKEN']
    public = ROOT/f'dist/claude-cn_{VERSION}_all.deb'
    private = ROOT/f'dist/claude-cn_{VERSION}+local1_all.deb'
    verify(public, False, token)
    verify(private, True, token)
    assert private.stat().st_mode & 0o777 == 0o600
    sys.path.insert(0, str(ROOT))
    from scripts.build_deb import build
    with tempfile.TemporaryDirectory() as tmp:
        rebuilt = build(tmp)
        assert public.read_bytes() == rebuilt.read_bytes(), 'public build is not reproducible'
    print('PASS reproducible public build')
    checksums = ''.join(hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.name + '\n' for p in (public, private))
    (ROOT/'dist/SHA256SUMS').write_text(checksums)


if __name__ == '__main__':
    main()
