from pathlib import Path
import difflib
import sys

stage = Path(sys.argv[1]).resolve()
pin = '80c3d5e175f39c2d2bbd267cd583842140154140'


def once(text, before, after):
    if text.count(before) != 1:
        raise RuntimeError('Upstream patch anchor mismatch: ' + before[:100])
    return text.replace(before, after, 1)


original = (stage / 'upstream-reinstall.sh').read_text()
text = once(original, 'confhome=https://raw.githubusercontent.com/bin456789/reinstall/main',
            'confhome=https://raw.githubusercontent.com/bin456789/reinstall/' + pin)
text = once(text, 'confhome_cn=https://cnb.cool/bin456789/reinstall/-/git/raw/main', 'confhome_cn=')
if text.count('command curl --insecure --connect-timeout') != 2:
    raise RuntimeError('Unexpected curl wrapper count')
text = text.replace('command curl --insecure --connect-timeout', 'command curl --connect-timeout')
text = once(text, '    cmdline="$nextos_cmdline $finalos_cmdline $extra_cmdline"',
            '    cmdline="$nextos_cmdline $finalos_cmdline $extra_cmdline panic=60"')
text = once(text, '    find . | cpio --quiet -o -H newc -R 0:0 | gzip -1 >/reinstall-initrd',
            '    python3 ' + str(stage / 'helpers/inject_initrd.py') + ' ' + str(stage) + ' "$initrd_dir"\n'
            '    find . | cpio --quiet -o -H newc -R 0:0 | gzip -1 >/reinstall-initrd\n    chmod 600 /reinstall-initrd')
(stage / 'reinstall.sh').write_text(text)
(stage / 'reinstall.diff').write_text(''.join(difflib.unified_diff(original.splitlines(True), text.splitlines(True), fromfile='upstream/reinstall.sh', tofile='patched/reinstall.sh')))

original = (stage / 'upstream-trans.sh').read_text()
text = once(original, 'trans() {\n    info "start trans"',
            'trans() {\n    umask 022\n    /configs/vps-reset/preflight.sh\n    info "start trans"')
text = once(text, '    wipefs -a -f /dev/$xda',
            '    /configs/vps-reset/gate.sh "/dev/$xda"\n    wipefs -a -f /dev/$xda')
text = once(text, '# 设置密码，添加开机启动 + 开启 ssh 服务\nadd_user_if_need /',
            '# Preserve the authenticated SSH host identity before Alpine SSH starts.\n'
            'cp -p /configs/vps-reset/host-keys/ssh_host_* /etc/ssh/\n'
            '# 设置密码，添加开机启动 + 开启 ssh 服务\nadd_user_if_need /')
text = once(text, '    # sshd\n    chroot $os_dir ssh-keygen -A',
            '    cp -p /configs/vps-reset/host-keys/ssh_host_* "$os_dir/etc/ssh/"\n'
            '    # sshd\n    chroot $os_dir ssh-keygen -A')
start = text.index('    if [ -n "$img_type_warp" ]; then', text.index('\ndownload_qcow() {'))
end = text.index('\n}\n\nconnect_qcow()', start)
text = text[:start] + '''    . /configs/vps-reset/plan.env
    cp /vps-reset-image.qcow2 "$qcow_file"
    printf '%s  %s\\n' "$IMAGE_SHA" "$qcow_file" | sha256sum -c -
    rm -f /vps-reset-image.qcow2
    echo disk-image-verified > /configs/vps-reset/phase
''' .rstrip() + text[end:]
text = once(text, 'get_cloud_image_part_size() {',
            'get_cloud_image_part_size() {\n'
            '    . /configs/vps-reset/plan.env\n'
            '    echo "$(get_part_size_mb_for_file_size_b "$IMAGE_BYTES")MiB"\n    return\n')
text = once(text, '        setup_web_if_enough_ram\n', '        # Logs are available only over authenticated SSH.\n')
text = once(text, 'if [ "$hold" = 2 ]; then\n    info "hold 2"\n    exit\nfi',
            'if [ "$hold" = 2 ]; then\n    /configs/vps-reset/finalize.sh\nfi')
# Only the Ubuntu copy branch needs this bounded temporary swap target.
text = once(text, '        ubuntu) echo 1024 ;;', '        ubuntu) echo 2048 ;;')
# Resolver files must remain readable by _apt inside the target root.
text = once(text, 'cp_resolv_conf() {', 'cp_resolv_conf() {\n    umask 022')
(stage / 'trans.sh').write_text(text)
(stage / 'trans.diff').write_text(''.join(difflib.unified_diff(original.splitlines(True), text.splitlines(True), fromfile='upstream/trans.sh', tofile='patched/trans.sh')))
print('Pinned source patches applied; every anchor matched exactly once.')
