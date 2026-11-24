#!/bin/sh
# Pause only this wrapper's verified supervisor; leave real ps free to complete.
supervisor_pid=$1
supervisor_start=$2
marker=$3
availability=$4
shift 4
if [ ! -e "$marker" ]; then
    [ "$PPID" = "$supervisor_pid" ] || exit 81
    observed_start=$(/bin/ps -p "$supervisor_pid" -o lstart=)
    [ "$(printf '%s' "$observed_start" | /usr/bin/xargs)" = "$(printf '%s' "$supervisor_start" | /usr/bin/xargs)" ] || exit 82
    /bin/sleep 0.1
    /bin/kill -STOP "$supervisor_pid" || exit 83
    : > "$marker"
elif [ "$availability" = unavailable ]; then
    exit 84
fi
exec /bin/ps "$@"
