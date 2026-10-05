#!/usr/bin/env python3
"""Stock-kernel BBR/FQ tuning with bounded options and an independent rollback timer."""
import argparse
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import uuid

STATE = Path('/var/lib/vps-net-opt')
PROGRAM = Path('/opt/vps-net-opt')
MARKER = 'vps-toolbox-network:v1'
SYSCTL_FILE = Path('/etc/sysctl.d/99-vps-net-opt.conf')
MODULE_FILE = Path('/etc/modules-load.d/vps-net-opt.conf')
UNIT_FILE = Path('/etc/systemd/system/vps-net-opt.service')
KEYS = ['net.ipv4.tcp_congestion_control', 'net.core.default_qdisc', 'net.core.rmem_max',
        'net.core.wmem_max', 'net.ipv4.tcp_rmem', 'net.ipv4.tcp_wmem']
DEFAULT_PRIOMAP = [1, 2, 2, 2, 1, 2, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1]


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def run(args, timeout=20, check=True):
    result = subprocess.run([str(v) for v in args], capture_output=True, text=True, timeout=timeout,
                            env=dict(os.environ, LC_ALL='C'))
    if check and result.returncode:
        raise RuntimeError('Command failed: %s: %s' % (' '.join(map(str, args)), result.stderr.strip()[:2000]))
    return result.stdout.strip()


def save(state):
    temporary = STATE / 'state.json.new'
    temporary.write_text(json.dumps(state, indent=2) + '\n')
    os.chmod(temporary, 0o600)
    os.replace(temporary, STATE / 'state.json')


def check_paths():
    for path in [STATE, PROGRAM, SYSCTL_FILE, MODULE_FILE, UNIT_FILE]:
        if path.is_symlink() or path.resolve() != path:
            raise RuntimeError('Refusing symlinked management path: ' + str(path))


def load():
    check_paths()
    if not (STATE / '.owner').is_file() or (STATE / '.owner').read_text().strip() != MARKER:
        raise RuntimeError('No installation owned by this optimizer')
    state = json.loads((STATE / 'state.json').read_text())
    if state.get('schema_version') != 1 or state.get('machine_id') != Path('/etc/machine-id').read_text().strip():
        raise RuntimeError('State version or machine identity mismatch')
    return state


def resolve_interface(mac):
    matches = [p.name for p in Path('/sys/class/net').iterdir() if (p / 'address').read_text().strip().lower() == mac]
    if len(matches) != 1:
        raise RuntimeError('The recorded NIC MAC is absent or ambiguous')
    return matches[0]


def queue(device):
    rows = json.loads(run(['tc', '-j', 'qdisc', 'show', 'dev', device]))
    return [{k: value for k, value in row.items() if k in ('kind', 'handle', 'parent', 'root', 'options')} for row in rows]


def factory_queue(rows):
    if len(rows) != 1 or not rows[0].get('root'):
        return False
    options = rows[0].get('options', {})
    if rows[0].get('kind') == 'pfifo_fast':
        return set(options) <= {'bands', 'priomap', 'multiqueue'} and options.get('bands', 3) == 3 and options.get('priomap', DEFAULT_PRIOMAP) == DEFAULT_PRIOMAP
    if rows[0].get('kind') == 'fq_codel':
        # Preserve only a fully modeled simple factory CoDel configuration.
        # iproute2 can display nominal 5/100 ms as 4999/99999 microseconds.
        return (set(options) <= {'limit', 'flows', 'quantum', 'target', 'interval', 'memory_limit', 'ecn', 'drop_batch'}
                and options.get('limit') == 10240 and options.get('flows') == 1024
                and isinstance(options.get('quantum'), int) and 64 <= options['quantum'] <= 65535
                and options.get('target') in (4999, 5000) and options.get('interval') in (99999, 100000)
                and options.get('memory_limit') == 33554432 and options.get('ecn') is True
                and options.get('drop_batch') == 64)
    return False


def restore_queue_command(device, rows):
    if not factory_queue(rows):
        raise RuntimeError('Cannot reconstruct this original queue')
    args = ['tc', 'qdisc', 'replace', 'dev', device, 'root', rows[0]['kind']]
    if rows[0]['kind'] == 'fq_codel':
        options = rows[0]['options']
        args += ['limit', str(options['limit']), 'flows', str(options['flows']), 'quantum', str(options['quantum']),
                 'target', '5ms', 'interval', '100ms', 'memory_limit', str(options['memory_limit']), 'ecn', 'drop_batch', '64']
    return args


