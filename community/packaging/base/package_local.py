#!/usr/bin/env python3
"""Rebuild the local RTX 3070 update package. Does not install the package."""
import gzip
import hashlib
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tarfile
import tempfile
import xml.etree.ElementTree as ET

BUILD = Path(__file__).resolve().parent
WORK = BUILD.parent
RELEASE = Path.home() / "Library/Application Support/NullMoth/nullmoth-nvidia-3.0.tar.gz"
RELEASE_SHA = "40f0390d0b7cda75637991879b09abb7f8f7e3e5db2e811196963b3a2cacbcad"
IDENTIFIER = "local.dewei.rtx3070.display"
OUTPUT = WORK / "RTX3070-Display-3.0-local.pkg"
PLUGIN = Path("Library/GPUBundles/NVMTLDriver.bundle")
ACCEL = Path("NVAccel.kext")
ALLOW = Path("Library/GPUBundles/nvmtl-allow.txt")


def run(*args, cwd=None, capture=False):
    return subprocess.run(list(map(str, args)), check=True, cwd=cwd,
                          stdout=subprocess.PIPE if capture else None,
                          text=True).stdout


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError("unexpected v3.0 installer at: " + old[:75])
    return text.replace(old, new, 1)


def between(text, start, end, replacement):
    if text.count(start) != 1 or text.count(end) != 1:
        raise ValueError("unexpected installer interval: " + start)
    first = text.index(start)
    last = text.index(end, first)
    return text[:first] + replacement + text[last:]


def local_installer(root):
    file = root / "install.sh"
    text = file.read_text()
    # This update needs the existing installation's assets and boot recovery.
    text = replace_once(text, 'step "1. this Mac"', '''step "1. this Mac"
[ "$(cat /Library/NullMoth/driver-version 2>/dev/null)" = 3.0 ] ||
    die "This local update requires an installed NullMoth 3.0 driver."
''')
    text = between(text, "# The audio driver.", "# The video codecs", "AUDIO_NOTE=\"\"\n\n")
    # The release pins are verified at package build and checked on the target.
    # This updater never replaces, downloads or removes installed GPU firmware.
    pins = re.findall(r"^\s*(\w+\.bin)\)\s+echo ([0-9a-f]{64});;", (root / "fw_fetch.sh").read_text(), re.M)
    if len(pins) != 4:
        raise ValueError("missing release firmware pins")
    firmware = '(cd "$FW/nvidia/620.70.06" && /usr/bin/shasum -a 256 --strict --quiet -c - <<\'FW_HASHES\'\n'
    firmware += "".join(digest + "  " + name + "\n" for name, digest in pins)
    firmware += 'FW_HASHES\n) || die "Installed 620.70.06 firmware does not match the release pins."\n\n'
    text = between(text, "# The GPU firmware is NVIDIA's", "BK=$(mktemp -d", firmware)
    text = between(text, "# The card runs these images", "# $GB/nvmtl is replaced wholesale", "")
    text = between(text, 'ditto "$FWSRC" "$FW"', "for k in $KEXTS; do chown", "")
    text = replace_once(text, 'cp "$HERE/Library/GPUBundles/nvmtl-allow.txt" "$GB/" || die "copy bundle allow list"',
                        '[ -f "$GB/nvmtl-allow.txt" ] && [ ! -L "$GB/nvmtl-allow.txt" ] || die "The existing regular allowlist is required."')
    # Leave the installed firmware and its old location alone during both rollback paths.
    text = text.replace('rm -rf /Library/NullMoth/kexts "$FW"', 'rm -rf /Library/NullMoth/kexts')
    text = between(text, '  # Restore the firmware to both locations:', '  if [ -f "$BK/AuxiliaryKernelExtensions.kc"', "")
    text = between(text, '[ ! -d "\\$BK/nvfw" ] || ditto', 'if [ -f "\\$BK/AuxiliaryKernelExtensions.kc"', "")
    # Reuse the upstream backup, lock, safe configuration merge and boot-guard code.
    text = between(text, "# An update replaces kexts", 'chown -R root:wheel "$GB/NVMTLDriver.bundle"', "")
    if text.count(' -z >"$T/kmutil') != 2:
        raise ValueError("unexpected kmutil create arguments")
    text = text.replace(' -z >"$T/kmutil', ' >"$T/kmutil')
    # Existing GPU, audio, BootGuard and firmware configuration outside the modified
    # bundles stays as installed. Only the original recovery jobs are refreshed.
    text = text[:text.index("# Panel brightness on firmware")] + '''
echo "RTX 3070 local update staged. If macOS requests approval, use Privacy & Security."
echo "The new kernel extensions take effect after a restart. Backup: $BK"
'''
    if "KextPolicy" in text or re.search(r"kmutil create[^\n]*(?: -z\b|--no-authorization)", text):
        raise ValueError("unexpected extension-policy override")
    file.write_text(text)
    run("/bin/bash", "-n", file)
    uninstall = root / "uninstall.sh"
    undo = replace_once(uninstall.read_text(), ' -A "$NEWKC" -z >', ' -A "$NEWKC" >')
    uninstall.write_text(undo)
    run("/bin/bash", "-n", uninstall)


