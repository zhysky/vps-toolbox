#!/bin/ash
set -eu
BASE=/configs/vps-reset
. "$BASE/plan.env"
disk=$1
case "$disk" in /dev/[a-z]*[a-z0-9]) ;; *) echo 'Invalid target disk' >&2; exit 1;; esac
[ ! -e "$BASE/destructive-started" ]
[ "$(cat "$BASE/phase")" = preflight-ready ]
[ "$(stat -f -c %T /)" = tmpfs ]
[ "$(blockdev --getsize64 "$disk")" = "$TARGET_DISK_BYTES" ]
actual_ptuuid=$(blkid -s PTUUID -o value "$disk" | tr 'A-F' 'a-f')
[ "$actual_ptuuid" = "$TARGET_PTUUID" ] || { echo 'Disk identity differs from the prepared disk' >&2; exit 1; }
[ "$EXTERNAL_READY_GATE" != 1 ] || test -f "$BASE/external-ready"
printf '%s  %s\n' "$IMAGE_SHA" /vps-reset-image.qcow2 | sha256sum -c -
pid=$(cat "$BASE/guard.pid")
case "$pid" in ''|*[!0-9]*) echo 'Invalid guard PID' >&2; exit 1;; esac
kill -0 "$pid"
case "$(readlink "/proc/$pid/exe")" in */vps-reset-bootguard|*/vps-reset-bootguard\ \(deleted\)) ;; *) echo 'Unexpected guard process' >&2; exit 1;; esac
kill -TERM "$pid"
count=0
while kill -0 "$pid" 2>/dev/null; do
    count=$((count + 1))
    [ "$count" -le 100 ] || { echo 'Watchdog did not terminate; erase cancelled' >&2; exit 1; }
    sleep .1
done
echo 'Watchdog stopped and verified. Entering the explicitly authorized disk rewrite.'
date -u +%Y-%m-%dT%H:%M:%SZ > "$BASE/destructive-started"
printf '%s\n' "$disk" > "$BASE/actual-target"
echo disk-rewrite > "$BASE/phase"
