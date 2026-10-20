import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from claude_cn import cli


class CliTests(unittest.TestCase):
    def test_custom_compatible_provider_and_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)/'settings.json'
            with patch.object(cli, 'settings_path', return_value=target), \
                 patch('sys.stdin'), \
                 patch.object(cli.getpass, 'getpass', return_value='test-provider-key'), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                cli.configure(['--base-url', 'https://api.example.cn/anthropic', '--model', 'custom-model'])
            data = json.loads(target.read_text())['env']
            self.assertEqual(data['ANTHROPIC_BASE_URL'], 'https://api.example.cn/anthropic')
            self.assertEqual(data['ANTHROPIC_MODEL'], 'custom-model')
            self.assertEqual(data['ANTHROPIC_AUTH_TOKEN'], 'test-provider-key')
            self.assertNotIn('test-provider-key', output.getvalue())

    def test_configure_without_terminal_keeps_existing_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)/'settings.json'
            target.write_text('{"env": {"ANTHROPIC_AUTH_TOKEN": "old-key"}}\n')
            with patch.object(cli, 'settings_path', return_value=target), \
                 patch('sys.stdin') as stdin, \
                 patch.object(cli.getpass, 'getpass', side_effect=AssertionError('unexpected prompt')) as prompted, \
                 contextlib.redirect_stdout(io.StringIO()):
                stdin.isatty.return_value = False
                cli.configure(['--model', 'new-model'])
            data = json.loads(target.read_text())['env']
            self.assertEqual(data['ANTHROPIC_AUTH_TOKEN'], 'old-key')
            self.assertEqual(data['ANTHROPIC_MODEL'], 'new-model')
            prompted.assert_not_called()

    def test_default_zhipu_template_has_no_key(self):
        env = json.loads(cli.template_path().read_text())['env']
        self.assertEqual(env['ANTHROPIC_AUTH_TOKEN'], '')
        self.assertIn('open.bigmodel.cn', env['ANTHROPIC_BASE_URL'])
        self.assertEqual(env['ANTHROPIC_MODEL'], 'glm-5.3[1m]')

    def test_launch_uses_broker_without_sudo(self):
        with patch('claude_cn.client.launch') as launch, patch.object(cli.os, 'execv') as execute:
            cli.launch(['--resume', 'session with spaces'])
        launch.assert_called_once_with(['--resume', 'session with spaces'])
        execute.assert_not_called()


if __name__ == '__main__':
    unittest.main()