def facts(interface=None):
    if interface is None:
        routes = json.loads(run(['ip', '-j', '-4', 'route', 'get', '1.1.1.1']))
        if len(routes) != 1 or not routes[0].get('dev'):
            raise RuntimeError('Unable to identify a unique IPv4 egress interface')
        interface = routes[0]['dev']
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,64}', interface) or not (Path('/sys/class/net') / interface).is_dir():
        raise RuntimeError('Invalid interface')
    return {'interface': interface, 'mac': (Path('/sys/class/net') / interface / 'address').read_text().strip().lower(),
            'sysctls': {key: run(['sysctl', '-n', key]) for key in KEYS}, 'qdisc': queue(interface),
            'filters': json.loads(run(['tc', '-j', 'filter', 'show', 'dev', interface])),
            'classes': json.loads(run(['tc', '-j', 'class', 'show', 'dev', interface])),
            'memory_kib': int(re.search(r'^MemTotal:\s*(\d+)', Path('/proc/meminfo').read_text(), re.M).group(1)),
            'recorded_utc': now()}


def set_sysctls(values):
    for key, value in values.items():
        if key not in KEYS or '\n' in value:
            raise RuntimeError('Unexpected saved sysctl')
        run(['sysctl', '-w', key + '=' + value])


def apply_queue(state):
    device = resolve_interface(state['before']['mac'])
    rate = state['desired']['rate_mbps']
    if rate:
        burst = max(16384, min(1024 * 1024, int(rate * 1000000 / 8 * .02)))
        run(['tc', 'qdisc', 'replace', 'dev', device, 'root', 'handle', '1:', 'tbf',
             'rate', str(int(rate * 1000000)) + 'bit', 'burst', str(burst), 'latency', '50ms'])
        run(['tc', 'qdisc', 'replace', 'dev', device, 'parent', '1:1', 'handle', '10:',
             'fq', 'limit', '512', 'flow_limit', '64'])
    else:
        run(['tc', 'qdisc', 'replace', 'dev', device, 'root', 'handle', '1:', 'fq', 'limit', '2000', 'flow_limit', '100'])
    return queue(device)


def stop_timer(state):
    timer = 'vps-net-rollback-' + state['run_id'] + '.timer'
    run(['systemctl', 'stop', timer], check=False)
    return run(['systemctl', 'is-active', timer], check=False) != 'active'


def verify_current(state):
    current = facts(resolve_interface(state['before']['mac']))
    for key, value in state['desired']['sysctls'].items():
        if current['sysctls'][key].split() != value.split():
            raise RuntimeError('Applied setting drifted: ' + key)
    if current['qdisc'] != state.get('applied_qdisc'):
        raise RuntimeError('Queue topology/options changed after application; refusing to commit')
    if current['filters'] or current['classes']:
        # TBF exposes one internal class; inspect it separately instead of treating it as a foreign classifier.
        if current['filters'] or not state['desired']['rate_mbps'] or any(v.get('kind') != 'tbf' for v in current['classes']):
            raise RuntimeError('Unexpected class/filter state')
    return current


def restore(state, automatic=False):
    if automatic and state['phase'] != 'pending':
        return {'restored': False, 'reason': 'transaction is no longer pending'}
    if state['phase'] == 'rolled_back':
        return {'restored': True, 'reason': 'already rolled back'}
    device = resolve_interface(state['before']['mac'])
    # Do not destroy a queue another administrator installed after this transaction.
    current = queue(device)
    if state.get('applied_qdisc') and current != state['applied_qdisc'] and current != state['before']['qdisc']:
        raise RuntimeError('Current queue is not the recorded optimizer queue; manual inspection required')
    for path in [SYSCTL_FILE, MODULE_FILE, UNIT_FILE]:
        if path.exists() and ('# Managed by ' + MARKER) not in path.read_text():
            raise RuntimeError('Persistent file ownership changed: ' + str(path))
    run(['systemctl', 'disable', '--now', 'vps-net-opt.service'], check=False)
    set_sysctls(state['before']['sysctls'])
    run(restore_queue_command(device, state['before']['qdisc']))
    for path in [SYSCTL_FILE, MODULE_FILE, UNIT_FILE]:
        path.unlink(missing_ok=True)
    run(['systemctl', 'daemon-reload'])
    after = facts(device)
    if (not factory_queue(after['qdisc']) or after['qdisc'][0]['kind'] != state['before']['qdisc'][0]['kind']
            or any(after['sysctls'][k].split() != v.split() for k, v in state['before']['sysctls'].items())):
        raise RuntimeError('Rollback verification failed')
    state.update(phase='rolled_back', rolled_back_utc=now(), rollback_verified=True, after_rollback=after)
    save(state)
    stop_timer(state)
    return {'restored': True, 'original_queue_type_parameters_restored': True,
            'queue_handles_counters_and_queued_packets_restored': False}


