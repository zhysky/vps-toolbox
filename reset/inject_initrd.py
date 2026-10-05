from pathlib import Path
import os
import re
import shutil
import sys

stage = Path(sys.argv[1]).resolve()
root = Path(sys.argv[2]).resolve()
if not (root / 'init').is_file() or not (root / 'trans.sh').is_file():
    raise RuntimeError('Unexpected upstream initrd layout')
text = (root / 'init').read_text()
matches = list(re.finditer(r'^(?:\$MOCK )?mount -t proc [^\n]* /proc[^\n]*$', text, re.M))
calls = list(re.finditer(r'^[ \t]*configure_ip(?:[ \t]|$)', text, re.M))
if len(matches) != 1 or not calls or matches[0].end() >= calls[0].start():
    raise RuntimeError('Cannot place the watchdog before network initialization')
offset = matches[0].end()
text = text[:offset] + '\n/vps-reset-bootguard 1800 /configs/vps-reset/guard.pid reboot >/dev/null 2>&1 &\n' + text[offset:]
(root / 'init').write_text(text)
shutil.copy2(stage / 'bootguard', root / 'vps-reset-bootguard')
shutil.copy2(stage / 'trans.sh', root / 'trans.sh')
destination = root / 'configs' / 'vps-reset'
destination.mkdir(parents=True, mode=0o700)
for name in ('plan.json', 'root-hash', 'authorized_keys', 'SHA256SUMS', 'SHA256SUMS.gpg', 'ubuntu-cloudimage-keyring.gpg'):
    shutil.copy2(stage / name, destination / name)
    os.chmod(destination / name, 0o600)
shutil.copytree(stage / 'host-keys', destination / 'host-keys')
for name in ('preflight.sh', 'gate.sh', 'finalize.sh', 'finalize.py'):
    shutil.copy2(stage / 'helpers' / name, destination / name)
    os.chmod(destination / name, 0o700)
plan = __import__('json').loads((stage / 'plan.json').read_text())
fields = {'TARGET_DISK_BYTES': plan['disk_bytes'], 'TARGET_PTUUID': plan['disk_ptuuid'], 'ROOT_UUID': plan['root_uuid'],
          'NIC_MAC': plan['mac'], 'IMAGE_BYTES': plan['image_bytes'], 'IMAGE_SHA': plan['image_sha256'],
          'IMAGE_NAME': plan['image_name'], 'RAM_RESERVE_MIB': plan['ram_reserve_mib'],
          'EXTERNAL_READY_GATE': '1' if plan['external_ready_gate'] else '0', 'BOOT_MODE': plan['boot_mode']}
import shlex
(destination / 'plan.env').write_text(''.join(k + '=' + shlex.quote(str(v)) + '\n' for k, v in fields.items()))
os.chmod(destination / 'plan.env', 0o600)
os.chmod(root / 'vps-reset-bootguard', 0o700)
os.chmod(root / 'trans.sh', 0o700)
print('Initrd: early watchdog, preserved SSH identity, signed-image metadata, automatic verified completion.')
