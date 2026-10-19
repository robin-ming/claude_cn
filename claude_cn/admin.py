"""Root administration, independent from any external GLM wrapper."""
import argparse
import json
import os
from pathlib import Path
import pwd
import subprocess

from .runtime import expand_path, read_runtime, write_settings

INSTALL = Path('/etc/claude-cn/install.json')
SEED = Path('/etc/claude-cn/provider.json')
SUDOERS = Path('/etc/sudoers.d/claude-cn')
UNITS = ['claude-cn-netns.service', 'claude-cn-proxy.service', 'claude-cn-relay.service', 'claude-cn-launch.service', 'claude-cn-update.timer']


def run(args):
    subprocess.run(args, check=True, env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin'})


def initialize_user(account, data, destination):
    # Drop root before touching the user's writable directory or following links.
    child = os.fork()
    if child == 0:
        try:
            os.initgroups(account.pw_name, account.pw_gid)
            os.setgid(account.pw_gid)
            os.setuid(account.pw_uid)
            write_settings(data, destination)
            os._exit(0)
        except BaseException:
            os._exit(1)
    _, status = os.waitpid(child, 0)
    if status != 0:
            raise RuntimeError('无法以目标用户身份创建供应商配置；现有配置不会覆盖')


def setup(user):
    account = pwd.getpwnam(user)
    if account.pw_uid == 0:
        raise ValueError('不支持 root 作为运行用户')
    if INSTALL.exists() and json.loads(INSTALL.read_text())['uid'] != account.pw_uid:
        raise ValueError('当前已注册其他用户，请先执行 claude-cn-admin reset')
    runtime = read_runtime()
    settings = expand_path(runtime['settings_path'], account.pw_dir)
    # Validate explicit paths but defer executable checks/discovery to the user
    # process; never execute a user-owned binary with root privileges.
    if runtime['claude_path'] != 'auto':
        expand_path(runtime['claude_path'], account.pw_dir)
    data = json.loads(SEED.read_text())
    initialize_user(account, data, settings)
    data = {'schema': 2, 'user': user, 'uid': account.pw_uid, 'home': account.pw_dir}
    INSTALL.write_text(json.dumps(data, indent=2) + '\n')
    INSTALL.chmod(0o644)
    # Upgrade removes the old passwordless sudo entry; runtime uses the broker.
    SUDOERS.unlink(missing_ok=True)
    run(['/usr/bin/systemctl', 'daemon-reload'])
    run(['/usr/bin/systemctl', 'enable', *UNITS])
    run(['/usr/bin/systemctl', 'start', *[u for u in UNITS if u != 'claude-cn-launch.service']])
    run(['/usr/bin/systemctl', 'restart', 'claude-cn-launch.service'])
    for unit in UNITS:
        run(['/usr/bin/systemctl', 'is-active', '--quiet', unit])
    print('已启用独立 claude-cn。供应商配置：' + str(settings))
    print('运行 claude-cn doctor 和 claude-cn self-test；API Key 为空时先运行 claude-cn configure。')


def reset():
    run(['/usr/bin/systemctl', 'stop', 'claude-cn-launch.service', 'claude-cn-update.timer', 'claude-cn-update.service',
         'claude-cn-relay.service', 'claude-cn-proxy.service', 'claude-cn-netns.service'])
    run(['/usr/bin/systemctl', 'disable', *UNITS])
    SUDOERS.unlink(missing_ok=True)
    INSTALL.unlink(missing_ok=True)
    print('运行授权和服务已停用，保留用户智谱配置与系统白名单。')


def main():
    parser = argparse.ArgumentParser(prog='claude-cn-admin')
    sub = parser.add_subparsers(dest='action', required=True)
    setup_parser = sub.add_parser('setup')
    setup_parser.add_argument('--user', required=True)
    sub.add_parser('reset')
    sub.add_parser('reload')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('请使用 sudo 执行系统管理命令')
    if args.action == 'setup':
        setup(args.user)
    elif args.action == 'reset':
        reset()
    else:
        run(['/usr/bin/systemctl', 'reload', 'claude-cn-netns.service'])
        run(['/usr/bin/systemctl', 'restart', 'claude-cn-proxy.service'])


if __name__ == '__main__':
    main()
