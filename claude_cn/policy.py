"""Destination checks. DNS answers are checked and the selected IP is pinned."""
import bisect
import ipaddress
import json
import re
import socket
from pathlib import Path


def hostname(value):
    value = value.rstrip('.').lower()
    if not value or '%' in value or any(c in value for c in '/\\@\r\n\t '):
        raise ValueError('invalid hostname')
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        value = value.encode('idna').decode('ascii')
        if len(value) > 253 or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', p)
                                       for p in value.split('.')):
            raise ValueError('invalid hostname')
        return value


def address(value):
    ip = ipaddress.ip_address(value)
    return ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip


class Networks:
    def __init__(self, values):
        self.ranges = {}
        for version in (4, 6):
            nets = [ipaddress.ip_network(v, strict=True) for v in values]
            nets = list(ipaddress.collapse_addresses(n for n in nets if n.version == version))
            self.ranges[version] = ([int(n.network_address) for n in nets],
                                    [int(n.broadcast_address) for n in nets])

    def contains(self, ip):
        starts, ends = self.ranges[ip.version]
        index = bisect.bisect_right(starts, int(ip)) - 1
        return index >= 0 and int(ip) <= ends[index]


class Policy:
    def __init__(self, config, country_networks):
        self.ports = set(config.get('ports', [80, 443]))
        if not self.ports or any(type(p) is not int or not 1 <= p <= 65535 for p in self.ports):
            raise ValueError('ports must be integers in 1..65535')
        self.domains = []
        for item in config.get('allow_domains', []):
            wildcard = item.startswith('*.')
            self.domains.append((hostname(item[2:] if wildcard else item), wildcard))
        self.cn = Networks(country_networks)
        self.extra = Networks(config.get('allow_cidrs', []))
        self.lan = Networks(['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', 'fc00::/7'])
        self.timeout = float(config.get('connect_timeout_seconds', 10))
        self.idle_timeout = float(config.get('idle_timeout_seconds', 300))
        if not 0 < self.timeout <= 120 or not 0 < self.idle_timeout <= 86400:
            raise ValueError('invalid timeout')

    @classmethod
    def load(cls, config_path, country_path):
        config = json.loads(Path(config_path).read_text())
        data = json.loads(Path(country_path).read_text())
        if data.get('schema') != 1 or not data.get('networks'):
            raise ValueError('missing or invalid country database; refusing to start')
        return cls(config, data['networks'])

    def domain_allowed(self, host):
        return any(host == suffix if not wildcard else host.endswith('.' + suffix)
                   for suffix, wildcard in self.domains)

    def check_ip(self, host, value):
        ip = address(value)
        # Private/local destinations require an explicit CIDR even for allowed domains.
        if self.extra.contains(ip):
            return True, 'custom-cidr'
        if not ip.is_global or ip.is_multicast or ip.is_reserved:
            return False, 'local-or-special-address'
        if self.domain_allowed(host):
            return True, 'custom-domain'
        if self.cn.contains(ip):
            return True, 'mainland-ip'
        return False, 'outside-mainland-and-allowlist'

    def connect(self, host, port, resolver=socket.getaddrinfo, connector=None):
        host = hostname(host)
        if type(port) is not int or not 1 <= port <= 65535:
            raise PermissionError('port-not-allowed')
        answers = resolver(host, port, type=socket.SOCK_STREAM)
        last_error = None
        permitted = False
        denied_port = False
        for family, socktype, proto, _, sockaddr in answers:
            if family not in (socket.AF_INET, socket.AF_INET6):
                continue
            allowed, reason = self.check_ip(host, sockaddr[0])
            if not allowed:
                continue
            ip = address(sockaddr[0])
            # Mainland IPs and explicitly allowed LAN addresses have no TCP port
            # restriction. Check each DNS answer so exemptions cannot carry over
            # to a foreign address returned for the same hostname.
            unrestricted = self.cn.contains(ip) or (self.extra.contains(ip) and self.lan.contains(ip))
            if port not in self.ports and not unrestricted:
                denied_port = True
                continue
            permitted = True
            # Never resolve the hostname again after checking it.
            sock = connector(family, socktype, proto) if connector else socket.socket(family, socktype, proto)
            try:
                sock.settimeout(self.timeout)
                sock.connect(sockaddr)
                sock.settimeout(self.idle_timeout)
                return sock, sockaddr[0], reason
            except OSError as exc:
                last_error = exc
                sock.close()
        if not permitted:
            raise PermissionError('port-not-allowed' if denied_port else 'destination-not-allowed')
        raise OSError('allowed-destination-unreachable') from last_error
