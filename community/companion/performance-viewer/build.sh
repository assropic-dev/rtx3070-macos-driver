#!/bin/bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
output_dir="${1:-$project_dir/build}"
deployment_target="${MACOSX_DEPLOYMENT_TARGET:-13.0}"
sdk_path="$(xcrun --sdk macosx --show-sdk-path)"

shopt -s nullglob
sources=("$project_dir"/Sources/*.swift)
if [[ ${#sources[@]} -eq 0 ]]; then
    echo "No Swift sources found in $project_dir/Sources." >&2
    exit 1
fi

mkdir -p "$output_dir"
output_dir="$(cd "$output_dir" && pwd)"
staging_dir="$(mktemp -d "$output_dir/.performance-viewer.XXXXXX")"
trap '/usr/bin/find "$staging_dir" -depth -delete' EXIT

app_name="性能查看器.app"
executable_name="PerformanceViewer"
bundle="$staging_dir/$app_name"
contents="$bundle/Contents"
mkdir -p "$contents/MacOS" "$contents/Resources"

cat > "$contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key>
    <string>zh-Hans</string>
    <key>CFBundleDisplayName</key>
    <string>性能查看器</string>
    <key>CFBundleExecutable</key>
    <string>$executable_name</string>
    <key>CFBundleIdentifier</key>
    <string>local.performanceviewer.app</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleName</key>
    <string>性能查看器</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.8</string>
    <key>CFBundleVersion</key>
    <string>9</string>
    <key>LSApplicationCategoryType</key>
    <string>public.app-category.utilities</string>
    <key>LSMinimumSystemVersion</key>
    <string>$deployment_target</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSScreenCaptureUsageDescription</key>
    <string>用于统计你选中屏幕或窗口的画面更新帧率、帧间隔和卡顿次数。</string>
</dict>
</plist>
EOF

if [[ -f "$project_dir/Resources/AppIcon.icns" ]]; then
    cp "$project_dir/Resources/AppIcon.icns" "$contents/Resources/AppIcon.icns"
    /usr/libexec/PlistBuddy -c "Add :CFBundleIconFile string AppIcon" "$contents/Info.plist"
fi

architectures=(x86_64 arm64)
binaries=()
for architecture in "${architectures[@]}"; do
    binary="$staging_dir/$executable_name-$architecture"
    echo "Building $architecture for macOS $deployment_target"
    xcrun --sdk macosx swiftc -parse-as-library -O -whole-module-optimization \
        -sdk "$sdk_path" -target "$architecture-apple-macosx$deployment_target" \
        -module-name "$executable_name" "${sources[@]}" -o "$binary"
    binaries+=("$binary")
done

executable="$contents/MacOS/$executable_name"
xcrun lipo -create "${binaries[@]}" -output "$executable"
xcrun lipo "$executable" -verify_arch "${architectures[@]}"

amd_clock="$contents/MacOS/amd-clock"
xcrun --sdk macosx clang -Wall -Wextra -O2 -isysroot "$sdk_path" \
    -target "x86_64-apple-macosx$deployment_target" "$project_dir/Sources/amd-clock.c" -o "$amd_clock"
/usr/bin/codesign --force --sign - "$amd_clock"

/usr/bin/plutil -lint "$contents/Info.plist"
/usr/bin/codesign --force --sign - "$bundle"
/usr/bin/codesign --verify --deep --strict "$bundle"

destination="$output_dir/$app_name"
if [[ -e "$destination" || -L "$destination" ]]; then
    existing_identifier="$(/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" \
        "$destination/Contents/Info.plist" 2>/dev/null || true)"
    if [[ -L "$destination" || "$existing_identifier" != "local.performanceviewer.app" ]]; then
        echo "Destination is already used by another file: $destination" >&2
        exit 1
    fi
    mv "$destination" "$staging_dir/previous.app"
fi
if ! mv "$bundle" "$destination"; then
    if [[ -e "$staging_dir/previous.app" || -L "$staging_dir/previous.app" ]]; then
        mv "$staging_dir/previous.app" "$destination"
    fi
    exit 1
fi

printf '%s\n' "$destination"
