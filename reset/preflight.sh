#!/bin/ash
set -eu
umask 022
BASE=/configs/vps-reset
. "$BASE/plan.env"
[ "$(stat -f -c %T /)" = tmpfs ] || { echo 'Not running from a RAM filesystem' >&2; exit 1; }
[ ! -e "$BASE/destructive-started" ] || { echo 'Destructive phase already started; do not rerun partitioning' >&2; exit 1; }
sshd -t
if ! grep -q '/community$' /etc/apk/repositories; then
    sed -n 's@/main$@/community@p' /etc/apk/repositories >> /etc/apk/repositories
fi
apk add --no-cache gpgv curl ca-certificates qemu-img parted e2fsprogs-extra wipefs util-linux
if [ "$BOOT_MODE" = efi ]; then apk add --no-cache dosfstools mtools; fi
swapoff -a
modprobe ext4
modprobe nbd nbds_max=1
test -b /dev/nbd0
matches=0
for device in /sys/class/net/*; do
    if [ "$(cat "$device/address")" = "$NIC_MAC" ]; then matches=$((matches + 1)); fi
done
[ "$matches" = 1 ] || { echo 'Expected network device identity is absent or ambiguous' >&2; exit 1; }
ip -4 route get 1.1.1.1
curl -fsS --proto '=https' --connect-timeout 10 --max-time 20 -o /dev/null https://cloud-images.ubuntu.com/
gpgv --keyring "$BASE/ubuntu-cloudimage-keyring.gpg" "$BASE/SHA256SUMS.gpg" "$BASE/SHA256SUMS"
manifest_hash=$(awk -v name="$IMAGE_NAME" '$2 == name || $2 == "*" name { print $1 }' "$BASE/SHA256SUMS")
[ "$manifest_hash" = "$IMAGE_SHA" ] || { echo 'Signed image manifest mismatch' >&2; exit 1; }
available_kib=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
required_kib=$((IMAGE_BYTES / 1024 + RAM_RESERVE_MIB * 1024))
printf 'RAM available before cache: %s KiB; required: %s KiB\n' "$available_kib" "$required_kib"
[ "$available_kib" -ge "$required_kib" ] || { echo 'Not enough actual RAM; original disk remains intact' >&2; exit 1; }
mkdir -p /mnt/vps-reset-source
mount -t ext4 -o ro,noload "/dev/disk/by-uuid/$ROOT_UUID" /mnt/vps-reset-source
source=/mnt/vps-reset-source/var/lib/vps-reset/image.qcow2
test -f "$source"
[ "$(stat -c %s "$source")" = "$IMAGE_BYTES" ]
dd if="$source" of=/vps-reset-image.qcow2 bs=1048576
umount /mnt/vps-reset-source
printf '%s  %s\n' "$IMAGE_SHA" /vps-reset-image.qcow2 | sha256sum -c -
qemu-img check -f qcow2 /vps-reset-image.qcow2
available_kib=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
printf 'RAM available after cache: %s KiB\n' "$available_kib"
[ "$available_kib" -ge "$((RAM_RESERVE_MIB * 1024))" ] || { echo 'Insufficient remaining RAM; no disk erase performed' >&2; exit 1; }
echo preflight-ready > "$BASE/phase"
if [ "$EXTERNAL_READY_GATE" = 1 ]; then
    echo 'Controlled validation: waiting for an authenticated external SSH readiness check.'
    count=0
    while [ ! -f "$BASE/external-ready" ]; do
        count=$((count + 1))
        [ "$count" -le 300 ] || { echo 'External readiness check did not arrive; original disk intact' >&2; exit 1; }
        sleep 1
    done
fi
echo 'Pre-erasure network, SSH, signed image and RAM checks completed.'
