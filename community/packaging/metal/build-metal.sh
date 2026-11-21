#!/bin/bash
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd -P)
build=$(dirname "$here")
plugin="$build/../nvidia-macos-driver/plugin"
prior="$build/sync-10ms/81E2CCFC-1C7E-393D-AAC7-9719ED6AB8BE/NVMTLDriver.bundle"
stage=$(mktemp -d "$here/compile-metal.XXXXXX")
trap 'rm -rf "$stage"' EXIT
cd "$plugin"
shasum -a 256 *.m *.c *.h dlss/*.c dlss/*.h ../nvk/nv_fence_trace.h > "$stage/sources.sha256"
xcrun clang -arch x86_64 -bundle -fobjc-arc -O1 -Werror -Wno-protocol -Wno-incomplete-implementation \
    -DNVMTL_WS_STARTUP_STATS_SECONDS=600 -I"$build/deps/vulkan-headers/include" \
    -o "$stage/NVMTLDriver" NVMTLDevice.m nvmtl_dlss.c dlss/elfhost.c dlss/lnxlibc.c \
    -framework Foundation -framework Metal -framework IOSurface -framework MetalFX -framework IOKit -liconv
shasum -a 256 --strict --quiet -c "$stage/sources.sha256"
uuid=$(dwarfdump --uuid "$stage/NVMTLDriver" | awk '{print $2}')
out="$here/metal-$uuid"
if [[ ! -e "$out" ]]; then
    ditto "$prior" "$stage/NVMTLDriver.bundle"
    install -m 755 "$stage/NVMTLDriver" "$stage/NVMTLDriver.bundle/Contents/MacOS/NVMTLDriver"
    codesign -f -s - --timestamp=none "$stage/NVMTLDriver.bundle"
    codesign --verify --deep --strict "$stage/NVMTLDriver.bundle"
    mv "$stage" "$out"
else
    cmp "$stage/sources.sha256" "$out/sources.sha256"
    cmp "$stage/NVMTLDriver" "$out/NVMTLDriver"
fi
echo "$out/NVMTLDriver.bundle/Contents/MacOS/NVMTLDriver"
