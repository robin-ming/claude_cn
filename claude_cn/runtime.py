"""Standalone settings and upgrade-safe Claude executable discovery."""
import json
import os
from pathlib import Path
import shutil
import tempfile

DEFAULT_RUNTIME = {'claude_path': 'auto', 'settings_path': '~/.config/claude-cn/settings.json',
                   'mode': 'strict'}


def expand_path(value, home):
    if value == '~':
        return Path(home)
    if value.startswith('~/'):
        return Path(home)/value[2:]
    path = Path(value)
    if not path.is_absolute():
        raise ValueError('配置路径必须为绝对路径或以 ~/ 开头')
    return path


def read_runtime(path='/etc/claude-cn/runtime.json'):
    data = dict(DEFAULT_RUNTIME)
    try:
        supplied = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return data
    if not isinstance(supplied, dict) or set(supplied) - set(data):
        raise ValueError('runtime.json 仅支持 claude_path、settings_path 和 mode')
    data.update(supplied)
    if any(not isinstance(v, str) or not v for v in data.values()):
        raise ValueError('运行路径配置不能为空')
    if data['mode'] not in ('strict', 'development'):
        raise ValueError('mode 必须为 strict 或 development')
    return data


def find_claude(hint='auto', *, home=None, path=None):
    home = Path(home or Path.home())
    path = os.environ.get('PATH', '') if path is None else path
    if hint != 'auto':
        candidates = [str(expand_path(hint, home))]
    else:
        found = shutil.which('claude', path=path)
        candidates = [p for p in [found, str(home/'.local/bin/claude'),
                                  str(home/'.npm-global/bin/claude'), '/usr/local/bin/claude',
                                  '/usr/bin/claude'] if p]
    for candidate in candidates:
        p = Path(candidate).absolute()
        if p.is_file() and os.access(p, os.X_OK):
            if p.resolve() == Path('/usr/bin/claude-cn').resolve():
                continue
            # Return stable entry, never the version-specific symlink target.
            return str(p)
    raise FileNotFoundError('找不到 Claude Code。请安装 CLI，或在 /etc/claude-cn/runtime.json 设置 claude_path。')


def write_settings(data, target, *, force=False):
    if not isinstance(data, dict) or not isinstance(data.get('env', {}), dict):
        raise ValueError('供应商配置必须为包含 env 对象的 JSON')
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.exists() and not force:
        return False
    fd, tmp = tempfile.mkstemp(prefix='.settings-', dir=target.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        if force:
            os.replace(tmp, target)
        else:
            # Atomic create without overwriting a concurrently created config.
            os.link(tmp, target)
        return True
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
