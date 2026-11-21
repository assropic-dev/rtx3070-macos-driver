#!/bin/bash
set -eu
set -o pipefail
umask 077
export LC_ALL=C PATH=/usr/bin:/bin:/usr/sbin:/sbin

# The package's verified postinstall supplies fixed paths. Tests call this same
# engine against a disposable directory and a simulated session manager.
mode=$1
resources=$2
folder=$3
manager=$4
owner=$5
label=local.nullmoth.angle-metal-defaults
template="$resources/$label.plist"
live="$folder/$label.plist"
mark=$(cat "$resources/policy-marker")
die() { echo "Metal preferences: $*" >&2; exit 1; }
case "$mode" in apply|undo) ;; *) die "invalid operation" ;; esac
[ "$(id -u)" = "$owner" ] || die "unexpected transaction user"

trusted() {
    [ ! -L "$1" ] && [ "$(stat -f '%u' "$1")" = "$owner" ] &&
        [ -z "$(find "$1" -maxdepth 0 \( -perm -020 -o -perm -002 \) -print)" ] &&
        ls -lde "$1" | awk '/^[[:space:]]*[0-9]+:.* allow / && /write|append|delete|add_file|add_subdirectory|chown/ {bad=1} END {exit bad}'
}
[ -d "$folder" ] && trusted "$folder" && trusted "$(dirname "$folder")" || die "untrusted LaunchAgents directory"

has_file() { [ -e "$live" ] || [ -L "$live" ]; }
ours() { [ -f "$live" ] && trusted "$live" && cmp -s "$live" "$template"; }
if has_file && ! ours; then die "existing plist differs from this package; retained $live"; fi

check_job() {
    local uid=$1 job path
    if job=$("$manager" inspect "$uid" "$label" 2>/dev/null); then
        path=$(printf '%s\n' "$job" | awk '/^[[:space:]]*path = / {sub(/^[[:space:]]*path = /,""); print; exit}')
        [ "$path" = "$live" ] || die "another job owns $label in gui/$uid"
        # A rollback interrupted after unlink may leave our job in memory.
        # Compare its unique script marker before touching a job without a file.
        if ! has_file; then
            printf '%s\n' "$job" | grep -F "$mark" >/dev/null || die "unrecognized in-memory job in gui/$uid"
        fi
        return 0
    fi
    return 1
}

all_sessions() {
    local uid ids
    ids=$("$manager" uids) || return 1
    for uid in $ids; do
        case "$uid" in ''|*[!0-9]*|0) continue ;; esac
        if "$manager" exists "$uid"; then printf '%s\n' "$uid"; fi
    done
}
uids=$(all_sessions)
for uid in $uids; do check_job "$uid" || :; done

install_file() {
    local temp
    temp=$(mktemp "$folder/.metal-defaults.XXXXXX")
    if install -m 644 "$template" "$temp" && "$resources/guard" link-new "$temp" "$live"; then
        rm -f "$temp"
    else
        rm -f "$temp"
        die "could not create $live"
    fi
}

if [ "$mode" = apply ]; then
    if ! has_file; then install_file; fi
    ours || die "installed file failed verification"
    uids=$(all_sessions)
    for uid in $uids; do
        if "$manager" disabled "$uid" "$label"; then
            echo "gui/$uid has explicitly disabled this agent"; continue
        fi
        if check_job "$uid"; then
            "$manager" start "$uid" "$label" || {
                "$manager" exists "$uid" || continue
                die "could not run agent in gui/$uid; retry the package"
            }
        elif ! "$manager" load "$uid" "$live"; then
            "$manager" exists "$uid" || continue
            check_job "$uid" || die "could not register agent in gui/$uid; retry the package"
        fi
        "$manager" wait "$uid" "$label" || {
            "$manager" exists "$uid" || continue
            die "agent exited unsuccessfully or did not finish in gui/$uid; retry the package"
        }
    done
    echo "Installed $live"
    exit 0
fi

# Stop new logins from loading the job, then rediscover live domains. The
# root-owned template and in-memory marker make an interrupted removal retryable.
if has_file; then ours || die "plist changed during uninstall"; rm "$live"; fi
uids=$(all_sessions)
for uid in $uids; do
    if check_job "$uid"; then
        "$manager" unload "$uid" "$label" || {
            "$manager" exists "$uid" || continue
            check_job "$uid" && die "could not unload agent in gui/$uid"
        }
    fi
    "$manager" undo "$uid" "$resources/session-defaults.sh" || {
        "$manager" exists "$uid" || continue
        die "could not clear owned defaults in gui/$uid; retry the removal"
    }
done
echo "Removed managed Metal preferences"