def sign(path):
    run("/usr/bin/codesign", "--force", "--sign", "-", "--timestamp=none", path)
    run("/usr/bin/codesign", "--verify", "--strict", path)


def unsigned_content(path, temp):
    # codesign may round the __LINKEDIT VM span to the next page when adding a
    # signature. Compare every byte, including linkedit payload and the load
    # commands, after removing only the signature and normalizing this span.
    shutil.copyfile(path, temp)
    signed_size = temp.stat().st_size
    if subprocess.run(["/usr/bin/codesign", "-d", temp], capture_output=True).returncode == 0:
        run("/usr/bin/codesign", "--remove-signature", temp)
    data = bytearray(temp.read_bytes())
    if struct.unpack_from("<II", data) != (0xfeedfacf, 0x01000007):
        raise ValueError("not an x86_64 Mach-O: " + str(path))
    offset = 32
    for _ in range(struct.unpack_from("<I", data, 16)[0]):
        command, size = struct.unpack_from("<II", data, offset)
        if command == 0x19 and data[offset + 8:offset + 24].rstrip(b"\0") == b"__LINKEDIT":
            vmsize, fileoff, filesize = struct.unpack_from("<QQQ", data, offset + 32)
            # Removing a signature truncates the file. The larger allocated VM
            # mapping can remain rounded from the original signed file length.
            if not filesize <= vmsize <= (signed_size - fileoff + 16383) // 16384 * 16384:
                raise ValueError("invalid linkedit span: " + str(path))
            struct.pack_into("<Q", data, offset + 32, filesize)
        if size < 8:
            raise ValueError("invalid Mach-O load command")
        offset += size
    return hashlib.sha256(data).hexdigest()


def stage(root):
    run("/usr/bin/shasum", "-a", "256", "--strict", "--quiet", "-c", "SHA256SUMS", cwd=root)
    run("/bin/bash", root / "sums_complete.sh", root)
    for variant in ("15", "26"):
        bundle = root / "Library/NullMoth/kexts" / variant / ACCEL
        source = BUILD / "accel" / variant / ACCEL
        copy_bundle(source, bundle, "NVAccel")
    # In the release archive the top-level accelerator is the macOS 15 build.
    copy_bundle(BUILD / "accel/15" / ACCEL, root / "Library/Extensions" / ACCEL, "NVAccel")
    copy_bundle(BUILD / "fb/NVRMFB.kext", root / "Library/Extensions/NVRMFB.kext", "NVRMFB")
    contents = root / PLUGIN / "Contents/MacOS"
    for source in (BUILD / "plugin/NVMTLDriver", BUILD / "video/libnvencvt.dylib"):
        if run("/usr/bin/lipo", "-archs", source, capture=True).strip() != "x86_64":
            raise ValueError("incorrect architecture: " + str(source))
        shutil.copy2(source, contents / source.name)
    sign(contents / "libnvencvt.dylib")
    sign(root / PLUGIN)
    run("/usr/bin/codesign", "--verify", "--deep", "--strict", root / PLUGIN)
    binaries = [(BUILD / "plugin/NVMTLDriver", contents / "NVMTLDriver"),
                (BUILD / "video/libnvencvt.dylib", contents / "libnvencvt.dylib")]
    for variant in ("15", "26"):
        rel = ACCEL / "Contents/MacOS/NVAccel"
        binaries.append((BUILD / "accel" / variant / rel, root / "Library/NullMoth/kexts" / variant / rel))
    fb = Path("NVRMFB.kext/Contents/MacOS/NVRMFB")
    binaries.extend([(BUILD / "fb" / fb, root / "Library/Extensions" / fb),
                     (BUILD / "accel/15" / rel, root / "Library/Extensions" / rel)])
    with tempfile.TemporaryDirectory(dir=BUILD) as tmp:
        for source, installed in binaries:
            if unsigned_content(source, Path(tmp) / "input") != unsigned_content(installed, Path(tmp) / "staged"):
                raise ValueError("signed executable differs from built input: " + str(source))
    shutil.copyfile(Path("/") / ALLOW, root / ALLOW)
    conf = root / "Library/GPUBundles/nvmtl/nvrm610.conf"
    text = conf.read_text()
    if "NVMTL_NVDEC_VRAM=" in text:
        raise ValueError("unexpected baseline NVDEC setting")
    conf.write_text(text.rstrip("\n") + "\nNVMTL_NVDEC_VRAM=1\n")
    local_installer(root)
    # Keep the upstream version file for ABI and upgrade matching. PackageKit
    # records the separate local receipt and the installer displays its identity.
    sums = root / "SHA256SUMS"
    records = [f"{sha(p)}  ./{p.relative_to(root).as_posix()}\n" for p in sorted(root.rglob("*"))
               if not p.is_symlink() and p.is_file() and p != sums]
    sums.write_text("".join(records))
    run("/usr/bin/shasum", "-a", "256", "--strict", "--quiet", "-c", "SHA256SUMS", cwd=root)
    run("/bin/bash", root / "sums_complete.sh", root)
    run("/bin/bash", WORK / "nvidia-macos-driver/package/check_package.sh", root)
    return sha(sums)