def persist(state):
    marker = '# Managed by ' + MARKER + '\n'
    SYSCTL_FILE.write_text(marker + ''.join(k + ' = ' + v + '\n' for k, v in state['desired']['sysctls'].items()))
    MODULE_FILE.write_text(marker + ('tcp_bbr\n' if state['desired']['cc'] == 'bbr' else '') + 'sch_fq\n' + ('sch_tbf\n' if state['desired']['rate_mbps'] else ''))
    UNIT_FILE.write_text(marker + '''[Unit]
Description=Apply measured VPS network queue
Wants=network-online.target
After=network-online.target systemd-sysctl.service
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/vps-net-opt/network_optimize.py persist-queue
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
''')
    for path in [SYSCTL_FILE, MODULE_FILE, UNIT_FILE]:
        os.chmod(path, 0o644)
    run(['systemd-analyze', 'verify', str(UNIT_FILE)])
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'enable', 'vps-net-opt.service'])


def commit(state):
    if state['phase'] == 'committed':
        return {'committed': True, 'reason': 'already committed'}
    if state['phase'] != 'pending':
        raise RuntimeError('No pending transaction to commit')
    verified = verify_current(state)
    persist(state)
    state.update(phase='committed', committed_utc=now(), verified=verified)
    save(state)
    if not stop_timer(state):
        raise RuntimeError('Settings committed, but rollback timer stop was not confirmed')
    return {'committed': True, 'run_id': state['run_id'], 'reboot_persistence_tested': False}


def apply(args):
    check_paths()
    if STATE.exists():
        previous = load()
        if previous['phase'] != 'rolled_back':
            raise RuntimeError('An optimizer transaction already exists; inspect status or roll it back first')
        archive = STATE / ('history-' + previous['run_id'] + '.json')
        if not archive.exists():
            shutil.copy2(STATE / 'state.json', archive)
    elif PROGRAM.exists():
        raise RuntimeError('Program path exists without owned state')
    for path in [SYSCTL_FILE, MODULE_FILE, UNIT_FILE]:
        if path.exists():
            raise RuntimeError('Persistent path already exists; refusing to overwrite: ' + str(path))
    before = facts(args.interface)
    if not factory_queue(before['qdisc']) or before['filters'] or before['classes']:
        raise RuntimeError('Only a simple default pfifo_fast/fq_codel root with no filters/classes is supported; complex queues are unchanged.')
    if args.buffer_mib and before['memory_kib'] < (512 if args.buffer_mib == 8 else 2048 if args.buffer_mib == 16 else 4096) * 1024:
        raise RuntimeError('Selected buffer candidate is too large for this memory tier')
    if args.cc == 'bbr':
        run(['modprobe', 'tcp_bbr'])
    run(['modprobe', 'sch_fq'])
    if args.rate_mbps:
        run(['modprobe', 'sch_tbf'])
    if args.cc not in run(['sysctl', '-n', 'net.ipv4.tcp_available_congestion_control']).split():
        raise RuntimeError('Requested congestion control is not available in this installed kernel')
    desired = dict(before['sysctls'])
    desired['net.ipv4.tcp_congestion_control'], desired['net.core.default_qdisc'] = args.cc, 'fq'
    if args.buffer_mib:
        limit = args.buffer_mib * 1024 * 1024
        for key in ('net.core.rmem_max', 'net.core.wmem_max'):
            desired[key] = str(max(int(desired[key]), limit))
        for key in ('net.ipv4.tcp_rmem', 'net.ipv4.tcp_wmem'):
            values = [int(v) for v in desired[key].split()]
            values[2] = max(values[2], limit)
            desired[key] = ' '.join(map(str, values))
    STATE.mkdir(mode=0o700, exist_ok=True)
    PROGRAM.mkdir(mode=0o755, exist_ok=True)
    (STATE / '.owner').write_text(MARKER + '\n')
    target = PROGRAM / 'network_optimize.py'
    if Path(__file__).resolve() != target:
        shutil.copy2(__file__, target)
        os.chmod(target, 0o644)
    state = {'schema_version': 1, 'machine_id': Path('/etc/machine-id').read_text().strip(), 'run_id': uuid.uuid4().hex[:12],
             'phase': 'pending', 'started_utc': now(), 'before': before,
             'desired': {'cc': args.cc, 'buffer_mib': args.buffer_mib, 'rate_mbps': args.rate_mbps, 'sysctls': desired},
             'applied_qdisc': None, 'rollback_seconds': args.rollback_seconds}
    save(state)
    run(['systemd-run', '--unit=vps-net-rollback-' + state['run_id'], '--on-active=' + str(args.rollback_seconds) + 's',
         '/usr/bin/python3', str(target), 'rollback', '--automatic', '--run-id', state['run_id']])
    try:
        set_sysctls(desired)
        state['applied_qdisc'] = apply_queue(state)
        save(state)
        verify_current(state)
        run(['sshd', '-t'])
        run(['ip', '-4', 'route', 'get', '1.1.1.1'])
        result = {'applied': True, 'run_id': state['run_id'], 'guard_seconds': args.rollback_seconds,
                  'desired': state['desired'], 'actual_qdisc': state['applied_qdisc']}
        if not args.manual_commit:
            run(['curl', '-4', '-fsS', '--noproxy', '*', '--connect-timeout', '5', '--max-time', '15',
                 '-o', '/dev/null', 'https://www.cloudflare.com/cdn-cgi/trace'], timeout=20)
            result['commit'] = commit(state)
            result['external_new_ssh_verified'] = False
        return result
    except BaseException:
        try:
            result = restore(state)
            print(json.dumps({'automatic_recovery': result}), file=sys.stderr)
        except Exception as exc:
            print('Automatic recovery needs inspection: ' + str(exc), file=sys.stderr)
        raise


