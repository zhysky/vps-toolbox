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
if [ -e "$destination" ]; then
    [ "$(cat "$destination/.owner")" = vps-reset-finalize:v1 ]
else
    mkdir -m 0700 "$destination"
    printf '%s\n' vps-reset-finalize:v1 > "$destination/.owner"
fi
cp "$BASE/plan.json" "$BASE/root-hash" "$BASE/authorized_keys" "$BASE/finalize.py" "$destination/"
mkdir -p "$destination/host-keys"
cp -p "$BASE/host-keys/"* "$destination/host-keys/"
chmod 0600 "$destination/plan.json" "$destination/root-hash" "$destination/authorized_keys"
# Minimal cloud images intentionally omit initramfs generators. Install one and
# build a full initrd before removing their initrd-less GRUB shortcut.
if [ ! -f /os/swapfile ]; then
    fallocate -l 1G /os/swapfile
    chmod 0600 /os/swapfile
    mkswap /os/swapfile
fi
if ! grep -q '^/os/swapfile ' /proc/swaps; then swapon /os/swapfile; fi
resolver=/os/etc/resolv.conf
resolver_backup=/os/etc/resolv.conf.vps-reset-original
restore_resolver() {
    if [ -e "$resolver_backup" ] || [ -L "$resolver_backup" ]; then
        rm -f "$resolver"
        mv "$resolver_backup" "$resolver"
    fi
}
trap restore_resolver EXIT
if [ ! -e "$resolver_backup" ] && [ ! -L "$resolver_backup" ]; then mv "$resolver" "$resolver_backup"; fi
cp /etc/resolv.conf "$resolver"
chmod 0644 "$resolver"
if [ ! -x /os/usr/sbin/update-initramfs ]; then
    DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l chroot /os apt-get -o DPkg::Lock::Timeout=120 update -qq
    DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l chroot /os apt-get -o DPkg::Lock::Timeout=120 install -y --no-install-recommends initramfs-tools
fi
for kernel in /os/boot/vmlinuz-*; do
    version=${kernel##*/vmlinuz-}
    if [ ! -s "/os/boot/initrd.img-$version" ]; then
        chroot /os update-initramfs -c -k "$version"
    fi
done
restore_resolver
trap - EXIT
chroot /os /usr/bin/python3 /var/lib/vps-reset-finalize/finalize.py
mkdir -p /os/var/log/vps-reset
chmod 0700 /os/var/log/vps-reset
cp /reinstall.log /os/var/log/vps-reset/install.log
chmod 0600 /os/var/log/vps-reset/install.log
echo final-validated > "$BASE/phase"
sync
echo 'Target system validation passed. Rebooting into the new Ubuntu installation.'
# The upstream success path performs sync and reboot after this hook returns.
