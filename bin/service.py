#!/usr/bin/python3 -I
import sys
sys.path.insert(0, '/usr/lib/claude-cn')
if sys.argv[1:2] == ['proxy']:
    del sys.argv[1]
    from claude_cn.proxy import main
elif sys.argv[1:2] == ['update']:
    del sys.argv[1]
    from claude_cn.update import main
elif sys.argv[1:2] == ['network']:
    del sys.argv[1]
    from claude_cn.network import main
elif sys.argv[1:2] == ['launch']:
    del sys.argv[1]
    from claude_cn.broker import main
else:
    raise SystemExit('expected proxy, update, network or launch')
main()
