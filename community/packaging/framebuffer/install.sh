#!/bin/bash
# Inputs are fixed by the package's postinstall. Keeping them explicit also
# permits exercising the file transaction on an isolated fixture.
set -euo pipefail
umask 077
export PATH=/usr/bin:/bin:/usr/sbin:/sbin LC_ALL=C
scripts=$1
action=$2
extensions=$3
collections=$4
state=$5
boot_collection=$6
system_collection=$7
kmutil=$8
live="$extensions/NVRMFB.kext"
apple_mobile=/Library/Apple/System/Library/Extensions/AppleMobileDevice.kext
collection="$collections/AuxiliaryKernelExtensions.kc"
store="$state/local-fb-backups"
guard="$state/install.lock"
known_collection="$store/candidate.$(cat "$scripts/new-executable.sha").sha"
owner=$(id -u)

die() { echo "NVRMFB update: $*" >&2; exit 1; }
file_hash() { shasum -a 256 "$1" | awk '{print $1}'; }
no_write_acls() {
    local listing
    listing=$(ls "$1" "$2") || return 1
    ! printf '%s\n' "$listing" | awk '/^[[:space:]]*[0-9]+:.* allow / && /write|add_file|add_subdirectory|delete|append|chown/ {bad=1} END {exit !bad}'
}
secure_dir() {
    [ -d "$1" ] && [ ! -L "$1" ] && [ "$(stat -f '%u' "$1")" = "${2:-$owner}" ] &&
        [ -z "$(find "$1" -maxdepth 0 \( -perm -020 -o -perm -002 \) -print)" ] && no_write_acls -lde "$1"
}
secure_file() {
    [ -f "$1" ] && [ ! -L "$1" ] && [ "$(stat -f '%u' "$1")" = "$owner" ] &&
        [ -z "$(find "$1" -maxdepth 0 \( -perm -020 -o -perm -002 \) -print)" ] && no_write_acls -le "$1"
}
bundle_matches() {
    secure_dir "$1" || return 1
    ( cd "$1" &&
      [ -z "$(find . ! -type d ! -type f -print)" ] &&
      [ -z "$(find . \( ! -user "$owner" -o -perm -020 -o -perm -002 \) -print)" ] && no_write_acls -leR . &&
      find . -print | sort | cmp -s - "$scripts/kext.paths" &&
      shasum -a 256 --strict --quiet -c "$scripts/$2.kext.sha256" &&
      codesign --verify --deep --strict . >/dev/null 2>&1 )
}
prerequisites() {
    [ "$(uname -m)" = x86_64 ] && [ "$(sw_vers -buildVersion)" = "$(cat "$scripts/os-build")" ] &&
        [ "$(file_hash "$boot_collection")" = "$(cat "$scripts/boot-kc.sha")" ] &&
        [ "$(file_hash "$system_collection")" = "$(cat "$scripts/system-kc.sha")" ] &&
        secure_dir "$apple_mobile" 0 &&
        ( cd "$apple_mobile" &&
          [ -z "$(find . \( -type l -o ! -user root -o -perm -020 -o -perm -002 \) -print)" ] &&
          find . -print | sort | cmp -s - "$scripts/apple-mobile.paths" &&
          shasum -a 256 --strict --quiet -c "$scripts/apple-mobile.sha256" ) &&
        ( cd "$extensions" &&
          [ -z "$(find . -name NVRMFB.kext -prune -o \( -type l -o ! -user "$owner" -o -perm -020 -o -perm -002 \) -print)" ] &&
          no_write_acls -leR . &&
          find . -name NVRMFB.kext -prune -o -print | sort | cmp -s - "$scripts/neighbors.paths" &&
          shasum -a 256 --strict --quiet -c "$scripts/neighbors.sha256" )
}
collect_members() {
    "$kmutil" inspect -a x86_64 -A "$1" --show-extension-info --show-kext-uuids |
        sed $'s/\\\\t/\t/g' |
        awk -F '\t' -v OFS='\t' -v shadow="${2:-}" '
            NF == 4 && $1 ~ /^[[:alnum:]_.-]+$/ {
                if (shadow != "" && $1 == "com.nullmoth.NVRMFB" && $3 == shadow)
                    $3 = "/Library/Extensions/NVRMFB.kext"
                print $1, $2, $3, $4
            }' | sort
}
old_collection_hash=$(cat "$scripts/old-kc.sha")
collection_kind() {
    local actual
    secure_file "$collection" || return 1
    actual=$(file_hash "$collection") || return 1
    if [ "$actual" = "$old_collection_hash" ]; then
        echo old
    elif secure_file "$known_collection" && [ "$actual" = "$(cat "$known_collection")" ] &&
         collect_members "$collection" | cmp -s - "$scripts/new.members"; then
        echo new
    else
        return 1
    fi
}