def main():
    parser = argparse.ArgumentParser(description='发行版内核 BBR/FQ 优化；默认保留缓冲，不限制出口速率。')
    sub = parser.add_subparsers(dest='action', required=True)
    candidate = sub.add_parser('apply')
    candidate.add_argument('--interface')
    candidate.add_argument('--cc', choices=['bbr', 'cubic'], default='bbr')
    candidate.add_argument('--buffer-mib', type=int, choices=[0, 8, 16, 32], default=0, help='0 保留缓冲；非零用于有测量依据的候选档')
    candidate.add_argument('--rate-mbps', type=float, default=0, help='0 不限速；非零限制整台 VPS 的出口带宽')
    candidate.add_argument('--manual-commit', action='store_true', help='等待新的外部 SSH 验证后执行 commit，否则定时回滚')
    candidate.add_argument('--rollback-seconds', type=int, default=180)
    sub.add_parser('status')
    sub.add_parser('commit')
    rollback = sub.add_parser('rollback')
    rollback.add_argument('--automatic', action='store_true')
    rollback.add_argument('--run-id')
    sub.add_parser('persist-queue')
    args = parser.parse_args()
    if platform.system() != 'Linux' or os.geteuid() != 0 or not Path('/run/systemd/system').is_dir():
        raise SystemExit('Requires root on Linux with systemd')
    if args.action == 'apply' and (not math.isfinite(args.rate_mbps) or not 0 <= args.rate_mbps <= 100000 or not 60 <= args.rollback_seconds <= 900):
        raise SystemExit('Invalid rate or rollback deadline')
    os.umask(0o077)
    import fcntl
    with open('/run/lock/vps-net-opt.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.action == 'apply':
            result = apply(args)
        elif args.action == 'status':
            result = {'live': facts(), 'state': load() if STATE.exists() else None}
        elif args.action == 'commit':
            result = commit(load())
        elif args.action == 'rollback':
            state = load()
            if args.run_id and args.run_id != state['run_id']:
                result = {'restored': False, 'reason': 'timer belongs to an older transaction'}
            else:
                result = restore(state, args.automatic)
        else:
            state = load()
            if state['phase'] != 'committed':
                raise RuntimeError('Only committed settings are applied on boot')
            device = resolve_interface(state['before']['mac'])
            current = facts(device)
            owned = current['qdisc'] == state.get('applied_qdisc')
            if not (factory_queue(current['qdisc']) or owned):
                raise RuntimeError('Boot queue has an unexpected topology; leaving it unchanged')
            if current['filters']:
                raise RuntimeError('Boot queue has filters; leaving it unchanged')
            state['applied_qdisc'] = apply_queue(state)
            state['persisted_at_boot_utc'] = now()
            save(state)
            result = {'queue_applied': True, 'actual_qdisc': state['applied_qdisc']}
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
