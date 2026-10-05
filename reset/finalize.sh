#!/bin/ash
set -eu
umask 022
BASE=/configs/vps-reset
. "$BASE/plan.env"
test -f "$BASE/destructive-started"
test -x /os/usr/bin/python3
if [ "$BOOT_MODE" = efi ]; then
    test -f /os/boot/efi/EFI/BOOT/BOOTX64.EFI
    disk=$(cat "$BASE/actual-target")
    esp=$(lsblk -nr -o PATH,PARTTYPE "$disk" | awk 'tolower($2)=="c12a7328-f81f-11d2-ba4b-00a0c93ec93b" { print $1 }')
    test -n "$esp"
    esp_uuid=$(blkid -s PARTUUID -o value "$esp")
    efibootmgr -v > "$BASE/final-efi.txt"
    first=$(sed -n 's/^BootOrder: \([^,]*\).*/\1/p' "$BASE/final-efi.txt")
    grep -i "^Boot$first" "$BASE/final-efi.txt" | grep -Fi "$esp_uuid" | grep -Fi 'bootx64.efi'
fi
destination=/os/var/lib/vps-reset-finalize
test ! -e "$destination"
mkdir -m 0700 "$destination"
cp "$BASE/plan.json" "$BASE/root-hash" "$BASE/authorized_keys" "$BASE/finalize.py" "$destination/"
cp -a "$BASE/host-keys" "$destination/"
chmod 0600 "$destination/plan.json" "$destination/root-hash" "$destination/authorized_keys"
chroot /os /usr/bin/python3 /var/lib/vps-reset-finalize/finalize.py
mkdir -p /os/var/log/vps-reset
chmod 0700 /os/var/log/vps-reset
cp /reinstall.log /os/var/log/vps-reset/install.log
chmod 0600 /os/var/log/vps-reset/install.log
echo final-validated > "$BASE/phase"
sync
echo 'Target system validation passed. Rebooting into the new Ubuntu installation.'
# The upstream success path performs sync and reboot after this hook returns.
