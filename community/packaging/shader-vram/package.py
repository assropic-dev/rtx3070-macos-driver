"""Package and verify the local RTX 3070 shader heap update."""
import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

work = Path(__file__).resolve().parent
workspace = work.parents[1]
name = "libvulkan_nouveau.dylib"
baseline = work.parent / "staging/pkgroot/Library/GPUBundles/nvmtl" / name
candidate = work / name
old = "e614521bd06d1c17a1996db977e7faec6749c0fc7da7dc55f0d79dafbad10e52"
new = "4e56026f67822a3b75959f061ee209d0f72d5f968eb2c935bf8fa5f96b887e30"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(*args, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def package(temp, mode, incoming, before, after, exchange):
    scripts = temp / mode
    scripts.mkdir()
    for path, target in [(incoming, name), (work / "install.sh", "install.sh"),
                         (exchange, "exchange")]:
        shutil.copy2(path, scripts / target)
    (scripts / "before").write_text(before + "\n")
    (scripts / "after").write_text(after + "\n")
    manifest = scripts / "resources.sha256"
    manifest.write_text("".join(f"{digest(path)}  ./{path.name}\n" for path in sorted(scripts.iterdir())))
    post = scripts / "postinstall"
    post.write_text(f"""#!/bin/bash
set -eu
umask 077
export PATH=/usr/bin:/bin:/usr/sbin:/sbin LC_ALL=C
[ "$(id -u)" = 0 ] && [ "${{3:-/}}" = / ] || {{ echo "Select the running macOS system volume." >&2; exit 1; }}
here=$(cd "$(dirname "$0")" && pwd -P)
cd "$here"
printf '%s  resources.sha256\\n' {digest(manifest)} | shasum -a 256 --strict --quiet -c -
shasum -a 256 --strict --quiet -c resources.sha256
exec "$here/exchange" with-lock /Library/GPUBundles "$here/install.sh" "$here" /Library/GPUBundles /Library/NullMoth
""")
    post.chmod(0o755)
    for path in (post, scripts / "install.sh"):
        run("/bin/bash", "-n", path)
    pkg = temp / f"RTX3070-Shader-VRAM-{'local' if mode == 'update' else mode}.pkg"
    run("/usr/bin/pkgbuild", "--nopayload", "--scripts", scripts, "--identifier",
        f"local.dewei.rtx3070.shader-vram.{mode}", "--version", "1.0.0",
        "--install-location", "/", pkg)
    expanded = temp / f"{mode}-expanded"
    run("/usr/sbin/pkgutil", "--expand-full", pkg, expanded)
    installed_scripts = expanded / "Scripts"
    if digest(installed_scripts / "postinstall") != digest(post):
        raise ValueError("Packaged postinstall differs from staging")
    run("/usr/bin/shasum", "-a", "256", "--strict", "--quiet", "-c",
        "resources.sha256", cwd=installed_scripts)
    return pkg, installed_scripts


def exercise(temp, forward, rollback):
    system = temp / "mock-system"
    parent, state = system / "GPUBundles", system / "NullMoth"
    (parent / "nvmtl").mkdir(parents=True)
    state.mkdir()
    live = parent / "nvmtl" / name
    shutil.copy2(baseline, live)

    def apply(scripts, accepted=True):
        result = subprocess.run([scripts / "exchange", "with-lock", parent, scripts / "install.sh",
                                 scripts, parent, state], capture_output=True, text=True)
        if (result.returncode == 0) != accepted:
            raise RuntimeError(result.stdout + result.stderr)

    apply(forward)
    assert digest(live) == new
    assert digest(state / "shader-vram-backups" / f"{old}.dylib") == old
    apply(forward)
    apply(rollback)
    assert digest(live) == old
    assert digest(state / "shader-vram-backups" / f"{new}.dylib") == new
    apply(rollback)
    guard = state / "install.lock"
    guard.mkdir()
    apply(forward, False)
    (guard / "owner").write_text("pid=999999\nkind=nvk-shader-vram-v1\n")
    apply(forward)
    apply(rollback)
    live.write_text("unknown NVK build")
    apply(forward, False)
    assert live.read_text() == "unknown NVK build"


if __name__ == "__main__":
    for library, expected in [(baseline, old), (candidate, new)]:
        if digest(library) != expected:
            raise ValueError(f"Unexpected input SHA-256: {library}")
        run("/usr/bin/codesign", "--verify", "--strict", library)
    with tempfile.TemporaryDirectory(prefix="pkg-", dir=work) as dirname:
        temp = Path(dirname)
        exchange = temp / "exchange"
        run("/usr/bin/xcrun", "clang", "-O2", "-mmacosx-version-min=15.0",
            work.parent / "ws-upload/packaging/exchange.c", "-o", exchange)
        update_pkg, update = package(temp, "update", candidate, old, new, exchange)
        rollback_pkg, rollback = package(temp, "rollback", baseline, new, old, exchange)
        exercise(temp, update, rollback)
        for pkg in (update_pkg, rollback_pkg):
            target = workspace / pkg.name
            shutil.copyfile(pkg, target)
            print(digest(target), target)
        print("Verified update, rollback, repeat install, backup, lock, and mismatched driver rejection.")
