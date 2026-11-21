#!/bin/bash
set -euo pipefail
umask 077
export PATH=/usr/bin:/bin:/usr/sbin:/sbin LC_ALL=C

scripts=$1
parent=$2
state=$3
live="$parent/nvmtl/libvulkan_nouveau.dylib"
guard="$state/install.lock"
owner=$(id -u)
before=$(cat "$scripts/before")
after=$(cat "$scripts/after")

fail() { echo "RTX 3070 shader VRAM: $*" >&2; exit 1; }
secure() {
    [ ! -L "$1" ] && [ "$(stat -f '%u' "$1")" = "$owner" ] &&
    [ -z "$(find "$1" -maxdepth 0 \( -perm -020 -o -perm -002 \) -print)" ] &&
    ! ls -lde "$1" | awk '/^[[:space:]]*[0-9]+:.* allow / && /write|add_file|add_subdirectory|delete|append|chown/ { bad=1 } END { exit !bad }'
}
matches() {
    [ -f "$1" ] && secure "$1" &&
    printf '%s  %s\n' "$2" "$1" | shasum -a 256 --strict --quiet -c - 2>/dev/null &&
    codesign --verify --strict "$1"
}

for path in "$(dirname "$parent")" "$parent" "$parent/nvmtl" "$state"; do
    [ -d "$path" ] && secure "$path" || fail "untrusted directory $path"
done
if matches "$live" "$after"; then
    echo "RTX 3070 shader VRAM: requested driver already installed."
    exit 0
fi
matches "$live" "$before" || fail "installed NVK does not match the tested starting build"

boot=$(sysctl -n kern.boottime | sed -E 's/.*sec = ([0-9]+),.*/\1/')
if [ -e "$guard" ] || [ -L "$guard" ]; then
    [ -d "$guard" ] && secure "$guard" && secure "$guard/owner" &&
        grep -qx 'kind=nvk-shader-vram-v1' "$guard/owner" ||
        fail "another driver installation owns $guard"
    pid=$(sed -n 's/^pid=\([0-9][0-9]*\)$/\1/p' "$guard/owner")
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null &&
        ps -p "$pid" -o command= | grep -q '/install\.sh'; then
        fail "another shader VRAM installation is running"
    fi
    rm "$guard/owner"
    rmdir "$guard"
fi
claim=$(mktemp -d "$state/.shader-vram-guard.XXXXXX")
printf 'pid=%s\nboot=%s\nkind=nvk-shader-vram-v1\n' "$$" "$boot" > "$claim/owner"
if ! "$scripts/exchange" claim "$claim" "$guard"; then
    rm "$claim/owner"
    rmdir "$claim"
    fail "another driver installation started"
fi
guard_id=$(stat -f '%d:%i' "$guard")
stage=
cleanup() {
    if [ -n "$stage" ]; then
        rm -f "$stage/libvulkan_nouveau.dylib"
        rmdir "$stage"
    fi
    if [ ! -L "$guard" ] && [ "$(stat -f '%d:%i' "$guard" 2>/dev/null || :)" = "$guard_id" ]; then
        rm -f "$guard/owner"
        rmdir "$guard"
    fi
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

store="$state/shader-vram-backups"
if [ ! -e "$store" ]; then mkdir -m 700 "$store"; fi
[ -d "$store" ] && secure "$store" || fail "untrusted backup directory"
backup="$store/$before.dylib"
if [ ! -e "$backup" ]; then
    temp_backup=$(mktemp "$store/.backup.XXXXXX")
    if cp -p "$live" "$temp_backup" && matches "$temp_backup" "$before"; then
        "$scripts/exchange" claim "$temp_backup" "$backup"
    else
        rm -f "$temp_backup"
        fail "could not verify the rollback backup"
    fi
fi
matches "$backup" "$before" || fail "rollback backup does not match"

stage=$(mktemp -d "$parent/nvmtl/.shader-vram.XXXXXX")
candidate="$stage/libvulkan_nouveau.dylib"
install -m 0755 "$scripts/libvulkan_nouveau.dylib" "$candidate"
if [ "$owner" = 0 ]; then chown root:wheel "$candidate"; fi
matches "$candidate" "$after" || fail "staged NVK failed verification"
matches "$live" "$before" || fail "installed NVK changed during staging"
"$scripts/exchange" swap "$live" "$candidate" || fail "atomic driver exchange failed"
if ! matches "$live" "$after"; then
    "$scripts/exchange" swap "$live" "$candidate" || fail "verification failed, backup at $backup"
    fail "verification failed, preceding driver restored"
fi
echo "RTX 3070 shader VRAM: requested driver verified on disk, digest $after."
