import ipaddress
import socket
import unittest

from claude_cn.policy import Policy, address, hostname
from claude_cn.update import parse_list


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = Policy({'allow_domains': ['github.com', '*.github.com'], 'ports': [80, 443]},
                             ['223.5.5.0/24', '240e::/20'])

    def test_mainland_ip_and_both_families(self):
        for value in ('223.5.5.5', '240e::1'):
            self.assertEqual(self.policy.check_ip('unlisted.example', value), (True, 'mainland-ip'))
        for value in ('1.1.1.1', '2606:4700::1111'):
            self.assertFalse(self.policy.check_ip('unlisted.example', value)[0])

    def test_chinese_domain_is_not_a_bypass(self):
        self.assertFalse(self.policy.check_ip('some-site.cn', '1.1.1.1')[0])

    def test_domain_boundaries(self):
        for host in ('github.com', 'api.github.com'):
            self.assertTrue(self.policy.check_ip(host, '1.1.1.1')[0])
        for host in ('evilgithub.com', 'github.com.evil.example'):
            self.assertFalse(self.policy.check_ip(host, '1.1.1.1')[0])
        self.assertEqual(hostname('GITHUB.COM.'), 'github.com')

    def test_local_and_special_addresses_not_allowed_by_domain(self):
        for value in ('127.0.0.1', '192.168.1.1', '169.254.169.254', '::1', 'fc00::1',
                      '::ffff:127.0.0.1', '224.0.0.1', '0.0.0.0'):
            self.assertFalse(self.policy.check_ip('github.com', value)[0], value)

    def test_explicit_private_exception(self):
        p = Policy({'allow_cidrs': ['192.168.1.10/32']}, [])
        self.assertTrue(p.check_ip('local.example', '192.168.1.10')[0])
        self.assertFalse(p.check_ip('local.example', '192.168.1.11')[0])

    def test_mapped_foreign_ipv6_is_not_mainland(self):
        self.assertEqual(address('::ffff:1.1.1.1'), ipaddress.ip_address('1.1.1.1'))
        self.assertFalse(self.policy.check_ip('example.cn', '::ffff:1.1.1.1')[0])

    def test_mainland_and_explicitly_allowed_lan_can_use_any_tcp_port(self):
        p = Policy({'allow_cidrs': ['10.0.0.0/8', '192.168.8.0/24',
                                   '192.168.10.0/24', 'fc00::/7', '1.1.1.1/32'],
                    'allow_domains': ['github.com']}, ['223.5.5.0/24', '240e::/20'])

        class FakeSocket:
            def settimeout(self, _): pass
            def connect(self, target): self.target = target
            def close(self): pass

        for ip in ('10.180.135.190', '192.168.8.1', '192.168.10.1', 'fd00::1',
                   '::ffff:10.1.2.3', '223.5.5.5', '240e::1', '::ffff:223.5.5.5'):
            family = socket.AF_INET6 if ':' in ip else socket.AF_INET
            for port in (1, 22, 3000, 8080, 65535):
                def resolver(host, requested_port, **kwargs):
                    return [(family, socket.SOCK_STREAM, 6, '', (ip, requested_port))]
                sock, connected, _ = p.connect('github.com', port, resolver, lambda *a: FakeSocket())
                self.assertEqual(connected, ip)
                self.assertEqual(sock.target[1], port)
        for host, ip in (('lan.example', '192.168.9.1'), ('lan.example', '11.0.0.1'),
                         ('github.com', '1.1.1.1')):
            def resolver(*args, **kwargs):
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 8080))]
            with self.assertRaises(PermissionError, msg=ip):
                p.connect(host, 8080, resolver, lambda *a: self.fail('unexpected connection'))

    def test_port_exception_does_not_leak_to_foreign_dns_fallback(self):
        p = Policy({'allow_cidrs': ['10.0.0.0/8'], 'allow_domains': ['github.com']}, ['223.5.5.0/24'])
        attempted = []

        class FailedSocket:
            def settimeout(self, _): pass
            def connect(self, target):
                attempted.append(target)
                raise OSError('unreachable destination')
            def close(self): pass

        for first in ('10.1.2.3', '223.5.5.5'):
            attempted.clear()
            def resolver(*args, **kwargs):
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 8080))
                        for ip in (first, '1.1.1.1')]
            with self.assertRaises(OSError):
                p.connect('github.com', 8080, resolver, lambda *a: FailedSocket())
            self.assertEqual(attempted, [(first, 8080)])

    def test_dns_checked_once_then_connects_to_checked_ip(self):
        calls = []

        def resolver(host, port, **kwargs):
            calls.append((host, port))
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('1.1.1.1', port)),
                    (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('223.5.5.5', port))]

        class FakeSocket:
            def settimeout(self, _): pass
            def connect(self, target): self.target = target
            def close(self): pass

        sock, ip, _ = self.policy.connect('example.cn', 443, resolver, lambda *args: FakeSocket())
        self.assertEqual(calls, [('example.cn', 443)])
        self.assertEqual(sock.target, ('223.5.5.5', 443))
        self.assertEqual(ip, '223.5.5.5')

    def test_denied_target_never_opens_socket(self):
        def resolver(*args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('1.1.1.1', 443))]
        def connector(*args):
            self.fail('must not connect to denied IP')
        with self.assertRaises(PermissionError):
            self.policy.connect('unknown.example', 443, resolver, connector)
        with self.assertRaises(PermissionError):
            self.policy.connect('github.com', 22, resolver, connector)

    def test_invalid_hostnames(self):
        for value in ('example.com@evil.com', 'example.com/path', 'fe80::1%eth0', 'a\r\nb', '*'):
            with self.assertRaises(ValueError):
                hostname(value)

    def test_updater_rejects_broad_or_broken_data(self):
        for value in ('0.0.0.0/0', '127.0.0.0/8', 'not-a-cidr', '1.1.1.1/24', '::/0'):
            with self.assertRaises(ValueError):
                parse_list(value, 4)
        with self.assertRaises(ValueError):
            parse_list('', 6)
        self.assertEqual(parse_list('223.5.5.0/24\n', 4), ['223.5.5.0/24'])

    def test_updater_counts_only_distinct_networks(self):
        # Reviewer reproduction: repeated records must not satisfy a minimum
        # that exists to prove the snapshot is complete.
        text = '223.5.5.0/24\n' * 3
        with self.assertRaises(ValueError):
            parse_list(text, 4, minimum=3)
        self.assertEqual(parse_list(text, 4, minimum=1), ['223.5.5.0/24'])

    def test_updater_rejects_implausible_coverage_swings(self):
        from claude_cn.update import check_plausible
        old = {'networks': ['1.0.0.0/8', '240e::/32']}
        tiny = {'networks': ['1.0.1.0/24', '240e::/32']}
        huge = {'networks': ['1.0.0.0/6', '240e::/32']}
        sane = {'networks': ['1.0.0.0/8', '240e::/31']}
        for bad in (tiny, huge):
            with self.assertRaises(ValueError):
                check_plausible(old, bad)
        check_plausible(old, sane)
        check_plausible(None, sane)
        with self.assertRaises(ValueError):
            check_plausible(old, {'networks': ['1.0.0.0/8']})


if __name__ == '__main__':
    unittest.main()
