#!/usr/bin/env python3
"""Assemble and inspect the local driver product without running its installer."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tarfile
import tempfile
import xml.etree.ElementTree as ET


HERE = Path(__file__).resolve().parent
WORK = HERE.parents[1]
BASE = HERE / "original/Scripts"
ACCEL = HERE / "accel-source/Scripts/local-driver.tar.gz"
RELEASE = Path.home() / "Library/Application Support/NullMoth/nullmoth-nvidia-3.0.tar.gz"
GUI = WORK / "全局Metal偏好-安装.pkg"
BOOST = Path.home() / "Documents/Codex/2026-11-19/du/work/host-import/boost/Enable-GPU-Boost.pkg"
PLUGIN = Path("Library/GPUBundles/NVMTLDriver.bundle")
FB = Path("Library/Extensions/NVRMFB.kext")
ACCEL_VARIANTS = Path("Library/NullMoth/kexts")
NVK = Path("Library/GPUBundles/nvmtl/libvulkan_nouveau.dylib")
ALLOW = Path("Library/GPUBundles/nvmtl-allow.txt")
CONF = Path("Library/GPUBundles/nvmtl/nvrm610.conf")
VERSION = "3.0.2"
DRIVER_ID = "local.rtx3070.display.consolidated"
GUI_ID = "local.nullmoth.angle-metal-defaults.apply"
BOOST_ID = "local.rtx3070.gpu-boost.enable"
FILENAME = f"RTX3070-Display-{VERSION}-Consolidated-local.pkg"
PROVISIONAL = "1122274aff4a21133dd96471b959b3273a91d7e90bf4ba3b4c48f8942703ed3f"


def run(*args, cwd=None, capture=False):
    return subprocess.run(list(map(str, args)), cwd=cwd, check=True, text=True,
                          stdout=subprocess.PIPE if capture else None).stdout


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_hash(path, expected):
    if sha(path) != expected:
        raise ValueError(f"Unexpected checksum: {path}")


def files_in(directory):
    return {path.relative_to(directory).as_posix(): sha(path) for path in directory.rglob("*") if path.is_file()}


def verify_code(path):
    run("/usr/bin/codesign", "--verify", "--deep", "--strict", path)


def verified_bundle(source, destination, executable, image_hash=None):
    verify_code(source)
    paths = lambda root: {p.relative_to(root) for p in root.rglob("*")}
    if paths(source) != paths(destination):
        raise ValueError(f"Overlay bundle layout changed: {source}")
    if (source / "Contents/Info.plist").read_bytes() != (destination / "Contents/Info.plist").read_bytes():
        raise ValueError(f"Overlay bundle metadata changed: {source}")
    binary = source / "Contents/MacOS" / executable
    if image_hash:
        verify_hash(binary, image_hash)
    shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)
    verify_code(destination)
    installed = destination / "Contents/MacOS" / executable
    verify_hash(installed, sha(binary))
    return sha(installed)


def verify_paths(root):
    banned = {"aircache", "spvcache", "efi", "users", "nvfw", "gpuboost",
              "reached-desktop.json", "system-profile.json", "state"}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if path.suffix in {".kc", ".log", ".ips", ".csv"} or banned.intersection(p.lower() for p in relative.parts):
            raise ValueError(f"Unexpected local data in payload: {relative}")
        if path.is_symlink() and (os.path.isabs(os.readlink(path)) or not path.resolve().is_relative_to(root)):
            raise ValueError(f"Unsafe payload link: {relative}")


def stage(directory):
    payload = BASE / "local-driver.tar.gz"
    verify_hash(payload, "e2414efa85f06442a3c4635ed1ba38f2fc1945d5316e9ae6731bf951a734aa51")
    verify_hash(ACCEL, "3c213f8258ba0d998349c0092bf950659c9af4d460a841e3f86b82fafe931a6f")
    verify_hash(RELEASE, "40f0390d0b7cda75637991879b09abb7f8f7e3e5db2e811196963b3a2cacbcad")
    run("/usr/bin/tar", "-xzf", payload, "-C", directory)
    root = directory / "pkgroot"
    run("/usr/bin/shasum", "-a", "256", "--strict", "--quiet", "-c", "SHA256SUMS", cwd=root)
    run("/bin/bash", root / "sums_complete.sh", root)
    (root / "Users").rmdir()  # The original archive carries this empty directory.

    overlay = directory / "accel"
    overlay.mkdir()
    run("/usr/bin/tar", "-xzf", ACCEL, "-C", overlay,
        *(f"pkgroot/{ACCEL_VARIANTS}/{os_major}/NVAccel.kext" for os_major in (15, 26)))
    accel = overlay / "pkgroot" / ACCEL_VARIANTS
    hashes = {}
    for os_major in ("15", "26"):
        variant = Path(os_major) / "NVAccel.kext"
        hashes[f"NVAccel-{os_major}"] = verified_bundle(
            accel / variant, root / ACCEL_VARIANTS / variant, "NVAccel",
            "d20b33fe71e3a3592dd1d7e70aee15b066b84e05ae32a95d8863ade5b2ce7797" if os_major == "26" else None)
    verified_bundle(accel / "15/NVAccel.kext", root / "Library/Extensions/NVAccel.kext",
                    "NVAccel", hashes["NVAccel-15"])
    hashes["NVRMFB"] = verified_bundle(
        HERE.parent / "drawable-pool-3.0.2.dcWf9c/candidate/NVRMFB.kext", root / FB, "NVRMFB",
        "6da394f155441cf1019c7d02d3b2ec51f9e591dc821bac7d79878ef03334a917")
    hashes["NVMTLDriver"] = verified_bundle(
        HERE.parent / "surface-stall-fix/metal-C2145874-1980-3DAB-A52B-EEF95603569E/NVMTLDriver.bundle",
        root / PLUGIN, "NVMTLDriver", "1b93db73660bb6975f7d9149e77998b4adf8ed77c08d4f5c4f03c4c22a98db80")
    nvk_image = HERE.parent / "shader-vram/libvulkan_nouveau.dylib"
    hashes["NVK"] = "4e56026f67822a3b75959f061ee209d0f72d5f968eb2c935bf8fa5f96b887e30"
    verify_hash(nvk_image, hashes["NVK"])
    shutil.copy2(nvk_image, root / NVK)
    verify_code(root / NVK)
    for name, digest in (
        ("NVRM", "ba6184e9a092687692af1b9e16cff555c7c3f1d81c258c8c8b77273ed7cadf05"),
        ("NVRMAGDC", "97623c55c4a804fd5d4a5338d06672fe87f160576435853dc6b6e13505b325b9"),
    ):
        verify_hash(root / f"Library/Extensions/{name}.kext/Contents/MacOS/{name}", digest)
        hashes[name] = digest
    for library in sorted((root / PLUGIN / "Contents/MacOS").glob("*.dylib")):
        hashes[library.name] = sha(library)
    for binary in ("Library/GPUBundles/nvmtl/libvulkan.1.4.362.dylib", "Library/NullMoth/boost/nvmtl-boost"):
        hashes[Path(binary).name] = sha(root / binary)

    # The local updater requires the target's existing allowlist, so only the
    # upstream default accompanies the staging tree, never the host's file.
    with tarfile.open(RELEASE, "r:gz") as upstream:
        file = upstream.getmember(f"pkgroot/{ALLOW}")
        if not file.isfile():
            raise ValueError("Unexpected upstream allowlist entry")
        (root / ALLOW).write_bytes(upstream.extractfile(file).read())
    config = root / CONF
    current = config.read_text()
    knobs = {
        "NVMTL_SURF_IMPORT_CACHE": "1",
        "NVMTL_HOST_PAGES_SHARE": "1",
        "NVMTL_HOST_PAGES_MB": "8192",
    }
    if any(f"{key}=" in current for key in knobs):
        raise ValueError("Updating a knob already shipped by the base")
    config.write_text(current.rstrip() + "\n" + "".join(f"{key}={value}\n" for key, value in knobs.items()))
    (root / "BASELINE-IDENTITY.json").unlink()
    repository = WORK / "nvidia-macos-driver"
    license_paths = {}
    for source in (repository / "LICENSE", repository / "NOTICE", *sorted((repository / "licenses").glob("*"))):
        if not source.is_file():
            raise ValueError(f"Unexpected upstream license entry: {source.name}")
        destination = root / "licenses" / source.name
        if destination.exists():
            verify_hash(destination, sha(source))
        else:
            shutil.copyfile(source, destination)
        license_paths[source.name] = sha(destination)

    installer = root / "install.sh"
    script = installer.read_text()
    original_gate = 'ok "macOS $v, x86_64, NVIDIA GPU present"'
    local_gate = ('printf \'%s\\n\' "$NV_IDS" | grep -Fxq 2488 || '
                  'die "This local build requires the tested RTX 3070 PCI device 10de:2488."\n'
                  'ok "macOS $v, x86_64, RTX 3070 (10de:2488) present"')
    for previous, updated in (("RTX 3070 local update staged.", f"RTX 3070 consolidated {VERSION} local update staged."),
                              ('case $MAJ in 15|26) ;; *) die "This driver is built for macOS 15 and 26 (this is $v); no changes made";; esac',
                               '[ "$MAJ" = 26 ] || die "This local driver was tested on macOS 26 and requires it."'),
                              (original_gate, local_gate)):
        if script.count(previous) != 1:
            raise ValueError(f"Unexpected installer script at {previous}")
        script = script.replace(previous, updated, 1)
    installer.write_text(script)

    verify_paths(root)
    manifest = root / "SHA256SUMS"
    before_manifest = sha(manifest)
    manifest.write_text("".join(f"{sha(p)}  ./{p.relative_to(root)}\n" for p in sorted(root.rglob("*"))
                                if p.is_file() and not p.is_symlink() and p != manifest))
    for path in sorted(root.glob("*.sh")):
        run("/bin/bash", "-n", path)
    run("/usr/bin/shasum", "-a", "256", "--strict", "--quiet", "-c", manifest, cwd=root)
    run("/bin/bash", WORK / "nvidia-macos-driver/package/check_package.sh", root)
    run("/bin/bash", root / "nullmoth-runtime-check.sh", root, "26.6.2")
    return root, hashes, before_manifest, license_paths


def build_component(directory, root, prior_manifest):
    scripts = directory / "scripts"
    scripts.mkdir()
    payload = scripts / "local-driver.tar.gz"
    with payload.open("wb") as stream, gzip.GzipFile(filename="", fileobj=stream, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
            def normalize(info):
                info.uid, info.gid, info.uname, info.gname, info.mtime = 0, 0, "root", "wheel", 0
                return info
            archive.add(root, arcname="pkgroot", filter=normalize)
    original = (BASE / "postinstall").read_text()
    for old, new in ((sha(BASE / payload.name), sha(payload)), (prior_manifest, sha(root / "SHA256SUMS"))):
        if original.count(old) != 1:
            raise ValueError("Unexpected digest entry in original package runner")
        original = original.replace(old, new, 1)
    post = scripts / "postinstall"
    post.write_text(original)
    post.chmod(0o755)
    run("/bin/bash", "-n", post)
    component = directory / "Driver.pkg"
    run("/usr/bin/pkgbuild", "--nopayload", "--scripts", scripts, "--identifier", DRIVER_ID,
        "--version", VERSION, "--install-location", "/", component)
    return component, {p.name: sha(p) for p in scripts.iterdir()}


def build_product(directory, driver):
    verify_hash(GUI, "6e3c2553abade8fc11544ea3c992c93af13a6eb648289c64f86eb63def28dec0")
    gui = directory / "OptionalMetalPreferences.pkg"
    shutil.copyfile(GUI, gui)
    verify_hash(BOOST, "060101b92da747feb5beb1e01406dad26a80ebed4fcaac43ee407876ded05feb")
    boost_scripts = HERE / "boost-source/Scripts"
    verify_hash(boost_scripts / "postinstall", "36679a3514d50b11d0f7a6ce0881c6b9bc7a101b919b28bdd02fd06c975c13d1")
    verify_hash(boost_scripts / "boost_daemon.sh", "d8ed06c2922b25b0351edc1ce21a805c3dfc2edc550ceb915d05cd9bdb06eb4a")
    verify_hash(boost_scripts / "Library/NullMoth/boost/nvmtl-boost", "7dd80566a00f945d4b3224564865956a04e26f254fd806eac8fec42853718c07")
    verify_code(boost_scripts / "Library/NullMoth/boost/nvmtl-boost")
    run("/bin/bash", "-n", boost_scripts / "postinstall")
    boost = directory / "EnableGPUBoost.pkg"
    run("/usr/bin/pkgbuild", "--nopayload", "--scripts", boost_scripts, "--identifier", BOOST_ID,
        "--version", VERSION, "--install-location", "/", boost)

    distribution = directory / "Distribution"
    run("/usr/bin/productbuild", "--synthesize", "--package", driver, "--package", gui,
        "--package", boost, distribution)
    doc = ET.parse(distribution)
    top = doc.getroot()
    ET.SubElement(top, "title").text = f"RTX 3070 Local Driver {VERSION} (requires NullMoth 3.0)"
    options = top.find("options")
    options.set("customize", "allow")
    options.set("hostArchitectures", "x86_64")
    choices = {choice.find("pkg-ref").get("id"): choice for choice in top.findall("choice")
               if choice.find("pkg-ref") is not None}
    if set(choices) != {DRIVER_ID, GUI_ID, BOOST_ID}:
        raise ValueError("Unexpected product choices")
    choices[DRIVER_ID].attrib.update(title="RTX 3070 local driver", visible="true", start_selected="true", enabled="false",
                                     description="For x86_64 macOS 26 and RTX 3070 PCI device 10de:2488. Requires installed NullMoth 3.0 and its firmware.")
    choices[GUI_ID].attrib.update(title="Global Metal preferences", visible="true", start_selected="false",
                                  description="Optional. Applies ANGLE Metal settings to graphical login sessions.")
    choices[BOOST_ID].attrib.update(title="启用 GPU Boost", visible="true", start_selected="false",
                                    description="Optional. Enables the system-wide NVIDIA GPU Boost daemon.")
    for ref in top.findall("pkg-ref"):
        if ref.text:
            ref.text = Path(ref.text).name
    doc.write(distribution, encoding="utf-8", xml_declaration=True)
    run("/usr/bin/xmllint", "--noout", distribution)
    product = directory / FILENAME
    run("/usr/bin/productbuild", "--distribution", distribution, "--package-path", directory, product)
    return product


def verify_product(directory, product, scripts):
    expanded = directory / "product"
    run("/usr/sbin/pkgutil", "--expand-full", product, expanded)
    components = (("Driver.pkg", DRIVER_ID, VERSION, scripts),
                  ("OptionalMetalPreferences.pkg", GUI_ID, "1.0", files_in(HERE / "gui-source/Scripts")),
                  ("EnableGPUBoost.pkg", BOOST_ID, VERSION, files_in(HERE / "boost-source/Scripts")))
    for name, identifier, version, expected in components:
        component = expanded / name
        metadata = ET.parse(component / "PackageInfo").getroot()
        if metadata.get("identifier") != identifier or metadata.get("version") != version or metadata.find("payload") is not None:
            raise ValueError(f"Unexpected component metadata: {name}")
        recovered = component / "Scripts"
        if files_in(recovered) != expected:
            raise ValueError(f"Package round trip changed scripts: {name}")
    changes = directory / "choice-changes.plist"
    result = {}
    for label, enabled in (("Default", ()), ("GUI only", (GUI_ID,)), ("Boost only", (BOOST_ID,)),
                           ("Both opt-ins", (GUI_ID, BOOST_ID))):
        entries = [{"choiceIdentifier": choice, "choiceAttribute": "selected", "attributeSetting": 1} for choice in enabled]
        changes.write_bytes(plistlib.dumps(entries))
        choices = plistlib.loads(run("/usr/sbin/installer", "-showChoicesAfterApplyingChangesXML", changes,
                                    "-pkg", product, "-target", "/", capture=True).encode())
        selected = {item.get("choiceIdentifier"): item.get("attributeSetting") for item in choices
                    if item.get("choiceAttribute") == "selected"}
        if selected.get(DRIVER_ID) != 1 or any(selected.get(choice) != int(choice in enabled) for choice in (GUI_ID, BOOST_ID)):
            raise ValueError(f"Unexpected selection for {label}: {choices}")
        result[label] = selected
    return result


def main():
    destination = WORK / FILENAME
    if destination.exists():
        verify_hash(destination, PROVISIONAL)
    with tempfile.TemporaryDirectory(prefix="build.", dir=HERE) as temp:
        directory = Path(temp)
        root, hashes, prior_manifest, license_paths = stage(directory)
        component, scripts = build_component(directory, root, prior_manifest)
        product = build_product(directory, component)
        choices = verify_product(directory, product, scripts)
        report = {"package": destination.name, "package_sha256": sha(product), "components": hashes,
                  "manifest_sha256": sha(root / "SHA256SUMS"), "payload_sha256": scripts["local-driver.tar.gz"],
                  "choices": choices, "license_files": license_paths, "installed_during_packaging": False}
        product.chmod(0o644)
        if destination.exists():
            verify_hash(destination, PROVISIONAL)
            os.replace(product, destination)
        else:
            os.link(product, destination)
        verify_hash(destination, report["package_sha256"])
        (HERE / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
