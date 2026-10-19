import json
from pathlib import Path
import tempfile
import unittest

from claude_cn.network import host_rules, namespace_rules, resolvers


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(Path('config/policy.json').read_text())
        self.country = ['223.5.5.0/24', '240e::/20']

    def test_direct_rules_do_not_open_foreign_domain_ips(self):
        for rules in (host_rules(self.config, self.country), namespace_rules(self.config, self.country)):
            self.assertIn('223.5.5.0/24', rules)
            self.assertIn('240e::/20', rules)
            self.assertIn('10.0.0.0/8', rules)
            self.assertNotIn('github.com', rules)
            self.assertNotIn('dport', rules)
        guest = namespace_rules(self.config, self.country)
        self.assertIn('hook output priority -200; policy drop;', guest)
        self.assertNotIn('ct state established,related accept', guest.split('chain input')[0])

    def test_host_boundary_is_scoped_to_claude_interface(self):
        rules = host_rules(self.config, self.country)
        self.assertIn('policy accept;', rules)
        self.assertIn('iifname "ccn-host" jump from_claude', rules)
        self.assertIn('iifname "ccn-host" ip saddr 169.254.253.0/30 masquerade', rules)
        self.assertIn('iifname "ccn-host" ip6 saddr fd6c:636e:1::/64 masquerade', rules)

    def test_resolver_does_not_use_stub_or_unapproved_foreign_dns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'resolv.conf'
            path.write_text('nameserver 127.0.0.53\nnameserver 1.1.1.1\nnameserver 10.42.0.1\nsearch corp.example\n')
            result = resolvers(self.config, self.country, [path])
            self.assertIn('nameserver 10.42.0.1', result)
            self.assertIn('search corp.example', result)
            self.assertNotIn('127.0.0.53', result)
            self.assertNotIn('1.1.1.1', result)

    def test_resolver_falls_back_only_to_allowed_mainland_dns(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = resolvers(self.config, self.country, [Path(tmp)/'missing'])
            self.assertIn('nameserver 223.5.5.5', result)
        with self.assertRaises(RuntimeError):
            resolvers({}, [], ['/nonexistent-claude-cn-resolver'])


if __name__ == '__main__':
    unittest.main()
