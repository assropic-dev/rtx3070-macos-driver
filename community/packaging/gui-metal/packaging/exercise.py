"""Exercise the actual embedded transaction, policy, resources and lock against simulated GUI sessions."""
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys

tmp=Path(sys.argv[1]); here=Path(__file__).resolve().parent
folder=tmp/"test-system/Library/LaunchAgents"
folder.mkdir(mode=0o755,parents=True)
live=folder/"local.nullmoth.angle-metal-defaults.plist"
root=tmp/"test-sessions"; root.mkdir()
env=dict(os.environ,TEST_GUI_SESSIONS=str(root))
manager=tmp/"mock-manager";shutil.copyfile(here/"mock_sessions.py",manager);manager.chmod(0o755)
ctl=root/"launchctl"
ctl.write_text("""#!/bin/sh
set -eu
case "$1" in
managername) echo Aqua ;;
getenv) [ ! -f "$TEST_GUI/env-$2" ] || cat "$TEST_GUI/env-$2" ;;
setenv) printf '%s' "$3" > "$TEST_GUI/env-$2" ;;
unsetenv) rm -f "$TEST_GUI/env-$2" ;;
*) exit 2 ;;
esac
""")
ctl.chmod(0o755)
def call(mode,ok=True):
    src=tmp/("expanded-"+mode)/"Scripts"
    cmd=[src/"guard","locked",folder,src/"transaction.sh",mode,src,folder,manager,str(os.getuid())]
    r=subprocess.run(cmd,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    if (r.returncode==0)!=ok:raise RuntimeError(r.stdout)

def create(uid,**defaults):
    s=root/str(uid);s.mkdir()
    for k,v in defaults.items():(s/("env-"+k)).write_text(v)
    return s
def state(s):return {p.name[4:]:p.read_text() for p in s.glob("env-*")}
p="ANGLE_DEFAULT_PLATFORM";d="NVMTL_ANGLE_METAL";m="NULLMOTH_GUI_METAL_DEFAULTS_V1"
a=create(501)
b=create(502,**{p:"metal",d:"1"})
c=create(503,**{p:"other",d:"0"})
e=create(504,**{p:"metal"})
f=create(505,**{"ANGLE_FEATURE_OVERRIDES_ENABLED":"disableMetalOnNvidia"})
g=create(506);(g/"disabled").touch()
call("apply")
assert live.is_file() and live.stat().st_mode&0o777==0o644
assert state(a)=={p:"metal",d:"1",m:"pd"}
assert state(b)=={p:"metal",d:"1"} and state(c)=={p:"other",d:"0"}
assert state(e)=={p:"metal",d:"1",m:"d"}
assert state(f)=={"ANGLE_FEATURE_OVERRIDES_ENABLED":"disableMetalOnNvidia"} and state(g)=={}
ino=live.stat().st_ino
call("apply")
assert live.stat().st_ino==ino and state(a)=={p:"metal",d:"1",m:"pd"}
(a/("env-"+d)).write_text("0")
call("undo")
assert not live.exists() and state(a)=={d:"0"} and state(b)=={p:"metal",d:"1"}
assert state(c)=={p:"other",d:"0"} and state(e)=={p:"metal"}
assert not any(s.is_dir() and (s/"job.plist").exists() for s in root.iterdir())
call("undo")
print("Passed multi-session install, repeated install/remove, pre-existing matching values, partial ownership, explicit opt-outs and later overrides.")

call("apply");live.unlink();call("undo")
assert not live.exists() and state(e)=={p:"metal"} and not (e/"job.plist").exists()
live.write_text("foreign plist")
call("apply",False);call("undo",False)
assert live.read_text()=="foreign plist"
live.unlink(); live.symlink_to(tmp/"missing")
call("apply",False);call("undo",False)
assert live.is_symlink();live.unlink()
call("apply")
saved=(a/"path").read_text();(a/"path").write_text("/elsewhere/foreign.plist")
call("undo",False)
assert live.is_file() and (a/"job.plist").exists()
(a/"path").write_text(saved);call("undo")
fd=os.open(str(folder),os.O_RDONLY)
try:
    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);call("apply",False)
finally: os.close(fd)
assert not live.exists()
for s in root.iterdir():
    if s.is_dir():shutil.rmtree(s)
call("apply");assert live.is_file()
call("undo");assert not live.exists() and not list(folder.iterdir())
print("Passed interrupted rollback, foreign files, symlink and loaded-label conflicts, concurrent transactions and no logged-in Aqua users.")