def copy_bundle(source, dest, executable):
    binary = Path("Contents/MacOS") / executable
    if run("/usr/bin/lipo", "-archs", source / binary, capture=True).strip() != "x86_64":
        raise ValueError("incorrect architecture: " + str(source))
    # Release packaging stamps its version into the source-tree 0.1 plists.
    from plistlib import loads
    compiled, published = (loads((p / "Contents/Info.plist").read_bytes()) for p in (source, dest))
    for key in ("CFBundleVersion", "CFBundleShortVersionString"):
        if compiled.get(key) not in ("0.1", "3.0") or published.get(key) != "3.0":
            raise ValueError("unexpected kext version: " + str(source))
        compiled[key] = published[key]
    if compiled != published:
        raise ValueError("unmatched bundle metadata: " + str(source))
    shutil.copy2(source / binary, dest / binary)
    sign(dest)


def main():
    if sha(RELEASE) != RELEASE_SHA:
        raise ValueError("NullMoth 3.0 archive does not match the published digest")
    with tempfile.TemporaryDirectory(prefix="local-driver-package-", dir=BUILD) as tmp:
        working = Path(tmp)
        run("/usr/bin/tar", "-xzf", RELEASE, "-C", working)
        root = working / "pkgroot"
        manifest = stage(root)
        scripts = working / "scripts"
        scripts.mkdir()
        payload = scripts / "local-driver.tar.gz"
        with payload.open("wb") as out, gzip.GzipFile(filename="", fileobj=out, mode="wb", mtime=0, compresslevel=3) as gzip_stream:
            with tarfile.open(fileobj=gzip_stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
                def normalize(info):
                    info.uid, info.gid, info.uname, info.gname, info.mtime = 0, 0, "root", "wheel", 0
                    return info
                archive.add(root, arcname="pkgroot", filter=normalize)
        payload_hash = sha(payload)
        postinstall = scripts / "postinstall"
        postinstall.write_text('''#!/bin/bash
set -eu
umask 077
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
[ "$(id -u)" = 0 ] && [ "${3:-/}" = / ] || { echo "Use the running system volume." >&2; exit 1; }
package_scripts=$(cd "$(dirname "$0")" && pwd -P)
stage=$(mktemp -d /private/var/tmp/rtx3070-local.XXXXXX)
trap 'rm -rf "$stage"' EXIT
cd "$package_scripts"
printf '%s  %s\\n' PAYLOAD_HASH local-driver.tar.gz | shasum -a 256 --strict --quiet -c -
/usr/bin/tar -xzf local-driver.tar.gz -C "$stage"
cd "$stage/pkgroot"
printf '%s  %s\\n' MANIFEST_HASH SHA256SUMS | shasum -a 256 --strict --quiet -c -
shasum -a 256 --strict --quiet -c SHA256SUMS
/bin/bash ./sums_complete.sh "$PWD"
/usr/bin/env -i PATH="$PATH" TMPDIR=/private/var/tmp /bin/bash ./install.sh
'''.replace("PAYLOAD_HASH", payload_hash).replace("MANIFEST_HASH", manifest))
        postinstall.chmod(0o755)
        run("/bin/bash", "-n", postinstall)
        pkg = working / OUTPUT.name
        run("/usr/bin/pkgbuild", "--nopayload", "--scripts", scripts, "--identifier", IDENTIFIER,
            "--version", "3.0.1", "--install-location", "/", pkg)
        expanded = working / "expanded"
        run("/usr/sbin/pkgutil", "--expand-full", pkg, expanded)
        metadata = ET.parse(expanded / "PackageInfo").getroot()
        if metadata.get("identifier") != IDENTIFIER:
            raise ValueError("package receipt identifier mismatch")
        embedded = expanded / "Scripts"
        if sha(embedded / "local-driver.tar.gz") != payload_hash or sha(embedded / "postinstall") != sha(postinstall):
            raise ValueError("packaged installer content mismatch")
        run("/bin/bash", "-n", embedded / "postinstall")
        # Atomic replacement leaves the preceding usable package intact if a rebuild fails.
        pkg.chmod(0o644)
        os.replace(pkg, OUTPUT)
        print(f"{OUTPUT}\nPackage SHA-256  {sha(OUTPUT)}\nManifest SHA-256 {manifest}\nPayload SHA-256  {payload_hash}")


if __name__ == "__main__":
    main()
