"""Public standalone command; all Claude arguments pass through unchanged."""
import argparse
import getpass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import urlsplit

from .runtime import expand_path, find_claude, read_runtime, write_settings

VERSION = '0.3.0'
BROKER_SOCKET = Path('/run/claude-cn-launch/control.sock')
SHARE = Path('/usr/share/claude-cn')


def template_path():
    installed = SHARE/'zhipu.example.json'
    return installed if installed.exists() else Path(__file__).resolve().parents[1]/'config/zhipu.example.json'


def settings_path():
    return expand_path(read_runtime()['settings_path'], Path.home())


def init(args):
    parser = argparse.ArgumentParser(prog='claude-cn init')
    parser.add_argument('--from', dest='source', type=Path, default=template_path())
    parser.add_argument('--force', action='store_true')
    options = parser.parse_args(args)
    data = json.loads(options.source.read_text())
    dest = settings_path()
    changed = write_settings(data, dest, force=options.force)
    print(('已创建：' if changed else '保留现有配置：') + str(dest))


def configure(args):
    parser = argparse.ArgumentParser(prog='claude-cn configure')
    parser.add_argument('--model', help='默认沿用当前模型，例如 glm-5.3[1m]')
    parser.add_argument('--base-url', help='模型供应商的 Anthropic 兼容 API 地址；默认沿用智谱配置')
    options = parser.parse_args(args)
    target = settings_path()
    data = json.loads((target if target.exists() else template_path()).read_text())
    if options.base_url:
        parsed = urlsplit(options.base_url)
        if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('供应商 API 地址必须为不含凭据的 HTTP(S) URL')
        data.setdefault('env', {})['ANTHROPIC_BASE_URL'] = options.base_url
    # Piped or scripted runs cannot answer the prompt; keep the existing key.
    token = getpass.getpass('供应商 API Key（留空保留原值）：') if sys.stdin.isatty() else ''
    if token:
        data.setdefault('env', {})['ANTHROPIC_AUTH_TOKEN'] = token
    if options.model:
        for key in ('ANTHROPIC_MODEL', 'ANTHROPIC_DEFAULT_OPUS_MODEL', 'ANTHROPIC_DEFAULT_SONNET_MODEL',
                    'ANTHROPIC_DEFAULT_HAIKU_MODEL', 'CLAUDE_CODE_SUBAGENT_MODEL'):
            data.setdefault('env', {})[key] = options.model
    write_settings(data, target, force=True)
    print('配置已保存（权限0600）：' + str(target))


def doctor():
    failed = False
    def check(label, ok, detail=''):
        nonlocal failed
        print(('PASS ' if ok else 'FAIL ') + label + (': ' + detail if detail else ''))
        failed |= not ok
    runtime = read_runtime()
    try:
        binary = find_claude(runtime['claude_path'])
        check('Claude 程序入口', True, binary)
        print('实际目标：' + str(Path(binary).resolve()))
    except FileNotFoundError as exc:
        check('Claude 程序入口', False, str(exc))
    target = expand_path(runtime['settings_path'], Path.home())
    check('独立供应商配置', target.is_file(), str(target))
    if target.is_file():
        data = json.loads(target.read_text())
        check('API Key 已填写', bool(data.get('env', {}).get('ANTHROPIC_AUTH_TOKEN')))
        check('配置权限0600', target.stat().st_mode & 0o777 == 0o600)
    install_path = Path('/etc/claude-cn/install.json')
    check('已注册运行用户', install_path.is_file())
    if install_path.is_file():
        check('当前用户已授权', json.loads(install_path.read_text())['uid'] == os.getuid())
    check('后台启动服务接口', BROKER_SOCKET.is_socket())
    if Path('/run/systemd/system').exists():
        for name in ('claude-cn-netns', 'claude-cn-proxy', 'claude-cn-relay', 'claude-cn-launch', 'claude-cn-update.timer'):
            result = subprocess.run(['/usr/bin/systemctl', 'is-active', '--quiet', name], check=False)
            check(name, result.returncode == 0)
    raise SystemExit(int(failed))


def launch(args):
    from .client import launch as broker_launch
    return broker_launch(args)


def main():
    args = sys.argv[1:]
    try:
        if args[:1] == ['init']:
            return init(args[1:])
        if args[:1] == ['configure']:
            return configure(args[1:])
        if args[:1] == ['doctor']:
            return doctor()
        if args[:1] == ['version']:
            print('claude-cn ' + VERSION)
            return
        if args[:1] in (['help'], ['--help'], ['-h']):
            print('claude-cn：通用 Claude Code 供应商与网络控制工具（默认智谱）\n'
                  '  init [--from 文件]       初始化独立供应商配置\n'
                  '  configure [--base-url URL] [--model 名称]  修改供应商 / Key / 模型\n'
                  '  doctor                   检查运行路径、配置和服务\n'
                  '  self-test                验证网络边界（不调用模型）\n'
                  '  version                  显示本项目版本\n'
                  '  [run] [Claude参数...]     启动受限 Claude；--version 显示 Claude 版本\n'
                  '默认配置：/etc/claude-cn/runtime.json 和 ~/.config/claude-cn/settings.json')
            return
        if args[:1] == ['self-test']:
            args = ['--cn-self-test', *args[1:]]
        if args[:1] == ['run']:
            args = args[1:]
        launch(args)
    except (ValueError, OSError) as exc:
        # Do not print configuration contents or credentials.
        raise SystemExit('claude-cn: ' + type(exc).__name__ + '；请检查配置文件和程序路径。') from None
