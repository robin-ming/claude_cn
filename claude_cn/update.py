"""Fetch and atomically replace a validated IPv4 + IPv6 country snapshot."""
import argparse
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import tempfile
import urllib.request

SOURCES = {
    4: 'https://china-operator-ip.yfgao.com/china.txt',
    6: 'https://china-operator-ip.yfgao.com/china6.txt',
}


def parse_list(text, version, minimum=1):
    networks = []
    for line in text.splitlines():
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        net = ipaddress.ip_network(line, strict=True)
        if net.version != version or net.prefixlen < (8 if version == 4 else 16):
            raise ValueError('unexpected country prefix: ' + line)
        if not net.network_address.is_global or not net.broadcast_address.is_global:
            raise ValueError('non-global country prefix: ' + line)
        networks.append(net)
    # Collapse first: duplicates and overlapping entries must not count towards
    # the minimum, or a file of one network repeated 1000 times would pass.
    collapsed = [str(net) for net in ipaddress.collapse_addresses(networks)]
    if len(collapsed) < minimum:
        raise ValueError(f'incomplete IPv{version} data: {len(collapsed)} distinct entries')
    return collapsed


def covered_space(rows):
    """Total address space of a network list, used for update sanity checks."""
    return sum(int(ipaddress.ip_network(row).num_addresses) for row in rows)


def exclude_space(networks, remove):
    """Address space of networks not covered by remove.

    CIDR networks are either disjoint or nested; containment is decided per
    remaining piece, because pieces split by an earlier exclusion may be
    disjoint from the network currently being removed.
    """
    pieces = []
    for net in networks:
        remaining = [net]
        for other in remove:
            reduced = []
            for piece in remaining:
                if other.version != piece.version or not other.overlaps(piece):
                    reduced.append(piece)
                elif piece.subnet_of(other):
                    pass
                elif other.subnet_of(piece):
                    reduced.extend(piece.address_exclude(other))
                else:
                    reduced.append(piece)
            remaining = reduced
            if not remaining:
                break
        pieces.extend(remaining)
    return pieces


def check_plausible(old_data, new_data):
    """Reject snapshots whose per-family coverage collapses, explodes or is swapped.

    A wrong library could mark foreign addresses as mainland; unusual swings
    or large-scale replacement against the current snapshot keep the old
    library in place instead. This detects anomalies only; it cannot prove
    the geographic origin of any address.
    """
    for version in ('4', '6'):
        new_rows = [row for row in new_data['networks']
                    if ipaddress.ip_network(row).version == int(version)]
        new_nets = [ipaddress.ip_network(row) for row in new_rows]
        new = sum(net.num_addresses for net in new_nets)
        if not new:
            raise ValueError(f'new snapshot covers no IPv{version} space')
        if not old_data:
            continue
        old_nets = [ipaddress.ip_network(row) for row in old_data.get('networks', [])
                    if ipaddress.ip_network(row).version == int(version)]
        old = sum(net.num_addresses for net in old_nets)
        if not old:
            continue
        if not 0.5 <= new / old <= 2.0:
            raise ValueError(f'IPv{version} coverage changed {new / old:.2f}x; keeping the old library')
        # Equal-size replacement evades the total check; bound the churn itself.
        added = sum(net.num_addresses for net in exclude_space(new_nets, old_nets))
        removed = sum(net.num_addresses for net in exclude_space(old_nets, new_nets))
        if added > 0.5 * old or removed > 0.5 * old:
            raise ValueError(f'IPv{version} space replaced (added {added / old:.0%}, '
                             f'removed {removed / old:.0%}); keeping the old library')


def download_snapshot(proxy=None):
    networks = []
    counts = {}
    for version, url in SOURCES.items():
        request = urllib.request.Request(url, headers={'User-Agent': 'claude-cn-country-updater/1'})
        # Only use an explicitly configured administrative download proxy.
        proxies = {'http': proxy, 'https': proxy} if proxy else {}
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
        with opener.open(request, timeout=30) as response:
            raw = response.read(4_000_001)
        if len(raw) > 4_000_000:
            raise ValueError('country database exceeds size limit')
        rows = parse_list(raw.decode('ascii'), version, 1000 if version == 4 else 100)
        counts[str(version)] = len(rows)
        networks.extend(rows)
    return {'schema': 1, 'updated_at': datetime.now(timezone.utc).isoformat(),
            'sources': list(SOURCES.values()), 'counts': counts, 'networks': networks}


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--proxy', help='Explicit HTTP proxy, e.g. http://127.0.0.1:10808')
    parser.add_argument('--download-config', help='JSON with http_proxy for database updates only')
    args = parser.parse_args()
    proxy = args.proxy
    if args.download_config:
        proxy = proxy or json.loads(Path(args.download_config).read_text()).get('http_proxy')
    if proxy and not proxy.startswith(('http://', 'https://')):
        parser.error('download proxy must use http:// or https://')
    output = Path(args.output)
    old_data = None
    if output.exists():
        try:
            old_data = json.loads(output.read_text())
        except ValueError:
            old_data = None
    data = download_snapshot(proxy)
    try:
        check_plausible(old_data, data)
    except ValueError as exc:
        raise SystemExit(f'claude-cn update: {exc}') from None
    atomic_write(output, data)
    print('Country snapshot updated:', json.dumps(data['counts']))


if __name__ == '__main__':
    main()
