import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from claude_cn.runtime import expand_path, find_claude, read_runtime, write_settings


class RuntimeTests(unittest.TestCase):
    def test_default_runtime_is_independent_from_old_wrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = read_runtime(Path(tmp)/'absent.json')
            self.assertEqual(config['claude_path'], 'auto')
            self.assertEqual(config['settings_path'], '~/.config/claude-cn/settings.json')
            self.assertNotIn('claude-glm', json.dumps(config))

    def test_stable_entry_survives_version_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            entry = home/'.local/bin/claude'
            entry.parent.mkdir(parents=True)
            for name in ('1', '2'):
                target = home/('version-' + name)
                target.write_text('#!/bin/sh\nprintf version-' + name)
                target.chmod(0o755)
            entry.symlink_to(home/'version-1')
            found = find_claude('auto', home=home, path='')
            self.assertEqual(found, str(entry))
            self.assertEqual(subprocess.check_output([found]), b'version-1')
            entry.unlink()
            entry.symlink_to(home/'version-2')
            self.assertEqual(find_claude('auto', home=home, path=''), str(entry))
            self.assertEqual(subprocess.check_output([found]), b'version-2')

    def test_explicit_path_and_home_expansion(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp)/'custom/bin/claude'
            binary.parent.mkdir(parents=True)
            binary.write_text('#!/bin/sh\nexit 0\n')
            binary.chmod(0o755)
            self.assertEqual(find_claude('~/custom/bin/claude', home=tmp), str(binary))
            with self.assertRaises(FileNotFoundError):
                find_claude('~/missing', home=tmp)
            with self.assertRaises(ValueError):
                expand_path('relative/path', tmp)

    def test_auto_uses_current_path_for_npm_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = Path(tmp)/'claude'
            entry.write_text('#!/usr/bin/env node\n')
            entry.chmod(0o755)
            self.assertEqual(find_claude('auto', home=tmp, path=tmp), str(entry))

    def test_key_config_permissions_and_no_silent_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)/'claude-cn/settings.json'
            data = {'env': {'ANTHROPIC_AUTH_TOKEN': 'test-placeholder'}}
            self.assertTrue(write_settings(data, target))
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)
            self.assertFalse(write_settings({'env': {}}, target))
            self.assertEqual(json.loads(target.read_text()), data)

    def test_runtime_rejects_unknown_or_empty_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'runtime.json'
            for data in ({'settings_path': ''}, {'unknown': 'value'}, []):
                path.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    read_runtime(path)


if __name__ == '__main__':
    unittest.main()
