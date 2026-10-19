"""Root-managed routed namespace. No host default firewall policy is changed."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import subprocess

from .policy import Policy, address
from .update import atomic_write

NS = 'claude-cn'
HOST_IF = 'ccn-host'
PEER_IF = 'ccn-peer'
TRANSIT4 = '169.254.253.0/30'
TRANSIT6 = 'fd6c:636e:1::/64'
STATE = Path('/run/claude-cn-network/state.json')
POLICY = '/etc/claude-cn/policy.json'
COUNTRY = '/var/lib/claude-cn/cn-networks.json'
RESOLV = Path('/etc/claude-cn/resolv.conf')
NFT = '/usr/sbin/nft'
IP = '/usr/sbin/ip'


def run(args, *, input=None, check=True):
    result = subprocess.run(args, input=input, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin'}, close_fds=True)
    if check and result.returncode:
        raise RuntimeError('network command failed: ' + ' '.join(args) + '\n' + result.stderr.strip())
    return result


def prefixes(values, version):
    nets = [ipaddress.ip_network(n, strict=True) for n in values]
    return [str(n) for n in ipaddress.collapse_addresses(n for n in nets if n.version == version)]


def sets(config, country):
    # Same permission semantics as the proxy, excluding special-use country IPs.
    Policy(config, country)
    country = [n for n in country if ipaddress.ip_network(n).network_address.is_global
               and ipaddress.ip_network(n).broadcast_address.is_global]
    result = []
    for name, version, rows in [('cn4', 4, country), ('cn6', 6, country),
                                ('extra4', 4, config.get('allow_cidrs', [])),
                                ('extra6', 6, config.get('allow_cidrs', []))]:
        values = prefixes(rows, version)
        elements = 'elements = { ' + ', '.join(values) + ' };' if values else ''
        result.append(f'set {name} {{ type ipv{version}_addr; flags interval; {elements} }}')
    return '\n'.join(result)


def allowed_rules():
    return '\n'.join(f'{family} daddr @{name} counter accept'
                     for family, name in [('ip', 'cn4'), ('ip6', 'cn6'),
                                          ('ip', 'extra4'), ('ip6', 'extra6')])


def namespace_rules(config, country):
    return f'''table inet claude_cn {{
{sets(config, country)}
chain output {{
 type filter hook output priority -200; policy drop;
 oifname "lo" accept
 ip6 daddr {{ {TRANSIT6}, fe80::/10, ff02::/16 }} icmpv6 type {{ nd-neighbor-solicit, nd-neighbor-advert }} accept
 {allowed_rules()}
 counter reject with icmpx type admin-prohibited
}}
chain input {{
 type filter hook input priority -200; policy drop;
 iifname "lo" accept
 ct state established,related accept
 ip6 saddr {{ {TRANSIT6}, fe80::/10 }} icmpv6 type {{ nd-neighbor-solicit, nd-neighbor-advert }} accept
}}
}}'''


def host_rules(config, country):
    return f'''table inet claude_cn {{
{sets(config, country)}
chain from_claude {{
 {allowed_rules()}
 counter reject with icmpx type admin-prohibited
}}
chain forward {{
 type filter hook forward priority -200; policy accept;
 iifname "{HOST_IF}" jump from_claude
 oifname "{HOST_IF}" ct state established,related accept
 oifname "{HOST_IF}" counter drop
}}
chain input {{
 type filter hook input priority -200; policy accept;
 iifname "{HOST_IF}" ip6 saddr {TRANSIT6} icmpv6 type {{ nd-neighbor-solicit, nd-neighbor-advert }} accept
 iifname "{HOST_IF}" jump from_claude
}}
}}
table inet claude_cn_nat {{
chain postrouting {{
 type nat hook postrouting priority srcnat; policy accept;
 iifname "{HOST_IF}" ip saddr {TRANSIT4} masquerade
 iifname "{HOST_IF}" ip6 saddr {TRANSIT6} masquerade
}}
}}'''


def load():
    config = json.loads(Path(POLICY).read_text())
    data = json.loads(Path(COUNTRY).read_text())
    Policy.load(POLICY, COUNTRY)
    return config, data['networks']


def nft_replace(text, tables, namespace=False):
    prefix = [IP, 'netns', 'exec', NS] if namespace else []
    existing = []
    for table in tables:
        if run(prefix + [NFT, 'list', 'table', 'inet', table], check=False).returncode == 0:
            existing.append(f'delete table inet {table}')
    # One netlink transaction: never flush an unrelated table or create an open gap.
    run(prefix + [NFT, '-f', '-'], input='\n'.join(existing) + '\n' + text + '\n')


def resolvers(config, country, paths=None):
    policy = Policy(config, country)
    servers, search = [], ''
    paths = paths or ['/run/systemd/resolve/resolv.conf', '/etc/resolv.conf']
    for path in paths:
        try:
            lines = Path(path).read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            fields = line.split('#', 1)[0].split()
            if len(fields) == 2 and fields[0] == 'nameserver':
                try:
                    ip = address(fields[1])
                except ValueError:
                    continue
                if not ip.is_loopback and policy.check_ip('', str(ip))[0] and str(ip) not in servers:
                    servers.append(str(ip))
            elif fields and fields[0] in ('search', 'domain'):
                search = ' '.join(fields) + '\n'
        if servers:
            break
    if not servers:
        for ip in ('223.5.5.5', '119.29.29.29'):
            if policy.check_ip('', ip)[0]:
                servers.append(ip)
    if not servers:
        raise RuntimeError('no allowed DNS server; configure an allowed mainland/LAN resolver')
    return ''.join('nameserver ' + ip + '\n' for ip in servers[:3]) + search + 'options timeout:2 attempts:2\n'


def reload_rules():
    config, country = load()
    dns = resolvers(config, country)
    nft_replace(host_rules(config, country), ['claude_cn', 'claude_cn_nat'])
    nft_replace(namespace_rules(config, country), ['claude_cn'], namespace=True)
    # Replace in place so existing per-process bind mounts see resolver changes.
    path = RESOLV
    with path.open('w') as stream:
        stream.write(dns)
    path.chmod(0o644)


def proc_value(path, value, state):
    path = Path(path)
    before = path.read_text().strip()
    if before != str(value):
        state['sysctls'].append({'path': str(path), 'before': before, 'set': str(value)})
        atomic_write(STATE, state)
        path.write_text(str(value))


def forward_rules(add, state):
    # Docker commonly makes FORWARD policy DROP. These narrow exceptions run
    # only after the nft priority -200 boundary has checked Claude's destination.
    for binary in ('/usr/sbin/iptables', '/usr/sbin/ip6tables'):
        for spec in (['-i', HOST_IF, '-m', 'comment', '--comment', 'claude-cn', '-j', 'ACCEPT'],
                     ['-o', HOST_IF, '-m', 'conntrack', '--ctstate', 'ESTABLISHED,RELATED',
                      '-m', 'comment', '--comment', 'claude-cn', '-j', 'ACCEPT']):
            if add:
                if run([binary, '-w', '-C', 'FORWARD', *spec], check=False).returncode != 0:
                    # Record intent before mutation so partial setup can roll back.
                    state['iptables'].append([binary, *spec])
                    atomic_write(STATE, state)
                    run([binary, '-w', '-I', 'FORWARD', '1', *spec])


def up():
    STATE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if STATE.exists():
        raise RuntimeError('stale network state; run network down before retrying')
    if run([IP, 'link', 'show', HOST_IF], check=False).returncode == 0 or Path('/run/netns/' + NS).exists():
        raise RuntimeError('reserved namespace/interface already exists; refusing to take ownership')
    for table in ('claude_cn', 'claude_cn_nat'):
        if run([NFT, 'list', 'table', 'inet', table], check=False).returncode == 0:
            raise RuntimeError('reserved nft table exists without ownership state: ' + table)
    load()  # Refuse invalid/missing policy before any network changes.
    state = {'schema': 1, 'sysctls': [], 'iptables': [], 'namespace': False, 'veth': False,
             'ipv6_forwarding': {str(p): p.read_text().strip()
                                 for p in Path('/proc/sys/net/ipv6/conf').glob('*/forwarding')
                                 if p.parent.name != 'all'}}
    atomic_write(STATE, state)
    try:
        run([IP, 'netns', 'add', NS])
        state['namespace'] = True
        atomic_write(STATE, state)
        run([IP, 'link', 'add', HOST_IF, 'type', 'veth', 'peer', 'name', PEER_IF])
        state['veth'] = True
        atomic_write(STATE, state)
        run([IP, 'link', 'set', PEER_IF, 'netns', NS])
        # Install both boundaries before giving the peer an address/default route.
        reload_rules()
        run([IP, 'addr', 'add', '169.254.253.1/30', 'dev', HOST_IF])
        run([IP, '-6', 'addr', 'add', 'fd6c:636e:1::1/64', 'dev', HOST_IF, 'nodad'])
        run([IP, 'link', 'set', HOST_IF, 'up'])
        for args in (['link', 'set', 'lo', 'up'], ['addr', 'add', '169.254.253.2/30', 'dev', PEER_IF],
                     ['-6', 'addr', 'add', 'fd6c:636e:1::2/64', 'dev', PEER_IF, 'nodad'],
                     ['link', 'set', PEER_IF, 'up'], ['route', 'add', 'default', 'via', '169.254.253.1'],
                     ['-6', 'route', 'add', 'default', 'via', 'fd6c:636e:1::1']):
            run([IP, '-n', NS, *args])
        # Forwarding disables normal accept_ra=1. Preserve host RA behaviour,
        # including DHCP/SLAAC default routes, before changing the forwarding flag.
        if Path('/proc/sys/net/ipv6/conf/all/forwarding').read_text().strip() != '1':
            for path in Path('/proc/sys/net/ipv6/conf').glob('*/accept_ra'):
                if path.read_text().strip() == '1':
                    proc_value(path, 2, state)
        proc_value('/proc/sys/net/ipv4/ip_forward', 1, state)
        proc_value('/proc/sys/net/ipv6/conf/all/forwarding', 1, state)
        run([IP, 'netns', 'exec', NS, '/usr/sbin/sysctl', '-q', '-w',
             'net.ipv4.ping_group_range=0 2147483647'])
        forward_rules(True, state)
    except BaseException:
        down()
        raise


def down():
    if not STATE.exists():
        return
    state = json.loads(STATE.read_text())
    errors = []
    forwarding_now = {name: Path(name).read_text().strip()
                      for name in state.get('ipv6_forwarding', {}) if Path(name).exists()}
    # Remove the peer's only route out before removing any guard rules.
    if state.get('veth'):
        result = run([IP, 'link', 'delete', HOST_IF], check=False)
        if result.returncode and run([IP, 'link', 'show', HOST_IF], check=False).returncode == 0:
            raise RuntimeError('cannot disconnect Claude network; keeping boundary and state')
    if state.get('namespace'):
        run([IP, 'netns', 'delete', NS], check=False)
        if Path('/run/netns/' + NS).exists():
            errors.append('namespace still exists')
    for record in reversed(state['iptables']):
        run([record[0], '-w', '-D', 'FORWARD', *record[1:]], check=False)
        if run([record[0], '-w', '-C', 'FORWARD', *record[1:]], check=False).returncode == 0:
            errors.append('FORWARD exception still exists')
    for table in ('claude_cn_nat', 'claude_cn'):
        run([NFT, 'delete', 'table', 'inet', table], check=False)
        if run([NFT, 'list', 'table', 'inet', table], check=False).returncode == 0:
            errors.append('nft table still exists: ' + table)
    for record in reversed(state['sysctls']):
        path = Path(record['path'])
        # Do not overwrite an unrelated change made after installation.
        if path.exists() and path.read_text().strip() == record['set']:
            path.write_text(record['before'])
    if any(r['path'] == '/proc/sys/net/ipv6/conf/all/forwarding' for r in state['sysctls']):
        for name, before in state.get('ipv6_forwarding', {}).items():
            path = Path(name)
            if path.exists() and forwarding_now.get(name) == '1':
                path.write_text(before)
    if errors:
        raise RuntimeError('; '.join(errors) + '; keeping state for cleanup retry')
    STATE.unlink()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['up', 'down', 'reload'])
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('network administration requires root')
    {'up': up, 'down': down, 'reload': reload_rules}[args.action]()


if __name__ == '__main__':
    main()
