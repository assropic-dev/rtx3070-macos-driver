#!/bin/bash
set -euo pipefail
stage=$(cd "$(dirname "$0")" && pwd -P)
repo="${NV_DRIVER_SOURCE:-$stage/../../nvidia-macos-driver}"
deps="${NV_BUILD_DEPS:-$stage/../../build-3.0/deps}"
cd "$repo"
sources=(plugin/NVMTLDevice.m plugin/nvmtl_vk.c plugin/nvmtl_vk.h plugin/NVMTLEventSync.m plugin/NVMTLObjects.m kexts/NVRM/accel/nvrm-accel.cpp kexts/NVRM/nvrm_surfshare_abi.h)
shasum -a 256 "${sources[@]}" > "$stage/build-sources.sha256"
OGKM="$deps/ogkm620" bash build/accel_build_all.sh "$repo/kexts/NVRM" "$stage/final-accel"
bash build/check_kext_libraries.sh "$stage/final-accel/15/NVAccel.kext" "$stage/final-accel/26/NVAccel.kext"
cd plugin
xcrun clang -arch x86_64 -bundle -fobjc-arc -O1 -Wno-protocol -Wno-incomplete-implementation \
    -I"$deps/vulkan-headers/include" -o "$stage/NVMTLDriver.building" NVMTLDevice.m nvmtl_dlss.c dlss/elfhost.c dlss/lnxlibc.c \
    -framework Foundation -framework Metal -framework IOSurface -framework MetalFX -framework IOKit -liconv
cd "$repo"
shasum -a 256 --check --quiet "$stage/build-sources.sha256"
mv "$stage/NVMTLDriver.building" "$stage/NVMTLDriver"
xcrun dwarfdump --uuid "$stage/NVMTLDriver" "$stage/final-accel/26/NVAccel.kext/Contents/MacOS/NVAccel"
