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
                 patch.object(cli.getpass, 'getpass', return_value='test-provider-key'), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                cli.configure(['--base-url', 'https://api.example.cn/anthropic', '--model', 'custom-model'])
            data = json.loads(target.read_text())['env']
            self.assertEqual(data['ANTHROPIC_BASE_URL'], 'https://api.example.cn/anthropic')
            self.assertEqual(data['ANTHROPIC_MODEL'], 'custom-model')
            self.assertEqual(data['ANTHROPIC_AUTH_TOKEN'], 'test-provider-key')
            self.assertNotIn('test-provider-key', output.getvalue())

    def test_default_zhipu_template_has_no_key(self):
        env = json.loads(cli.template_path().read_text())['env']
        self.assertEqual(env['ANTHROPIC_AUTH_TOKEN'], '')
        self.assertIn('open.bigmodel.cn', env['ANTHROPIC_BASE_URL'])
        self.assertEqual(env['ANTHROPIC_MODEL'], 'glm-5.3[1m]')

    def test_launch_forwards_args_and_no_key_in_sudo_arguments(self):
        with patch.object(cli.Path, 'is_file', return_value=True), \
             patch.object(cli.os, 'execv') as execute, \
             patch.dict(cli.os.environ, {'ANTHROPIC_AUTH_TOKEN': 'must-not-forward',
                                        'HTTPS_PROXY': 'http://untrusted:1234'}):
            cli.launch(['--resume', 'session with spaces'])
        args = execute.call_args.args[1]
        self.assertEqual(args[-3:], ['--', '--resume', 'session with spaces'])
        self.assertNotIn('must-not-forward', str(args))
        self.assertNotIn('untrusted', str(args))


if __name__ == '__main__':
    unittest.main()
