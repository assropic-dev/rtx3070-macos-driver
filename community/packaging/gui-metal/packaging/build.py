"""Build and test the local Aqua environment preference installers without installing them."""
import hashlib
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

HERE=Path(__file__).resolve().parent
WORK=HERE.parents[2]
POLICY=HERE.parent/"session-defaults.sh"
LABEL="local.nullmoth.angle-metal-defaults"
NAMES={"apply":"全局Metal偏好-安装.pkg", "undo":"全局Metal偏好-撤销.pkg"}

def run(*args,**kw):
    return subprocess.run([str(x) for x in args],check=True,**kw)

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    policy=POLICY.read_text()
    for name in NAMES.values():
        if (WORK/name).exists():
            raise FileExistsError(WORK/name)
    if sha(POLICY)!="41f3e5dd333d9ece5e868dfaad3f5f560fce476f61eb9681d9452e0b9f9841c7":
        raise ValueError("Session policy changed; review before building")
    with tempfile.TemporaryDirectory(prefix="build-",dir=HERE) as temp:
        tmp=Path(temp)
        native=tmp/"guard"
        run("/usr/bin/xcrun","clang","-arch","x86_64","-arch","arm64","-mmacosx-version-min=11.0",
            "-O2","-Wall","-Wextra","-Werror",HERE/"guard.c","-o",native)
        run("/usr/bin/codesign","--force","--sign","-","--timestamp=none",native)
        run("/usr/bin/codesign","--verify","--strict",native)
        marker="# "+LABEL+" policy "+sha(POLICY)
        agent={"Label":LABEL,"RunAtLoad":True,"LimitLoadToSessionType":"Aqua",
               "ProgramArguments":["/bin/sh","-c",marker+"\n"+policy,LABEL,"apply"]}
        print("Embedded policy",sha(POLICY),flush=True)
        outputs={}
        for mode,name in NAMES.items():
            scripts=tmp/mode
            scripts.mkdir()
            plist=scripts/(LABEL+".plist")
            plist.write_bytes(plistlib.dumps(agent,sort_keys=False))
            run("/usr/bin/plutil","-lint",plist)
            (scripts/"policy-marker").write_text(marker+"\n")
            shutil.copyfile(POLICY,scripts/"session-defaults.sh")
            shutil.copy2(native,scripts/"guard")
            for path in ("transaction.sh","sessions.sh"):
                shutil.copyfile(HERE/path,scripts/path)
                (scripts/path).chmod(0o755)
            sums=scripts/"resources.sha256"
            sums.write_text("".join(f"{sha(p)}  {p.name}\n" for p in sorted(scripts.iterdir())))
            post=scripts/"postinstall"
            post.write_text(f"""#!/bin/sh
set -eu
export PATH=/usr/bin:/bin:/usr/sbin:/sbin LC_ALL=C
[ "$(/usr/bin/id -u)" = 0 ] && [ "${{3:-/}}" = / ] || {{ echo 'Select the running system volume.' >&2; exit 1; }}
here=$(cd "$(dirname "$0")" && pwd -P)
cd "$here"
printf '%s  resources.sha256\\n' {sha(sums)} | /usr/bin/shasum -a 256 --strict --quiet -c -
/usr/bin/shasum -a 256 --strict --quiet -c resources.sha256
exec /usr/bin/env -i PATH="$PATH" LC_ALL=C "$here/guard" locked /Library/LaunchAgents \
    "$here/transaction.sh" {mode} "$here" /Library/LaunchAgents "$here/sessions.sh" 0
""")
            post.chmod(0o755)
            for shell in [post,scripts/"sessions.sh",scripts/"session-defaults.sh"]:
                run("/bin/sh","-n",shell)
            run("/bin/bash","-n",scripts/"transaction.sh")
            package=tmp/name
            ident=LABEL+"."+mode
            run("/usr/bin/pkgbuild","--nopayload","--scripts",scripts,"--identifier",ident,
                "--version","1.0","--install-location","/",package)
            expanded=tmp/("expanded-"+mode)
            run("/usr/sbin/pkgutil","--expand-full",package,expanded)
            metadata=ET.parse(expanded/"PackageInfo").getroot()
            if metadata.get("identifier")!=ident or metadata.get("version")!="1.0" or metadata.find("payload") is not None:
                raise ValueError("Unexpected package structure")
            items=expanded/"Scripts"
            if {p.name:sha(p) for p in scripts.iterdir()}!={p.name:sha(p) for p in items.iterdir()}:
                raise ValueError("Packaging changed transaction resources")
            run("/usr/bin/shasum","-a","256","--strict","--quiet","-c","resources.sha256",cwd=items)
            if plistlib.loads((items/plist.name).read_bytes())!=agent:
                raise ValueError("Incorrect agent after expansion")
            outputs[mode]=package
        run(sys.executable,"-B",HERE/"exercise.py",tmp)
        for mode,package in outputs.items():
            package.chmod(0o644)
            dest=WORK/NAMES[mode]
            os.link(package,dest)
            print(f"{dest}  SHA-256 {sha(dest)}",flush=True)

if __name__=="__main__":
    main()
