#!/bin/sh
set -eu
export LC_ALL=C PATH=/usr/bin:/bin:/usr/sbin:/sbin
ctl=/bin/launchctl
if [ "$1" = uids ]; then
    /bin/ps -axo uid= | /usr/bin/awk '$1 ~ /^[0-9]+$/ && $1 > 0 {print $1}' | /usr/bin/sort -nu
    exit 0
fi
uid=$2
case "$uid" in ''|*[!0-9]*|0) exit 2 ;; esac
d=gui/$uid
case "$1" in
exists) "$ctl" print "$d" >/dev/null 2>&1 ;;
inspect) "$ctl" print "$d/$3" ;;
disabled) "$ctl" print-disabled "$d" | /usr/bin/awk -v name="\"$3\"" '$1==name && $2=="=>" && ($3=="disabled" || $3=="true"){ok=1} END {exit !ok}' ;;
load) "$ctl" bootstrap "$d" "$3" ;;
start) "$ctl" kickstart "$d/$3" ;;
unload) "$ctl" bootout "$d/$3" ;;
wait)
    n=0
    while [ "$n" -lt 40 ]; do
        job=$("$ctl" print "$d/$3" 2>/dev/null) || exit 1
        if printf '%s\n' "$job" | /usr/bin/awk '
            /^[[:space:]]*state = / && !seen {active=($0 !~ /state = (not running|exited)$/); seen=1}
            /^[[:space:]]*last exit code = / {done=($NF=="0")}
            END {exit (!seen || active || !done)}'; then exit 0; fi
        /bin/sleep 0.25
        n=$((n+1))
    done
    exit 1 ;;
undo)
    policy=$(/bin/cat "$3")
    # PackageKit may keep its script directory inaccessible to the login user.
    # Hand the exact verified text to the unprivileged shell as an argument.
    "$ctl" asuser "$uid" /usr/bin/sudo -n -H -u "#$uid" -- /bin/sh -c '
        [ "$(/usr/bin/id -u)" = "$1" ] || exit 1
        exec /bin/sh -c "$2" local.nullmoth.angle-metal-defaults undo
    ' checked-session "$uid" "$policy" ;;
*) exit 2 ;;
esac
