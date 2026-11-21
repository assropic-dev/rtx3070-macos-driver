#!/bin/sh
# Called in an Aqua login domain. The marker belongs to this launchd session;
# it lets uninstall distinguish our defaults from identical pre-existing values.
set -eu
ctl=/bin/launchctl
platform=ANGLE_DEFAULT_PLATFORM
driver=NVMTL_ANGLE_METAL
marker=NULLMOTH_GUI_METAL_DEFAULTS_V1
read_key() { "$ctl" getenv "$1"; }

[ "$("$ctl" managername)" = Aqua ] || { echo "An Aqua login session is required." >&2; exit 1; }
owned=$(read_key "$marker")
case "$owned" in ''|p|d|pd) ;; *) echo "Unrecognized session marker." >&2; exit 1 ;; esac

case "${1:-}" in
apply)
    # Keep explicit backend selections and per-session opt-outs. Individual
    # applications can still choose their own backend or disable compositing.
    set_platform=0
    set_driver=0
    current_platform=$(read_key "$platform")
    current_driver=$(read_key "$driver")
    case "$current_platform" in ''|metal) ;; *) exit 0 ;; esac
    case "$current_driver" in ''|1) ;; *) exit 0 ;; esac
    enabled=$(read_key ANGLE_FEATURE_OVERRIDES_ENABLED)
    case ":$enabled:" in *:disableMetalOnNvidia:*) exit 0 ;; esac
    [ -n "$current_platform" ] || set_platform=1
    [ -n "$current_driver" ] || set_driver=1
    [ "$set_platform$set_driver" != 00 ] || exit 0

    previous=$owned
    if [ "$set_platform" = 1 ]; then
        case "$owned" in '') owned=p ;; d) owned=pd ;; esac
    fi
    if [ "$set_driver" = 1 ]; then
        case "$owned" in '') owned=d ;; p) owned=pd ;; esac
    fi
    "$ctl" setenv "$marker" "$owned"
    if { [ "$set_platform" = 0 ] || "$ctl" setenv "$platform" metal; } &&
       { [ "$set_driver" = 0 ] || "$ctl" setenv "$driver" 1; }; then
        exit 0
    fi
    # Roll back changes from this invocation if launchd declines the second write.
    if [ "$set_platform" = 1 ] && [ "$(read_key "$platform")" = metal ]; then
        "$ctl" unsetenv "$platform"
    fi
    if [ -n "$previous" ]; then "$ctl" setenv "$marker" "$previous"; else "$ctl" unsetenv "$marker"; fi
    exit 1 ;;
undo)
    case "$owned" in *p*) [ "$(read_key "$platform")" != metal ] || "$ctl" unsetenv "$platform" ;; esac
    case "$owned" in *d*) [ "$(read_key "$driver")" != 1 ] || "$ctl" unsetenv "$driver" ;; esac
    [ -z "$owned" ] || "$ctl" unsetenv "$marker" ;;
*) echo "Usage: session-defaults.sh apply|undo" >&2; exit 2 ;;
esac
