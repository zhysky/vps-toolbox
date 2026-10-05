#!/usr/bin/env python3
"""Prepare a guarded Ubuntu 26.04 minimal reset on a single-disk KVM VPS."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys

PIN = '80c3d5e175f39c2d2bbd267cd583842140154140'
IMAGE = 'ubuntu-26.04-minimal-cloudimg-amd64.img'
IMAGE_BASE = 'https://cloud-images.ubuntu.com/minimal/releases/resolute/release-20261002/'
IMAGE_SHA = 'bae05f5515b205ff3689105a862884103a6bbf2a1f5eac4fd5c8f639270fee7f'
IMAGE_BYTES = 427122688
SOURCE_HASHES = {'reinstall.sh': 'b7ae1e185355d9e1ef0635828f9e7bbd0393475e38e1df72cba87fa415c7a255',
                 'trans.sh': 'bb445e74c78fb26e2afcfa74980521938a8d0c5cf8d2a8cfd28b6e4940873091'}
STATE = Path('/var/lib/vps-reset')
MARKER = 'vps-toolbox-reset:v1'
BOOT_PREPARATION_STARTED = False


def run(argv, capture=False, **kwargs):
    result = subprocess.run(argv, check=True, text=True, capture_output=capture, **kwargs)
    return result.stdout.strip() if capture else None


def write_private(path, text):
    Path(path).write_text(text, encoding='utf-8')
    os.chmod(path, 0o600)


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def download(url, path, expected=None):
    run(['curl', '--fail', '--silent', '--show-error', '--location', '--noproxy', '*', '--proto', '=https',
         '--connect-timeout', '15', '--max-time', '600', '--retry', '2', '--output', str(path), url])
    if expected and digest(path) != expected:
        raise RuntimeError('Download checksum failed: ' + Path(path).name)


def preflight():
    if os.geteuid() != 0 or platform.machine() != 'x86_64' or not Path('/run/systemd/system').is_dir():
        raise RuntimeError('This reset currently supports root on amd64 Ubuntu with systemd.')
    release = Path('/etc/os-release').read_text()
    if not re.search(r'^ID=ubuntu$', release, re.M):
        raise RuntimeError('The reset preparation host must be Ubuntu; detection and optimization have separate support ranges.')
    if run(['systemd-detect-virt', '--vm'], True) != 'kvm':
        raise RuntimeError('This reviewed reset path currently supports KVM VPS only.')
    source = Path(run(['findmnt', '-n', '-o', 'SOURCE', '/'], True)).resolve()
    filesystem = run(['findmnt', '-n', '-o', 'FSTYPE', '/'], True)
    if filesystem != 'ext4' or run(['lsblk', '-ndo', 'TYPE', str(source)], True) != 'part':
        raise RuntimeError('Expected a simple ext4 root partition; LVM, RAID and encrypted-root layouts are not supported.')
    parent = run(['lsblk', '-ndo', 'PKNAME', str(source)], True)
    disk = Path('/dev') / parent
    disks = json.loads(run(['lsblk', '-J', '-b', '-d', '-o', 'PATH,TYPE,SIZE'], True))['blockdevices']
    real_disks = [v for v in disks if v['type'] == 'disk']
    if len(real_disks) != 1 or real_disks[0]['path'] != str(disk):
        raise RuntimeError('Expected exactly one system disk; multi-disk machines require a separate reviewed plan.')
    disk_bytes = int(run(['blockdev', '--getsize64', str(disk)], True))
    memory_kib = int(re.search(r'^MemTotal:\s*(\d+)', Path('/proc/meminfo').read_text(), re.M).group(1))
    if disk_bytes < 10 * 1024 ** 3 or memory_kib < 800 * 1024:
        raise RuntimeError('Requires at least 10 GiB disk and 800 MiB visible RAM; Alpine rechecks actual available RAM before erasure.')
    defaults = json.loads(run(['ip', '-j', '-4', 'route', 'show', 'default'], True))
    if len(defaults) != 1:
        raise RuntimeError('Expected one IPv4 default route.')
    interface = defaults[0]['dev']
    mac = Path('/sys/class/net/' + interface + '/address').read_text().strip().lower()
    if not re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}', mac):
        raise RuntimeError('Invalid interface identity')
    policy = dict(line.split(' ', 1) for line in run(['sshd', '-T'], True).splitlines() if ' ' in line)
    if policy.get('pubkeyauthentication') != 'yes':
        raise RuntimeError('Public-key SSH must be enabled before the RAM boot.')
    keys = Path('/root/.ssh/authorized_keys')
    if not keys.is_file() or not any(line.strip() and not line.startswith('#') for line in keys.read_text().splitlines()):
        raise RuntimeError('Install and test your root SSH public key first; the Alpine stage uses this key.')
    run(['ssh-keygen', '-l', '-f', str(keys)], capture=True)
    efi = Path('/sys/firmware/efi').is_dir()
    if efi:
        secure = list(Path('/sys/firmware/efi/efivars').glob('SecureBoot-*'))
        if secure and secure[0].read_bytes()[-1] == 1:
            raise RuntimeError('Secure Boot is enabled; this Alpine boot path is unsupported.')
    if not Path('/boot/grub/grub.cfg').is_file() or not Path('/boot/grub/grubenv').is_file():
        raise RuntimeError('A working GRUB configuration and environment are required.')
    if re.search(r'^next_entry=.+', run(['grub-editenv', '/boot/grub/grubenv', 'list'], True), re.M):
        raise RuntimeError('Another one-shot GRUB boot is already pending.')
    if efi and list(Path('/sys/firmware/efi/efivars').glob('BootNext-*')):
        raise RuntimeError('Another one-shot EFI boot is already pending.')
    root_hash = next(line.split(':')[1] for line in Path('/etc/shadow').read_text().splitlines() if line.startswith('root:'))
    keep_password = policy.get('passwordauthentication') == 'yes' and root_hash.startswith('$')
    ssh_connection = os.environ.get('SSH_CONNECTION', '').split()
    port = int(ssh_connection[-1]) if len(ssh_connection) == 4 else int(policy['port'])
    plan = {'version': 1, 'prepared_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'disk': str(disk), 'disk_bytes': disk_bytes, 'root_partition': str(source),
            'root_uuid': run(['blkid', '-s', 'UUID', '-o', 'value', str(source)], True),
            'disk_ptuuid': run(['blkid', '-s', 'PTUUID', '-o', 'value', str(disk)], True).lower(),
            'boot_mode': 'efi' if efi else 'bios', 'interface': interface, 'mac': mac,
            'default_route': defaults[0], 'dhcp4': defaults[0].get('protocol') == 'dhcp',
            'hostname': platform.node(), 'ssh_port': port, 'keep_root_password': keep_password,
            'timezone': run(['timedatectl', 'show', '-p', 'Timezone', '--value'], True),
            'permit_root_login': policy.get('permitrootlogin'), 'memory_kib': memory_kib,
            'console_arguments': [v for v in Path('/proc/cmdline').read_text().split() if v.startswith('console=')],
            'image_url': IMAGE_BASE + IMAGE, 'image_name': IMAGE, 'image_bytes': IMAGE_BYTES,
            'image_sha256': IMAGE_SHA, 'upstream_commit': PIN}
    return plan, root_hash


def main():
    global BOOT_PREPARATION_STARTED
    parser = argparse.ArgumentParser(description='将单盘 KVM Ubuntu VPS 重置为 Ubuntu 26.04 minimal；清空整个系统盘。')
    parser.add_argument('--yes', action='store_true', help='已明确授权擦盘；必须同时给出独立备份回执')
    parser.add_argument('--backup-receipt', help='独立备份已经验证的 JSON 回执')
    parser.add_argument('--prepare-only', action='store_true', help='准备并检查启动项，暂不重启')
    parser.add_argument('--external-ready-gate', action='store_true', help='受控验收模式：RAM 阶段等待外部 SSH 验证标记')
    parser.add_argument('--ram-reserve-mib', type=int, default=192)
    parser.add_argument('--check', action='store_true', help='只检查适用条件')
    parser.add_argument('--cancel', action='store_true', help='在重启前取消本工具准备的一次性重装启动项')
    args = parser.parse_args()
    os.umask(0o022)
    if args.cancel:
        if not (STATE / '.owner').is_file() or (STATE / '.owner').read_text().strip() != MARKER:
            raise RuntimeError('No owned reset preparation found')
        run(['bash', str(STATE / 'reinstall.sh'), 'reset'])
        print('Pending reset boot cancelled. Staging files retained for inspection.')
        return
    plan, root_hash = preflight()
    if not 192 <= args.ram_reserve_mib <= 512:
        raise RuntimeError('RAM reserve must be 192..512 MiB')
    plan['ram_reserve_mib'] = args.ram_reserve_mib
    plan['external_ready_gate'] = args.external_ready_gate
    if args.check:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return
    if STATE.exists():
        raise RuntimeError('A reset staging directory already exists. Inspect it; do not silently overwrite a pending reset.')
    if args.backup_receipt:
        receipt = json.loads(Path(args.backup_receipt).read_text())
        if not receipt.get('independent_backup_verified') or not receipt.get('decrypted_archive_read_verified'):
            raise RuntimeError('Backup receipt does not confirm independent recovery verification.')
        plan['backup_receipt'] = receipt
    elif args.yes:
        raise RuntimeError('--yes requires --backup-receipt')
    else:
        print('将清空整个系统盘 %s（%.1f GiB），安装 Ubuntu 26.04 minimal。' % (plan['disk'], plan['disk_bytes'] / 1024 ** 3))
        print('请先在其他机器保存并验证所需备份。重装保留 root SSH 公钥、主机密钥以及当前可用的密码登录。')
        expected = 'ERASE ' + plan['disk']
        if input('确认备份后输入 ' + expected + '：').strip() != expected:
            raise RuntimeError('Cancelled')
        plan['backup_receipt'] = {'independent_backup_verified': True, 'user_confirmed': True}
    STATE.mkdir(mode=0o700)
    write_private(STATE / '.owner', MARKER + '\n')
    write_private(STATE / 'plan.json', json.dumps(plan, indent=2) + '\n')
    write_private(STATE / 'root-hash', root_hash + '\n')
    write_private(STATE / 'authorized_keys', Path('/root/.ssh/authorized_keys').read_text())
    (STATE / 'host-keys').mkdir(mode=0o700)
    for source in Path('/etc/ssh').glob('ssh_host_*'):
        if source.is_file():
            shutil.copy2(source, STATE / 'host-keys' / source.name)
            os.chmod(STATE / 'host-keys' / source.name, 0o644 if source.suffix == '.pub' else 0o600)
    shutil.copytree(Path(__file__).resolve().parent, STATE / 'helpers', ignore=shutil.ignore_patterns('__pycache__'))
    env = dict(os.environ, DEBIAN_FRONTEND='noninteractive', NEEDRESTART_MODE='l')
    run(['apt-get', '-o', 'DPkg::Lock::Timeout=120', 'update', '-qq'], env=env)
    run(['apt-get', '-o', 'DPkg::Lock::Timeout=120', 'install', '-y', '--no-install-recommends',
         'ca-certificates', 'curl', 'python3', 'gcc', 'libc6-dev', 'gpgv', 'cpio', 'gzip', 'file'], env=env)
    keyring = Path('/usr/share/keyrings/ubuntu-cloudimage-keyring.gpg')
    if not keyring.is_file():
        raise RuntimeError('Trusted Ubuntu cloud-image keyring is absent; no boot changes made.')
    shutil.copy2(keyring, STATE / 'ubuntu-cloudimage-keyring.gpg')
    for name in ('SHA256SUMS', 'SHA256SUMS.gpg'):
        download(IMAGE_BASE + name, STATE / name)
    signature = run(['gpgv', '--status-fd', '1', '--keyring', str(keyring), str(STATE / 'SHA256SUMS.gpg'), str(STATE / 'SHA256SUMS')], True)
    write_private(STATE / 'signature-verification.txt', signature + '\n')
    entries = [line.split() for line in (STATE / 'SHA256SUMS').read_text().splitlines()]
    if [v[0] for v in entries if len(v) == 2 and v[1].lstrip('*') == IMAGE] != [IMAGE_SHA]:
        raise RuntimeError('Signed image digest differs from the reviewed image.')
    print('Downloading and verifying the official minimal image before changing boot state.', flush=True)
    download(plan['image_url'], STATE / 'image.qcow2', IMAGE_SHA)
    if (STATE / 'image.qcow2').stat().st_size != IMAGE_BYTES:
        raise RuntimeError('Image size mismatch')
    for name, expected in SOURCE_HASHES.items():
        download('https://raw.githubusercontent.com/bin456789/reinstall/' + PIN + '/' + name,
                 STATE / ('upstream-' + name), expected)
    run(['gcc', '-static', '-O2', '-Wall', '-Wextra', '-Werror', str(STATE / 'helpers' / 'bootguard.c'), '-o', str(STATE / 'bootguard')])
    run([str(STATE / 'bootguard'), '1', str(STATE / 'guard-test.pid'), 'dry-run'])
    guard = subprocess.Popen([str(STATE / 'bootguard'), '60', str(STATE / 'guard-cancel.pid'), 'dry-run'])
    import time
    time.sleep(.2)
    guard.terminate()
    if guard.wait(timeout=3) != 0:
        raise RuntimeError('Boot guard cancellation test failed')
    run([sys.executable, str(STATE / 'helpers' / 'patch_sources.py'), str(STATE)])
    run(['bash', '-n', str(STATE / 'reinstall.sh')])
    (STATE / 'original-boot').mkdir(mode=0o700)
    for source in ('/etc/default/grub', '/boot/grub/grub.cfg', '/boot/grub/grubenv'):
        shutil.copy2(source, STATE / 'original-boot' / Path(source).name)
    write_private(STATE / 'original-partitions.sfdisk', run(['sfdisk', '--dump', plan['disk']], True) + '\n')
    BOOT_PREPARATION_STARTED = True
    run(['bash', str(STATE / 'reinstall.sh'), 'ubuntu', '26.04', '--minimal', '--username', 'root',
         '--ssh-key', str(STATE / 'authorized_keys'), '--ssh-port', str(plan['ssh_port']), '--target-disk', plan['disk'],
         '--hold', '2', '--img', plan['image_url']], cwd=STATE)
    os.chmod('/reinstall-initrd', 0o600)
    if plan['boot_mode'] == 'efi':
        boot_text = run(['efibootmgr', '-v'], True)
        next_boot = re.search(r'^BootNext:\s*([0-9A-Fa-f]{4})$', boot_text, re.M)
        if not next_boot or not re.search(r'^Boot' + next_boot.group(1) + r'\*?.*reinstall', boot_text, re.M | re.I):
            raise RuntimeError('EFI BootNext does not point to the prepared reinstall entry')
        write_private(STATE / 'prepared-efi.txt', boot_text + '\n')
    else:
        env_text = run(['grub-editenv', '/boot/grub/grubenv', 'list'], True)
        if 'next_entry=' not in env_text:
            raise RuntimeError('No one-shot GRUB boot was prepared')
    listing = subprocess.run('gzip -dc /reinstall-initrd | cpio -it --quiet', shell=True, text=True, capture_output=True, check=True).stdout
    for required in ('vps-reset-bootguard', 'configs/vps-reset/preflight.sh', 'configs/vps-reset/finalize.sh', 'configs/vps-reset/plan.json'):
        if required not in listing.splitlines():
            raise RuntimeError('Prepared initrd is missing ' + required)
    plan['phase'] = 'prepared'
    write_private(STATE / 'plan.json', json.dumps(plan, indent=2) + '\n')
    run(['sync'])
    print('Preparation verified. The next boot enters the guarded RAM installer.', flush=True)
    if not args.prepare_only:
        run(['systemd-run', '--unit=vps-reset-reboot', '--on-active=5s', '/usr/sbin/reboot'])


if __name__ == '__main__':
    try:
        main()
    except BaseException:
        # A failed preparation must not silently leave a destructive next boot armed.
        if BOOT_PREPARATION_STARTED and (STATE / 'original-boot').is_dir() and (STATE / 'reinstall.sh').is_file():
            cancelled = subprocess.run(['bash', str(STATE / 'reinstall.sh'), 'reset'], text=True,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print('Preparation failed; one-shot reset cancellation ' + ('completed.' if cancelled.returncode == 0 else 'FAILED; inspect boot state before reboot.'), file=sys.stderr)
        raise
