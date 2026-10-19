#!/usr/bin/python3
"""Build an auditable pure-Python Debian package without root or extra modules."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def build(output, private=False, version=None):
    base = (ROOT/'VERSION').read_text().strip()
    version = version or (base + '+local1' if private else base)
    if not re.fullmatch(r'[0-9][a-zA-Z0-9.+~:-]*', version):
        raise ValueError('invalid Debian version')
    country = ROOT/'data/cn-networks.json'
    if not country.exists():
        raise FileNotFoundError('先运行 python3 -m claude_cn.update --output data/cn-networks.json')
    sys.path.insert(0, str(ROOT))
    from claude_cn.policy import Policy
    Policy.load(ROOT/'config/policy.json', country)
    zhipu = ROOT/('config/zhipu.local.json' if private else 'config/zhipu.example.json')
    settings = json.loads(zhipu.read_text())
    if not isinstance(settings.get('env'), dict):
        raise ValueError('invalid Zhipu settings')
    if not private and settings['env'].get('ANTHROPIC_AUTH_TOKEN'):
        raise ValueError('public template unexpectedly contains API credentials')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    artifact = output/f'claude-cn_{version}_all.deb'
    epoch = int(os.environ.get('SOURCE_DATE_EPOCH', '1792368000'))
    with tempfile.TemporaryDirectory(prefix='claude-cn-deb-') as tmp:
        stage = Path(tmp)/'root'
        stage.mkdir(mode=0o755)

        def copy(source, dest, mode=0o644):
            target = stage/dest.lstrip('/')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            target.chmod(mode)

        for source in (ROOT/'claude_cn').glob('*.py'):
            copy(source, '/usr/lib/claude-cn/claude_cn/' + source.name)
        for name in ('enter', 'payload.py', 'service.py'):
            copy(ROOT/'bin'/name, '/usr/lib/claude-cn/' + name, 0o755)
        copy(ROOT/'bin/claude-cn', '/usr/bin/claude-cn', 0o755)
        copy(ROOT/'bin/claude-cn-admin', '/usr/sbin/claude-cn-admin', 0o755)
        for source in (ROOT/'systemd').glob('claude-cn-*'):
            copy(source, '/usr/lib/systemd/system/' + source.name)
        for name in ('policy', 'download', 'runtime'):
            copy(ROOT/f'config/{name}.json', f'/etc/claude-cn/{name}.json')
        copy(zhipu, '/etc/claude-cn/provider.json', 0o600)
        copy(ROOT/'config/zhipu.example.json', '/usr/share/claude-cn/zhipu.example.json')
        copy(country, '/usr/share/claude-cn/cn-networks.json')
        copy(ROOT/'README.md', '/usr/share/doc/claude-cn/README.md')
        copy(ROOT/'LICENSE', '/usr/share/doc/claude-cn/copyright')
        copy(ROOT/'THIRD_PARTY.md', '/usr/share/doc/claude-cn/THIRD_PARTY.md')
        copy(ROOT/'third_party/china-operator-ip.LICENSE', '/usr/share/doc/claude-cn/china-operator-ip.LICENSE')
        # Ship the isolated integration lab for verification on the installed host.
        copy(ROOT/'tests/integration_network.py', '/usr/share/claude-cn/integration_network.py')
        control = stage/'DEBIAN'
        control.mkdir(mode=0o755)
        installed_size = sum(p.stat().st_size for p in stage.rglob('*') if p.is_file())
        metadata = (ROOT/'packaging/control').read_text().replace('@VERSION@', version)
        (control/'control').write_text(metadata + f'Installed-Size: {(installed_size+1023)//1024}\n')
        (control/'conffiles').write_text(''.join(f'/etc/claude-cn/{name}.json\n'
                                                for name in ('policy', 'download', 'runtime', 'provider')))
        for name in ('postinst', 'prerm', 'postrm'):
            copy(ROOT/'packaging'/name, '/DEBIAN/' + name, 0o755)
        checksums = []
        for p in sorted(stage.rglob('*')):
            if p.is_file() and 'DEBIAN' not in p.relative_to(stage).parts:
                checksums.append(hashlib.md5(p.read_bytes()).hexdigest() + '  ' + str(p.relative_to(stage)))
        (control/'md5sums').write_text('\n'.join(checksums) + '\n')
        # Public and private packages differ only in the selected seed, version,
        # and resulting checksums. No user home or real Claude binary is copied.
        for p in stage.rglob('*'):
            if p.is_dir():
                p.chmod(0o755)
            elif p.parent == control and p.name not in ('postinst', 'prerm', 'postrm'):
                p.chmod(0o644)
            os.utime(p, (epoch, epoch), follow_symlinks=False)
        os.utime(stage, (epoch, epoch))
        subprocess.run(['dpkg-deb', '--root-owner-group', '--threads-max=1', '-Zxz', '--build', str(stage), str(artifact)],
                       check=True, env={**os.environ, 'SOURCE_DATE_EPOCH': str(epoch)})
    artifact.chmod(0o600 if private else 0o644)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    print(f'{digest}  {artifact.name}')
    return artifact


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT/'dist')
    parser.add_argument('--private', action='store_true', help='Include explicitly authorized local Zhipu configuration')
    parser.add_argument('--version')
    args = parser.parse_args()
    build(args.output, args.private, args.version)


if __name__ == '__main__':
    main()
