import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

BASE = Path('/var/lib/vps-reset-finalize')
plan = json.loads((BASE / 'plan.json').read_text())
os.umask(0o022)


def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, **kwargs).stdout.strip()


release = Path('/etc/os-release').read_text()
if 'ID=ubuntu' not in release or 'VERSION_ID="26.04"' not in release:
    raise RuntimeError('Target is not the selected Ubuntu release')
ssh = Path('/etc/ssh')
for source in (BASE / 'host-keys').glob('ssh_host_*'):
    shutil.copy2(source, ssh / source.name)
    os.chmod(ssh / source.name, 0o644 if source.suffix == '.pub' else 0o600)
Path('/root/.ssh').mkdir(mode=0o700, exist_ok=True)
shutil.copy2(BASE / 'authorized_keys', '/root/.ssh/authorized_keys')
os.chmod('/root/.ssh/authorized_keys', 0o600)
if plan['keep_root_password']:
    password_hash = (BASE / 'root-hash').read_text().strip()
    if not password_hash.startswith('$') or '\n' in password_hash:
        raise RuntimeError('Invalid preserved password hash')
    run(['chpasswd', '-e'], input='root:' + password_hash + '\n')
    password_hash = None
ssh_dropin = ssh / 'sshd_config.d' / '00-vps-reset-login.conf'
ssh_dropin.parent.mkdir(exist_ok=True)
ssh_dropin.write_text('# Managed by vps-toolbox reset\nPubkeyAuthentication yes\n'
                     'PermitRootLogin ' + ('yes' if plan['keep_root_password'] else 'prohibit-password') + '\n'
                     'PasswordAuthentication ' + ('yes' if plan['keep_root_password'] else 'no') + '\n'
                     'KbdInteractiveAuthentication no\n')
Path('/run/sshd').mkdir(mode=0o755, exist_ok=True)
run(['sshd', '-t'])
policy = dict(line.split(' ', 1) for line in run(['sshd', '-T']).splitlines() if ' ' in line)
if policy.get('pubkeyauthentication') != 'yes' or int(policy.get('port', 0)) != plan['ssh_port']:
    raise RuntimeError('Target SSH public-key policy or port mismatch')
if plan['keep_root_password'] and (policy.get('passwordauthentication') != 'yes' or policy.get('permitrootlogin') != 'yes'):
    raise RuntimeError('Target root-password login policy was not preserved')
hostname = plan['hostname']
if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}', hostname):
    raise RuntimeError('Invalid preserved hostname')
Path('/etc/hostname').write_text(hostname + '\n')
hosts = Path('/etc/hosts').read_text()
if re.search(r'^127\.0\.1\.1\s', hosts, re.M):
    hosts = re.sub(r'^127\.0\.1\.1\s.*$', '127.0.1.1 ' + hostname, hosts, flags=re.M)
else:
    hosts += '\n127.0.1.1 ' + hostname + '\n'
Path('/etc/hosts').write_text(hosts)
if plan.get('timezone') and (Path('/usr/share/zoneinfo') / plan['timezone']).is_file():
    localtime = Path('/etc/localtime')
    localtime.unlink(missing_ok=True)
    localtime.symlink_to('/usr/share/zoneinfo/' + plan['timezone'])
    Path('/etc/timezone').write_text(plan['timezone'] + '\n')
import yaml
networks = []
for path in Path('/etc/netplan').glob('*.yaml'):
    configuration = yaml.safe_load(path.read_text()) or {}
    networks.extend((configuration.get('network', {}).get('ethernets') or {}).values())
matching = [v for v in networks if (v.get('match', {}).get('macaddress') or '').lower() == plan['mac']]
if len(matching) != 1 or (plan['dhcp4'] and matching[0].get('dhcp4') is not True):
    raise RuntimeError('Target Netplan does not preserve the expected NIC identity / DHCP policy')
run(['netplan', 'generate'])
if not Path('/etc/cloud/cloud-init.disabled').exists():
    raise RuntimeError('Expected cloud-init disable marker is absent')
grub = Path('/etc/default/grub.d/99-vps-reset.cfg')
grub.parent.mkdir(exist_ok=True)
consoles = ' '.join(plan['console_arguments'])
if not re.fullmatch(r'[A-Za-z0-9_=, ./:-]*', consoles):
    raise RuntimeError('Unexpected console argument characters')
grub.write_text('# Managed by vps-toolbox reset\nGRUB_FORCE_PARTUUID=""\nGRUB_DISABLE_LINUX_UUID=false\n'
                'GRUB_DISABLE_LINUX_PARTUUID=true\nGRUB_CMDLINE_LINUX_DEFAULT="' + consoles + '"\n')
run(['update-grub'])
kernels = sorted(Path('/boot').glob('vmlinuz-*'))
if not kernels:
    raise RuntimeError('No target kernel installed')
for kernel in kernels:
    version = kernel.name[len('vmlinuz-'):]
    initrd = Path('/boot') / ('initrd.img-' + version)
    if not initrd.is_file() or initrd.stat().st_size < 1024 * 1024 or not (Path('/lib/modules') / version).is_dir():
        raise RuntimeError('Missing kernel modules or full initrd: ' + version)
    run(['apt-mark', 'manual', 'linux-image-' + version])
grub_content = Path('/boot/grub/grub.cfg').read_text()
if not re.search(r'^\s*initrd\s+.*initrd\.img-', grub_content, re.M):
    raise RuntimeError('GRUB has no full initrd entry')
audit = run(['dpkg', '--audit'])
if audit:
    raise RuntimeError('Package database is incomplete: ' + audit[:1000])
# Give a small VPS bounded swap for maintenance; no memory/network tuning is copied.
swap = Path('/swapfile')
if not swap.exists():
    run(['fallocate', '-l', '1G', str(swap)])
    os.chmod(swap, 0o600)
    run(['mkswap', str(swap)])
fstab = Path('/etc/fstab').read_text()
if not re.search(r'^/swapfile\s', fstab, re.M):
    Path('/etc/fstab').write_text(fstab.rstrip() + '\n/swapfile none swap sw 0 0\n')
run(['systemd-machine-id-setup'])
run(['systemctl', 'enable', 'systemd-networkd', 'systemd-resolved', 'ssh.socket'])
log = Path('/var/log/vps-reset')
log.mkdir(mode=0o700, exist_ok=True)
receipt = {'phase': 'target-validated-before-first-boot', 'recorded_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
           'plan': plan, 'kernel_versions': [v.name[len('vmlinuz-'):] for v in kernels],
           'ssh_host_fingerprint': run(['ssh-keygen', '-lf', '/etc/ssh/ssh_host_ed25519_key.pub']),
           'password_login_preserved': plan['keep_root_password'], 'ssh_public_keys_preserved': True,
           'netplan_mac_verified': True, 'dpkg_audit_clean': True, 'full_initrd_verified': True,
           'new_os_reachable': False}
(log / 'reset-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
os.chmod(log / 'reset-receipt.json', 0o600)
# This exact directory was created by finalize.sh for this invocation.
if (BASE / '.owner').read_text().strip() != 'vps-reset-finalize:v1':
    raise RuntimeError('Finalize directory ownership mismatch')
shutil.rmtree(BASE)
print('Target SSH, network, kernel, initrd, GRUB and package checks passed; external first-boot verification remains pending.')
