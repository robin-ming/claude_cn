#!/usr/bin/python3 -I
"""Runs as the original user, after entering the namespace and seccomp boundary."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, '/usr/lib/claude-cn')
from claude_cn.runtime import find_claude


def main():
    if os.environ.get('CLAUDE_CN_ACTIVE') != '1':
        raise SystemExit('claude-cn: missing network boundary')
    if str(os.stat('/proc/self/ns/net').st_ino) != os.environ.get('CLAUDE_CN_EXPECTED_NS'):
        raise SystemExit('claude-cn: network namespace mismatch')
    if sys.argv[1:2] == ['--cn-self-test']:
        from claude_cn.probe import main as probe
        return probe()
    settings = Path(os.environ['CLAUDE_CN_SETTINGS'])
    # Validate without printing contents or creating a second credentials file.
    parsed = json.loads(settings.read_text())
    if not isinstance(parsed, dict):
        raise SystemExit('claude-cn: original GLM settings must be a JSON object')
    binary = find_claude(os.environ['CLAUDE_CN_EXECUTABLE'])
    os.execv(binary, [binary, '--settings', str(settings), *sys.argv[1:]])


if __name__ == '__main__':
    main()
