#!/usr/bin/env python3
"""Build workstation-specific NVRMFB update and rollback packages without installing."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import xml.etree.ElementTree as ET


HERE = Path(__file__).resolve().parent
BOOT = Path("/System/Library/KernelCollections/BootKernelExtensions.kc")
SYSTEM = Path("/System/Library/KernelCollections/SystemKernelExtensions.kc")
LIVE_KC = Path("/Library/KernelCollections/AuxiliaryKernelExtensions.kc")
EXTENSIONS = Path("/Library/Extensions")
LIVE = EXTENSIONS / "NVRMFB.kext"
APPLE_MOBILE = Path("/Library/Apple/System/Library/Extensions/AppleMobileDevice.kext")
EXE = Path("Contents/MacOS/NVRMFB")
INFO = Path("Contents/Info.plist")
EXCHANGE = HERE.parent / "ws-spaces/packaging/exchange.c"
OLD_EXECUTABLE = "bc2f58c7261b2aac3da8a3cecb27e65b031acc1997f3cc9456a13c2958be792c"
OLD_COLLECTION = "cc85c6adeedf7bcd091f0e528775776c61c2ed2c5495b33d4a3c4ab4126f298a"
OLD_UUID = "A036ACEF-A64E-3A3F-ACD1-1FACDC0E67A4"
ID = "com.nullmoth.NVRMFB"
KMUTIL = "/usr/bin/kmutil"
ENV = dict(os.environ, LC_ALL="C")


def run(*args, capture=False, **kwargs):
    return subprocess.run(
        list(map(str, args)), check=True, text=True, capture_output=capture, env=ENV, **kwargs
    )


def digest(path):
    with path.open("rb") as stream:
        sha = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(chunk)
        return sha.hexdigest()


def inventory(collection, require_target=True):
    record = run(KMUTIL, "inspect", "-a", "x86_64", "-A", collection, "--show-extension-info",
                 "--show-kext-uuids", capture=True).stdout
    result = {}
    for line in record.splitlines():
        fields = line.replace(r"\t", "\t").split("\t")
        if len(fields) != 4:
            continue
        identifier, version, path, uuid = fields
        if identifier in result:
            raise ValueError(f"Duplicate kernel-collection identifier {identifier}")
        result[identifier] = (version, path, uuid)
    if require_target and ID not in result:
        raise ValueError("The collection does not contain NVRMFB")
    return result, record


def snapshot(directory, skip=None):
    paths = ["."]
    files = {}
    for path in directory.rglob("*"):
        relative = path.relative_to(directory)
        if skip and relative.parts[0] == skip:
            continue
        name = "./" + relative.as_posix()
        if "\n" in name or "\r" in name or "\\" in name:
            raise ValueError(f"Unsupported bundle path {path}")
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError(f"Unexpected special file {path}")
        paths.append(name)
        if path.is_file():
            files[name] = digest(path)
    return "".join(item + "\n" for item in sorted(paths)), files


def manifest(files):
    return "".join(f"{files[path]}  {path}\n" for path in sorted(files))


def render_members(members):
    return "".join("\t".join((name, *members[name])) + "\n" for name in sorted(members))


def uuid(bundle):
    output = run("xcrun", "dwarfdump", "--uuid", bundle / EXE, capture=True).stdout.split(maxsplit=3)
    if len(output) != 4 or output[0] != "UUID:" or output[2] != "(x86_64)":
        raise ValueError(f"Expected one x86_64 image in {bundle}")
    return output[1]


def preflight(candidate, output, old_members, expected):
    args = [
        argument
        for kext in [*sorted(EXTENSIONS.glob("*.kext")), APPLE_MOBILE]
        for argument in ("-p", candidate if kext == LIVE else kext)
    ]
    collection = output / "candidate-shadow.kc"
    with (output / "candidate-shadow.log").open("w") as log:
        run(KMUTIL, "create", "-n", "aux", "--volume-root", "/", "--allow-missing-kdk", "-x",
            "-B", BOOT, "-S", SYSTEM, *args, "-A", collection, stdout=log, stderr=subprocess.STDOUT)
    members, report = inventory(collection, require_target=False)
    (output / "candidate-shadow-inspect.txt").write_text(report)
    others = {name: row for name, row in old_members.items() if name != ID}
    if ID in members and members[ID][1] == str(candidate):
        version, _, image_uuid = members[ID]
        members[ID] = (version, str(LIVE), image_uuid)
    if members != expected:
        diagnostic = run("/usr/bin/strings", "-a", collection, capture=True).stdout
        pending_root = (members == others and candidate.stat().st_uid != 0 and
                        "Invalid ownership" in diagnostic and str(candidate) in diagnostic)
        if not pending_root:
            raise ValueError("Shadow collection did not preserve the expected member identities")
        status = "unprivileged shadow retained the seven neighbors; root-owned candidate rebuild required"
    else:
        status = "candidate UUID and all seven neighbors verified in shadow"
    (output / "baseline.members").write_text(render_members(old_members))
    (output / "candidate.members").write_text(render_members(members))
    return collection, status


def build_one(stage, label, package_data, source, exchange):
    scripts = stage / label
    scripts.mkdir()
    run("/usr/bin/ditto", source, scripts / "NVRMFB.kext")
    shutil.copy2(exchange, scripts / "exchange")
    shutil.copy2(HERE / "install.sh", scripts / "install.sh")
    for name, value in package_data.items():
        (scripts / name).write_text(value)
    if label == "rollback":
        shutil.copyfile(LIVE_KC, scripts / "rollback.kc")
        if digest(scripts / "rollback.kc") != OLD_COLLECTION:
            raise ValueError("Original live collection changed during copy")
    integrity = scripts / "resources.sha256"
    _, files = snapshot(scripts)
    integrity.write_text(manifest(files))
    post = scripts / "postinstall"
    post.write_text('''#!/bin/bash
set -euo pipefail
umask 077
export PATH=/usr/bin:/bin:/usr/sbin:/sbin LC_ALL=C
[ "$(id -u)" = 0 ] && [ "${3:-}" = / ] || { echo "Select this running system volume." >&2; exit 1; }
here=$(cd "$(dirname "$0")" && pwd -P)
cd "$here"
printf '%s  resources.sha256\\n' RESOURCE_HASH | shasum -a 256 --strict --quiet -c -
shasum -a 256 --strict --quiet -c resources.sha256
exec "$here/exchange" with-lock /Library/NullMoth "$here/install.sh" "$here" ACTION \
    /Library/Extensions /Library/KernelCollections /Library/NullMoth \
    /System/Library/KernelCollections/BootKernelExtensions.kc \
    /System/Library/KernelCollections/SystemKernelExtensions.kc /usr/bin/kmutil
'''.replace("RESOURCE_HASH", digest(integrity)).replace("ACTION", label))
    post.chmod(0o755)
    run("/bin/bash", "-n", post)
    run("/bin/bash", "-n", scripts / "install.sh")
    identifier = "local.dewei.rtx3070.nvrmfb.drawable-pool." + label
    suffix = "install" if label == "update" else "rollback"
    pkg = stage / f"RTX3070-NVRMFB-DrawablePool-3.0.2-{suffix}.pkg"
    run("/usr/bin/pkgbuild", "--nopayload", "--scripts", scripts, "--identifier", identifier,
        "--version", "3.0.2", "--install-location", "/", pkg)
    expanded = stage / ("expanded-" + label)
    run("/usr/sbin/pkgutil", "--expand-full", pkg, expanded)
    info = ET.parse(expanded / "PackageInfo").getroot()
    if info.get("identifier") != identifier or info.find("payload") is not None:
        raise ValueError(f"Unexpected {label} metadata")
    packed = expanded / "Scripts"
    if snapshot(packed) != snapshot(scripts):
        raise ValueError(f"Packaged {label} contents changed")
    run("/usr/bin/shasum", "-a", "256", "--strict", "--quiet", "-c", "resources.sha256", cwd=packed)
    return pkg, packed


def rehearse(stage, forward, inverse, package_data):
    """Exercise exact file swapping and recovery against disposable copies."""
    mock = stage / "test-system"
    ext, collections, state = [mock / name for name in ("Extensions", "KernelCollections", "NullMoth")]
    for folder in (ext, collections, state):
        folder.mkdir(parents=True)
    for kext in EXTENSIONS.glob("*.kext"):
        run("/usr/bin/ditto", kext, ext / kext.name)
    kc = collections / LIVE_KC.name
    shutil.copyfile(LIVE_KC, kc)
    kc.chmod(stat.S_IMODE(LIVE_KC.stat().st_mode))
    fixture_kmutil = mock / "kmutil"
    fixture_kmutil.write_text('''#!/bin/bash
set -eu
if [ "$1" = create ]; then
    while [ "$1" != -A ]; do shift; done
    if [ "${2##*/}" = next.kc ] && [ -e "$(dirname "$0")/fail-canonical" ]; then exit 27; fi
    if [ "${2##*/}" = shadow.kc ] && [ -e "$(dirname "$0")/fail-shadow" ]; then exit 27; fi
    printf 'simulated collection\\n' > "$2"
else
    cat "$(dirname "$0")/new.members"
fi
''')
    fixture_kmutil.chmod(0o755)
    (mock / "new.members").write_text(package_data["new.members"])
    live = ext / LIVE.name

    def apply(pack, direction, succeeds=True):
        args = [pack / "exchange", "with-lock", state, pack / "install.sh",
                pack, direction, ext, collections, state, BOOT, SYSTEM, fixture_kmutil]
        result = subprocess.run(list(map(str, args)), text=True, capture_output=True, env=ENV)
        if (result.returncode == 0) != succeeds:
            raise ValueError(f"{direction} rehearsal unexpected status: {result.stdout}{result.stderr}")
        return result.stdout + result.stderr

    (mock / "fail-shadow").touch()
    failure = apply(forward, "update", succeeds=False)
    if "No installed file was replaced" not in failure:
        raise ValueError(f"Shadow preflight failure did not gate live exchange: {failure}")
    if digest(live / EXE) != OLD_EXECUTABLE or digest(kc) != OLD_COLLECTION:
        raise ValueError("Shadow preflight failure touched installed artifacts")
    (mock / "fail-shadow").unlink()
    (mock / "fail-canonical").touch()
    failure = apply(forward, "update", succeeds=False)
    if "Original kext and collection restored" not in failure:
        raise ValueError(f"Failure recovery did not run: {failure}")
    if digest(live / EXE) != OLD_EXECUTABLE or digest(kc) != OLD_COLLECTION:
        raise ValueError("Failed transaction did not restore exact originals")
    (mock / "fail-canonical").unlink()
    for direction, pack in (("update", forward), ("rollback", inverse)):
        apply(pack, direction)
        if "already staged" not in apply(pack, direction):
            raise ValueError(f"{direction} was not idempotent")
        if snapshot(live) != snapshot(pack / LIVE.name):
            raise ValueError(f"{direction} did not stage the exact signed bundle")
    # Model termination between individual atomic exchanges in either direction.
    for direction, unpacked in (("update", forward), ("rollback", inverse)):
        interrupted = state / "install.lock"
        interrupted.mkdir()
        (interrupted / "owner").write_text("pid=0\nkind=nvrmfb-atomic-v1\n")
        shutil.rmtree(live)
        run("/usr/bin/ditto", unpacked / LIVE.name, live)
        apply(unpacked, direction)
        if "already staged" not in apply(unpacked, direction):
            raise ValueError(f"{direction} did not resume a partial previous exchange")
    if digest(kc) != OLD_COLLECTION or (state / "install.lock").exists():
        raise ValueError("Rollback rehearsal did not restore the exact collection and release the guard")


def build(candidate, output):
    output.mkdir(mode=0o700)
    for artifact in (LIVE, candidate):
        run("/usr/bin/codesign", "--verify", "--deep", "--strict", artifact)
    if digest(LIVE / EXE) != OLD_EXECUTABLE or digest(LIVE_KC) != OLD_COLLECTION or uuid(LIVE) != OLD_UUID:
        raise ValueError("The current system is not the verified baseline")
    if (candidate / INFO).read_bytes() != (LIVE / INFO).read_bytes():
        raise ValueError("NVRMFB Info.plist or dependencies changed")
    dirs, old_files = snapshot(LIVE)
    new_dirs, new_files = snapshot(candidate)
    if new_dirs != dirs or set(old_files) != set(new_files) or uuid(candidate) == OLD_UUID:
        raise ValueError("Candidate must have the canonical bundle layout and a different image UUID")
    unaffected_files = set(old_files) - {"./" + EXE.as_posix(), "./Contents/_CodeSignature/CodeResources"}
    if any(old_files[k] != new_files[k] for k in unaffected_files):
        raise ValueError("Unexpected bundle contents changed")

    before, before_report = inventory(LIVE_KC)
    expected = before.copy()
    expected[ID] = ("3.0", str(LIVE), "(" + uuid(candidate) + ")")
    if before[ID] != ("3.0", str(LIVE), "(" + OLD_UUID + ")"):
        raise ValueError("The installed auxiliary collection does not contain the expected original kext")
    (output / "live-baseline-inspect.txt").write_text(before_report)
    shadow, shadow_status = preflight(candidate, output, before, expected)
    other_paths, other_files = snapshot(EXTENSIONS, LIVE.name)
    apple_paths, apple_files = snapshot(APPLE_MOBILE)
    data = {
        "kext.paths": dirs, "old.kext.sha256": manifest(old_files), "new.kext.sha256": manifest(new_files),
        "new-executable.sha": digest(candidate / EXE) + "\n", "old-kc.sha": OLD_COLLECTION + "\n",
        "boot-kc.sha": digest(BOOT) + "\n", "system-kc.sha": digest(SYSTEM) + "\n",
        "neighbors.paths": other_paths, "neighbors.sha256": manifest(other_files),
        "apple-mobile.paths": apple_paths, "apple-mobile.sha256": manifest(apple_files),
        "os-build": run("sw_vers", "-buildVersion", capture=True).stdout,
        "new.members": render_members(expected),
    }
    with tempfile.TemporaryDirectory(dir=output) as tmp:
        stage = Path(tmp)
        exchange = stage / "exchange"
        run("xcrun", "clang", "-O2", "-Wall", "-Wextra", "-Werror", EXCHANGE, "-o", exchange)
        update, forward = build_one(stage, "update", data, candidate, exchange)
        rollback, inverse = build_one(stage, "rollback", data, LIVE, exchange)
        rehearse(stage, forward, inverse, data)
        if digest(LIVE / EXE) != OLD_EXECUTABLE or digest(LIVE_KC) != OLD_COLLECTION:
            raise ValueError("The live artifacts changed during the build")
        paths = []
        for path in (update, rollback):
            record = {"name": path.name, "sha256": digest(path)}
            path.rename(output / path.name)
            paths.append(record)
    report = {"packages": paths, "candidate_uuid": uuid(candidate), "baseline_uuid": OLD_UUID,
              "shadow_sha256": digest(shadow), "retained_collection_members": len(before) - 1,
              "shadow_is_validation_only": True, "shadow_status": shadow_status,
              "isolated_rehearsal": "shadow gating, canonical creation recovery, update, rollback, interruption and idempotence passed"}
    (output / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build(args.candidate.resolve(strict=True), args.output.resolve())