case "$action" in update) desired=new;; rollback) desired=old;; *) die "Unrecognized operation.";; esac
secure_dir "$extensions" && secure_dir "$collections" && secure_dir "$state" ||
    die "The installed driver directories are not trusted."
[ "$(stat -f '%d' "$extensions")" = "$(stat -f '%d' "$collections")" ] &&
    [ "$(stat -f '%d' "$extensions")" = "$(stat -f '%d' "$state")" ] ||
    die "The kext, collection and backups must be on the same filesystem."
prerequisites || die "The system build, dependencies or neighboring extensions differ from the package baseline."
if [ ! -e "$store" ]; then mkdir -m 700 "$store"; fi
secure_dir "$store" || die "The backup directory is not trusted."

if [ -e "$guard" ] || [ -L "$guard" ]; then
    secure_dir "$guard" && secure_file "$guard/owner" &&
        [ ! -e "$guard/installing" ] && grep -qx 'kind=nvrmfb-atomic-v1' "$guard/owner" ||
        die "Another NullMoth installation owns the install guard."
    pid=$(sed -n 's/^pid=\([0-9][0-9]*\)$/\1/p' "$guard/owner")
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && ps -p "$pid" -o command= | grep -q '/install\.sh'; then
        die "Another NVRMFB updater is running."
    fi
    rm "$guard/owner"
    rmdir "$guard"
fi
claim=$(mktemp -d "$state/.nvrmfb-guard.XXXXXX")
boot=$(sysctl -n kern.boottime | sed -E 's/.*sec = ([0-9]+),.*/\1/')
printf 'pid=%s\nboot=%s\nkind=nvrmfb-atomic-v1\n' "$$" "$boot" > "$claim/owner"
if ! "$scripts/exchange" claim "$claim" "$guard"; then
    rm -rf "$claim"
    die "Another NullMoth installation started."
fi
guard_id=$(stat -f '%d:%i' "$guard")
stage=
changing=0
new_kc_hash=

