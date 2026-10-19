#!/usr/bin/python3
"""Probe the installed production broker with real pipes and a controlling PTY."""
import errno
import json
import os
from pathlib import Path
import select
import subprocess
import tempfile
import termios

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = r'''
import json,os,sys
from pathlib import Path
status=Path('/proc/self/status').read_text()
chain=[]; pid=os.getpid()
for _ in range(12):
 try:
  data=Path(f'/proc/{pid}/status').read_text()
  name=next(line.split(':',1)[1].strip() for line in data.splitlines() if line.startswith('Name:'))
  chain.append(name)
  pid=int(next(line.split(':',1)[1] for line in data.splitlines() if line.startswith('PPid:')))
  if not pid:break
 except OSError:break
try:
 fd=os.open('/dev/tty',os.O_RDWR);os.close(fd);ctty=True
except OSError:ctty=False
print(json.dumps({'uid':os.getuid(),'euid':os.geteuid(),'tty':os.isatty(0),'controlling_tty':ctty,
 'no_new_privileges':'NoNewPrivs:\t1' in status,'seccomp':'Seccomp:\t2' in status,
 'no_capabilities':int(next(line.split(':',1)[1].strip() for line in status.splitlines() if line.startswith('CapEff:')),16)==0,
 'namespace':os.readlink('/proc/self/ns/net'),'ancestors':chain}),flush=True)
print('LAUNCH_STDERR_OK',file=sys.stderr,flush=True)
raise SystemExit(23)
'''


def main():
    checks = []
    details = {}
    def check(name, ok):
        checks.append({'name': name, 'ok': bool(ok)})
        print(('PASS ' if ok else 'FAIL ') + name)
    with tempfile.TemporaryDirectory(prefix='claude-cn-launch-live-') as tmp:
        binary = Path(tmp)/'claude'
        binary.write_text('#!/usr/bin/python3\n' + PAYLOAD)
        binary.chmod(0o700)
        env = {**os.environ, 'PATH': tmp + ':' + os.environ['PATH']}
        for terminal in (False, True):
            label = 'PTY' if terminal else 'pipe'
            if terminal:
                master, slave = os.openpty()
                original = termios.tcgetattr(slave)
                process = subprocess.Popen(['/usr/bin/claude-cn'], stdin=slave, stdout=slave, stderr=slave, env=env)
                try:
                    code = process.wait(timeout=15)
                    chunks = []
                    while select.select([master], [], [], 0.2)[0]:
                        try:
                            chunk = os.read(master, 65536)
                        except OSError as exc:
                            if exc.errno != errno.EIO:
                                raise
                            break
                        if not chunk:
                            break
                        chunks.append(chunk)
                    stdout = b''.join(chunks).decode()
                    check('PTY restores terminal', original == termios.tcgetattr(slave))
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                    os.close(master)
                    os.close(slave)
            else:
                result = subprocess.run(['/usr/bin/claude-cn'], input='', capture_output=True, text=True, env=env, timeout=15)
                code, stdout = result.returncode, result.stdout
                check('pipe separate stderr', result.stderr.strip() == 'LAUNCH_STDERR_OK')
            data = json.loads(next(line for line in stdout.splitlines() if line.startswith('{')))
            details[label] = data
            check(label + ' exit status', code == 23)
            check(label + ' real and effective UID', data['uid'] == data['euid'] == os.getuid() != 0)
            check(label + ' no capabilities', data['no_capabilities'])
            check(label + ' inherited guard', data['no_new_privileges'] and data['seccomp'])
            check(label + ' network namespace', data['namespace'] != os.readlink('/proc/self/ns/net'))
            check(label + ' terminal mode', data['tty'] == data['controlling_tty'] == terminal)
            check(label + ' no sudo in ancestors', 'sudo' not in data['ancestors'])
    check('obsolete sudo helper removed', not Path('/usr/lib/claude-cn/enter').exists())
    check('obsolete sudoers removed', not Path('/etc/sudoers.d/claude-cn').exists())
    report = {'checks': checks, 'details': details}
    (ROOT/'reports').mkdir(exist_ok=True)
    (ROOT/'reports/installed-launch-0.3.0.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    raise SystemExit(0 if all(item['ok'] for item in checks) else 1)


if __name__ == '__main__':
    main()
