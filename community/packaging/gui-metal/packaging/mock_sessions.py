#!/usr/bin/python3
"""Only used by the unprivileged temporary-volume test, never included in a pkg."""
import os
from pathlib import Path
import plistlib
import subprocess
import sys

root=Path(os.environ["TEST_GUI_SESSIONS"])
operation=sys.argv[1]
if operation=="uids":
    print(*sorted(p.name for p in root.iterdir() if p.is_dir()));sys.exit()
session=root/sys.argv[2]
if operation=="exists": sys.exit(0 if session.is_dir() else 1)
if operation=="disabled": sys.exit(0 if (session/"disabled").exists() else 1)
job=session/"job.plist"

def policy(source,mode):
    if source.count("ctl=/bin/launchctl")!=1:
        raise ValueError("Only the session's launchctl endpoint is replaced in the policy test")
    source=source.replace("ctl=/bin/launchctl",'ctl="$TEST_GUI_SESSIONS/launchctl"')
    env=dict(os.environ,TEST_GUI=str(session))
    result=subprocess.run(["/bin/sh","-c",source,"test-session",mode],env=env)
    (session/"last-result").write_text(str(result.returncode))
    return result.returncode

if operation=="inspect":
    if not job.exists(): sys.exit(1)
    plist=plistlib.loads(job.read_bytes())
    print("path = "+(session/"path").read_text())
    print(plist["ProgramArguments"][2]);sys.exit()
if operation=="load":
    path=Path(sys.argv[3])
    job.write_bytes(path.read_bytes())
    (session/"path").write_text(str(path))
if operation in ("load","start"):
    plist=plistlib.loads(job.read_bytes())
    assert plist["RunAtLoad"] and plist["LimitLoadToSessionType"]=="Aqua"
    exe,option,script,_,mode=plist["ProgramArguments"]
    assert (exe,option,mode)==("/bin/sh","-c","apply")
    policy(script,"apply");sys.exit()
if operation=="wait": sys.exit(int((session/"last-result").read_text()))
if operation=="unload": job.unlink();sys.exit()
if operation=="undo": sys.exit(policy(Path(sys.argv[3]).read_text(),"undo"))
sys.exit(2)