restore() {
    local current
    current=$(file_hash "$collection") || return 1
    if [ "$current" != "$before_kc_hash" ]; then
        [ -n "$new_kc_hash" ] && [ "$current" = "$new_kc_hash" ] &&
            [ "$(file_hash "$stage/original.kc")" = "$before_kc_hash" ] &&
            "$scripts/exchange" swap "$collection" "$stage/original.kc" || return 1
    fi
    if ! bundle_matches "$live" "$before_bundle"; then
        bundle_matches "$live" "$desired" && bundle_matches "$stage/original.kext" "$before_bundle" &&
            "$scripts/exchange" swap "$live" "$stage/original.kext" || return 1
    fi
    bundle_matches "$live" "$before_bundle" && [ "$(file_hash "$collection")" = "$before_kc_hash" ]
}
cleanup() {
    local result=$?
    trap - EXIT HUP INT TERM
    if [ "$result" != 0 ] && [ "$changing" = 1 ]; then
        if ! restore; then
            echo "Recovery incomplete. The guard and backups are retained at $guard and $stage." >&2
            exit 1
        fi
        echo "Original kext and collection restored." >&2
    fi
    if [ -n "$stage" ]; then rm -rf "$stage"; fi
    if [ ! -L "$guard" ] && [ "$(stat -f '%d:%i' "$guard" 2>/dev/null || :)" = "$guard_id" ]; then
        rm -f "$guard/owner"
        rmdir "$guard"
    fi
    exit "$result"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

if bundle_matches "$live" old; then before_bundle=old
elif bundle_matches "$live" new; then before_bundle=new
else die "The installed NVRMFB bundle does not match either verified build."; fi
before_kc=$(collection_kind) || die "The installed auxiliary collection does not match either verified build."
before_kc_hash=$(file_hash "$collection")
if [ "$before_bundle" = "$desired" ] && [ "$before_kc" = "$desired" ]; then
    echo "The requested NVRMFB and auxiliary collection are already staged."
    exit 0
fi

stage=$(mktemp -d "$store/.transaction.XXXXXX")
ditto "$live" "$stage/original.kext"
cp -p "$collection" "$stage/original.kc"
bundle_matches "$stage/original.kext" "$before_bundle" &&
    [ "$(file_hash "$stage/original.kc")" = "$before_kc_hash" ] || die "Could not verify the transaction backup."
if [ "$before_bundle" = old ] && [ "$before_kc" = old ]; then
    saved="$store/baseline-$old_collection_hash"
    if [ ! -e "$saved" ]; then
        mkdir "$stage/baseline"
        ditto "$stage/original.kext" "$stage/baseline/NVRMFB.kext"
        cp -p "$stage/original.kc" "$stage/baseline/AuxiliaryKernelExtensions.kc"
        "$scripts/exchange" claim "$stage/baseline" "$saved"
    fi
    secure_dir "$saved" && bundle_matches "$saved/NVRMFB.kext" old &&
        [ "$(file_hash "$saved/AuxiliaryKernelExtensions.kc")" = "$old_collection_hash" ] ||
        die "The persistent rollback backup does not match the verified baseline."
fi
if [ "$before_bundle" != "$desired" ] || { [ "$desired" = new ] && [ "$before_kc" != new ]; }; then
    ditto "$scripts/NVRMFB.kext" "$stage/NVRMFB.kext"
    if [ "$owner" = 0 ]; then chown -R root:wheel "$stage/NVRMFB.kext"; fi
    chmod -R go-w,a+rX "$stage/NVRMFB.kext"
    bundle_matches "$stage/NVRMFB.kext" "$desired" || die "Staged NVRMFB failed verification."
fi

if [ "$desired" = new ] && [ "$before_kc" != new ]; then
    # The root-owned shadow checks linkage and exact member identities before
    # exchanging any installed file. It is never published.
    shadow_args=(-p "$stage/NVRMFB.kext" -p "$apple_mobile")
    for path in "$extensions"/*.kext; do
        if [ "$path" != "$live" ]; then shadow_args+=(-p "$path"); fi
    done
    shadow_kc="$stage/shadow.kc"
    if ! "$kmutil" create -n aux --volume-root / --allow-missing-kdk -x -B "$boot_collection" -S "$system_collection" \
        "${shadow_args[@]}" -A "$shadow_kc" > "$stage/shadow.log" 2>&1; then
        tail -40 "$stage/shadow.log" >&2
        die "The shadow candidate collection did not build. No installed file was replaced."
    fi
    collect_members "$shadow_kc" "$stage/NVRMFB.kext" | cmp -s - "$scripts/new.members" ||
        die "The shadow collection does not contain exactly the expected members. No installed file was replaced."
fi

prerequisites && bundle_matches "$live" "$before_bundle" &&
    [ "$(file_hash "$collection")" = "$before_kc_hash" ] || die "Driver inputs changed during staging."
changing=1
if [ "$before_bundle" != "$desired" ]; then
    "$scripts/exchange" swap "$live" "$stage/NVRMFB.kext"
fi
bundle_matches "$live" "$desired" || die "NVRMFB failed verification after exchange."

if [ "$before_kc" != "$desired" ]; then
    next="$stage/next.kc"
    if [ "$desired" = old ]; then
        cp -p "$scripts/rollback.kc" "$next"
        [ "$(file_hash "$next")" = "$old_collection_hash" ] || die "Packaged rollback collection hash mismatch."
    else
        # Compile from the canonical live extension path, never a shadow path.
        if ! "$kmutil" create -n aux --volume-root / --allow-missing-kdk -B "$boot_collection" -S "$system_collection" \
            --repository "$extensions" -A "$next" > "$stage/kmutil.log" 2>&1; then
            tail -40 "$stage/kmutil.log" >&2
            die "kmutil did not build the auxiliary collection."
        fi
        collect_members "$next" | cmp -s - "$scripts/new.members" ||
            die "The rebuilt collection does not preserve all expected extension members."
        strings -a "$next" > "$stage/collection.strings"
        if grep -Fq "$stage" "$stage/collection.strings"; then
            die "The rebuilt collection contains a temporary install path."
        fi
    fi
    # Retain the installed collection's metadata, including its protection
    # flags. A refusal here leaves the live collection untouched.
    publish="$stage/publish.kc"
    cp -p "$collection" "$publish"
    cat "$next" > "$publish"
    [ "$(stat -f '%Lp:%f:%u:%g' "$publish")" = "$(stat -f '%Lp:%f:%u:%g' "$collection")" ] ||
        die "Could not preserve the auxiliary collection's protection metadata."
    new_kc_hash=$(file_hash "$publish")
    [ "$new_kc_hash" = "$(file_hash "$next")" ] || die "The staged collection copy is incomplete."
    if [ "$desired" = new ]; then
        printf '%s\n' "$new_kc_hash" > "$stage/candidate.sha"
        mv -f "$stage/candidate.sha" "$known_collection"
    fi
    prerequisites && bundle_matches "$live" "$desired" &&
        [ "$(file_hash "$collection")" = "$before_kc_hash" ] ||
        die "Driver inputs changed before publishing the collection."
    "$scripts/exchange" swap "$collection" "$publish"
fi
[ "$(collection_kind)" = "$desired" ] || die "The auxiliary collection failed verification after exchange."
changing=0
echo "NVRMFB and its auxiliary collection are staged on disk. Restart to load them."
