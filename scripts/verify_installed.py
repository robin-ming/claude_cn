#!/usr/bin/python3
"""Exercise the installed boundary as an ordinary user, without model calls.

A temporary executable is selected through the documented auto PATH lookup.
It runs after the installed helper drops root, in the real Claude namespace and
under the same seccomp filter. No installed files/settings are modified.
"""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
RUNNER = r'''
import errno,json,os,socket,subprocess,sys
from pathlib import Path
p=json.loads(sys.argv[-1]); checks=[]
def check(name,ok,detail=''):
 checks.append({'name':name,'ok':bool(ok),'detail':detail})
def attempt(name,func):
 try:func()
 except Exception as exc:check(name,False,type(exc).__name__+': '+str(exc))
def echo(kind,port):
 with socket.socket(socket.AF_INET,kind) as s:
  s.settimeout(5);s.connect((p['host'],port));s.sendall(b'claude-cn-installed-test')
  assert s.recv(128)==b'claude-cn-installed-test'
def blocked_errno(ip,exc):
 if exc.errno in (errno.EPERM,errno.EACCES):return True
 if exc.errno!=errno.EHOSTUNREACH:return False
 # IPv4 TCP may map ICMP packet-filtered to EHOSTUNREACH. Do not accept that
 # errno alone: require a local namespace-generated filtered/admin response.
 r=subprocess.run(['/usr/bin/ping','-n','-c','1','-W','2',ip],capture_output=True,timeout=5)
 text=(r.stdout+r.stderr).decode(errors='replace').lower()
 return r.returncode!=0 and ('from 169.254.253.2' in text or 'from fd6c:636e:1::2' in text) and ('packet filtered' in text or 'administratively prohibited' in text)
check('ordinary UID',os.getuid()==p['uid'],str(os.getuid()))
ns=os.readlink('/proc/self/ns/net')
check('dedicated namespace',ns!=p['host_namespace'],ns)
status=Path('/proc/self/status').read_text()
check('no-new-privileges', 'NoNewPrivs:\t1' in status)
check('seccomp active','Seccomp:\t2' in status)
resolver=Path('/etc/resolv.conf').read_text()
check('private real DNS','127.0.0.53' not in resolver)
for name,kind,port in [('LAN high-port TCP',socket.SOCK_STREAM,p['tcp']),('LAN high-port UDP',socket.SOCK_DGRAM,p['udp'])]:
 try:echo(kind,port);check(name,True,str(port))
 except Exception as exc:check(name,False,type(exc).__name__+': '+str(exc))
try:
 with socket.create_connection((p['host'],22),timeout=5) as s:
  banner=s.recv(256)
  check('direct SSH banner',banner.startswith(b'SSH-2.0-'),banner.splitlines()[0].decode(errors='replace'))
except Exception as exc:check('direct SSH banner',False,type(exc).__name__)
r=subprocess.run(['/usr/bin/ssh-keyscan','-T','3','-p','22',p['host']],capture_output=True,timeout=15)
check('OpenSSH direct handshake',r.returncode==0 and bool(r.stdout.strip()),'exit='+str(r.returncode))
for ip,family in [('1.1.1.1',socket.AF_INET),('2606:4700:4700::1111',socket.AF_INET6)]:
 for kind,label in [(socket.SOCK_STREAM,'TCP'),(socket.SOCK_DGRAM,'UDP')]:
  try:
   with socket.socket(family,kind) as s:
    s.settimeout(3);s.connect((ip,443));s.sendall(b'denied-test')
    if kind==socket.SOCK_DGRAM:s.recv(128)
  except OSError as exc:check('foreign IPv'+str(4 if family==socket.AF_INET else 6)+' '+label+' kernel rejection',blocked_errno(ip,exc),'errno='+str(exc.errno))
  else:check('foreign '+ip+' '+label+' kernel rejection',False,'unexpected access')
 r=subprocess.run(['/usr/bin/ping','-n','-c','1','-W','2',ip],capture_output=True,timeout=5)
 output=(r.stdout+r.stderr).decode(errors='replace')
 check('foreign '+ip+' ping rejection',r.returncode!=0 and ('not permitted' in output.lower() or 'prohibited' in output.lower() or 'permission denied' in output.lower()),output[:250])
def tunnel(host,port):
 s=socket.create_connection(('127.0.0.1',18080),timeout=10)
 s.sendall(f'CONNECT {host}:{port} HTTP/1.1\r\n\r\n'.encode())
 head=b''
 while b'\r\n\r\n' not in head:
  data=s.recv(4096)
  if not data:break
  head+=data
 return s,head
try:
 s,head=tunnel(p['host'],p['tcp'])
 with s:
  assert head.startswith(b'HTTP/1.1 200')
  s.sendall(b'claude-cn-installed-test')
  check('proxy LAN arbitrary TCP port',s.recv(128)==b'claude-cn-installed-test',str(p['tcp']))
except Exception as exc:check('proxy LAN arbitrary TCP port',False,type(exc).__name__+': '+str(exc))
for host,port in [('1.1.1.1',443),('github.com',22)]:
 try:
  s,head=tunnel(host,port)
  s.close();check('proxy rejects '+host+':'+str(port),head.startswith(b'HTTP/1.1 403'),head.split(b'\r\n')[0].decode())
 except Exception as exc:check('proxy rejects '+host+':'+str(port),False,type(exc).__name__)
try:
 ip=socket.getaddrinfo('github.com',443,type=socket.SOCK_STREAM)[0][4][0]
 family=socket.AF_INET6 if ':' in ip else socket.AF_INET
 with socket.socket(family,socket.SOCK_STREAM) as s:
  s.settimeout(3);s.connect((ip,443))
except OSError as exc:check('allowlisted foreign IP still cannot connect directly',blocked_errno(ip,exc),'errno='+str(exc.errno))
else:check('allowlisted foreign IP still cannot connect directly',False,ip)
print(json.dumps({'uid':os.getuid(),'namespace':ns,'checks':checks},ensure_ascii=False))
raise SystemExit(0 if all(c['ok'] for c in checks) else 1)
'''


def main():
    policy = json.loads(Path('/etc/claude-cn/policy.json').read_text())
    nets = [ipaddress.ip_network(n) for n in policy['allow_cidrs']]
    interfaces = json.loads(subprocess.check_output(['ip', '-j', '-4', 'addr'], text=True))
    addresses = [a['local'] for i in interfaces for a in i.get('addr_info', [])
                 if any(ipaddress.ip_address(a['local']) in n for n in nets if n.version == 4)]
    host = next((ip for ip in addresses if ip.startswith('192.168.8.')), addresses[0])
    settings = Path.home()/'.config/claude-cn/settings.json'
    before = hashlib.sha256(settings.read_bytes()).digest()
    stopped = threading.Event()
    sockets = []

    def serve(sock, kind):
        sock.settimeout(.2)
        while not stopped.is_set():
            try:
                if kind == socket.SOCK_DGRAM:
                    data, peer = sock.recvfrom(1024)
                    sock.sendto(data, peer)
                else:
                    conn, _ = sock.accept()
                    with conn:
                        conn.settimeout(3)
                        conn.sendall(conn.recv(1024))
            except (socket.timeout, OSError):
                continue
    try:
        ports = []
        for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
            sock = socket.socket(socket.AF_INET, kind)
            sock.bind((host, 0))
            if kind == socket.SOCK_STREAM:
                sock.listen(5)
            sockets.append(sock)
            ports.append(sock.getsockname()[1])
            threading.Thread(target=serve, args=(sock, kind), daemon=True).start()
        payload = {'host': host, 'tcp': ports[0], 'udp': ports[1], 'uid': os.getuid(),
                   'host_namespace': os.readlink('/proc/self/ns/net')}
        with tempfile.TemporaryDirectory(prefix='claude-cn-installed-') as tmp:
            entry = Path(tmp)/'claude'
            entry.write_text('#!/usr/bin/python3\n' + RUNNER)
            entry.chmod(0o700)
            result = subprocess.run(['/usr/bin/claude-cn', '--installed-network-probe', json.dumps(payload)],
                                    env={**os.environ, 'PATH': tmp + ':' + os.environ['PATH']},
                                    text=True, capture_output=True, timeout=60)
        if not result.stdout.strip():
            raise RuntimeError('probe did not run: ' + result.stderr[:1000])
        report = json.loads(result.stdout.strip().splitlines()[-1])
        report['configuration_unchanged'] = before == hashlib.sha256(settings.read_bytes()).digest()
        report['host_namespace'] = payload['host_namespace']
        for check in report['checks']:
            print(('PASS ' if check['ok'] else 'FAIL ') + check['name'] + ': ' + check['detail'])
        print('User provider configuration unchanged:', report['configuration_unchanged'])
        out = ROOT/'reports/installed-network.json'
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        raise SystemExit(result.returncode or (0 if report['configuration_unchanged'] else 1))
    finally:
        stopped.set()
        for sock in sockets:
            sock.close()


if __name__ == '__main__':
    main()
