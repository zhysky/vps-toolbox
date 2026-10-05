#!/usr/bin/env python3
"""Bounded VPS diagnostics and source-restricted two-end network tests.

Python standard library only. Reports remain on the machine running the command.
"""
import argparse
import base64
import collections
import datetime
import hashlib
import http.client
import http.server
import ipaddress
import json
import math
import mmap
import os
from pathlib import Path
import platform
import random
import re
import select
import shutil
import signal
import socket
import ssl
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid

VERSION = '0.1.0'
MIB = 1024 * 1024
MAGIC = b'VPSQC001'
PAYLOAD = bytes(range(256)) * 256
PUBLIC_TARGETS = [
    ('example', 'https://example.com/', [200]),
    ('cloudflare', 'https://www.cloudflare.com/cdn-cgi/trace', [200]),
    ('google', 'https://www.google.com/generate_204', [204]),
    ('youtube', 'https://www.youtube.com/generate_204', [204]),
    ('github', 'https://github.com/', [200]),
    ('openai_api', 'https://api.openai.com/v1/models', [401]),
]


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def stats(values):
    ordered = sorted(values)
    if not ordered:
        return None
    return {'count': len(ordered), 'min': ordered[0], 'mean': statistics.mean(ordered),
            'median': statistics.median(ordered), 'p95': ordered[max(0, math.ceil(len(ordered) * .95) - 1)],
            'max': ordered[-1], 'stddev': statistics.pstdev(ordered)}


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.new')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def command(argv, timeout=30, input_data=None):
    start = time.monotonic()
    env = os.environ.copy()
    env['LC_ALL'] = 'C'
    options = {'start_new_session': True} if os.name != 'nt' else {'creationflags': subprocess.CREATE_NO_WINDOW}
    try:
        child = subprocess.Popen([str(v) for v in argv], stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, **options)
    except FileNotFoundError:
        return {'status': 'skip', 'reason': 'tool_missing', 'command': argv, 'returncode': None}
    timed_out = False
    try:
        out, err = child.communicate(input=input_data, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        if os.name == 'nt':
            child.kill()
        else:
            os.killpg(child.pid, signal.SIGKILL)
        out, err = child.communicate()
    return {'status': 'error' if timed_out else 'pass' if child.returncode == 0 else 'fail',
            'command': [str(v) for v in argv], 'returncode': child.returncode, 'timed_out': timed_out,
            'elapsed_seconds': time.monotonic() - start,
            'stdout': out.decode('utf-8', 'replace')[-2 * MIB:],
            'stderr': err.decode('utf-8', 'replace')[-65536:]}


def json_output(result):
    try:
        return json.loads(result.get('stdout', ''))
    except (ValueError, TypeError):
        return None


class Report:
    def __init__(self, output, mode, options):
        base = Path(output).expanduser().resolve()
        base.mkdir(parents=True, exist_ok=True)
        self.path = base / (datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:6])
        self.path.mkdir(mode=0o700)
        self.data = {'schema_version': 1, 'tool_version': VERSION, 'mode': mode, 'started_utc': utc(),
                     'options': options, 'checks': [], 'complete': False, 'report_directory': str(self.path)}
        self.save()

    def save(self):
        self.data['status_counts'] = dict(collections.Counter(x['status'] for x in self.data['checks']))
        atomic_json(self.path / 'report.json', self.data)

    def add(self, name, result):
        row = dict(result, name=name, recorded_utc=utc())
        if 'status' not in row:
            row['status'] = 'info'
        self.data['checks'].append(row)
        self.save()
        metrics = row.get('metrics', {})
        progress = {'check': name, 'status': row['status']}
        if metrics:
            progress['metrics'] = metrics
        if row.get('reason'):
            progress['reason'] = row['reason']
        print(json.dumps(progress, ensure_ascii=False), flush=True)
        return row

    def run(self, name, function, *args, **kwargs):
        try:
            result = function(*args, **kwargs)
        except Exception as exc:
            result = {'status': 'error', 'error_type': type(exc).__name__, 'reason': str(exc)}
        return self.add(name, result)

    def finish(self, complete=True):
        self.data['complete'] = complete
        self.data['finished_utc'] = utc()
        self.save()
        lines = ['VPS 质量检测', '版本：' + VERSION, '模式：' + self.data['mode'],
                 '开始时间（UTC）：' + self.data['started_utc'], '完成时间（UTC）：' + self.data['finished_utc'],
                 '执行完成：' + str(complete), '状态统计：' + json.dumps(self.data['status_counts'], ensure_ascii=False), '']
        for row in self.data['checks']:
            lines.append('[%s] %s' % (row['status'], row['name']))
            if row.get('metrics'):
                lines.append('  ' + json.dumps(row['metrics'], ensure_ascii=False))
            if row.get('reason'):
                lines.append('  ' + row['reason'])
        lines.extend(['', '说明：pass 仅表示该项完成并满足明确的校验条件；info 是采集信息，skip 是没有执行或不适用。',
                      'fail 是检测失败或条件未满足；partial 表示部分成功；error 表示超时或工具执行错误。',
                      'HTTP 401 可证明未登录的 API 已响应，不能证明账号可用、付费权限或地区解锁。',
                      '中间路由节点不回 ICMP 不能单独证明端到端丢包；TCP 重传次数不是 UDP 丢包率。',
                      '短测不是长期稳定性或承诺带宽证明；磁盘 O_DIRECT 仍可能受到宿主机缓存影响。',
                      '完整参数、原始输出与失败信息见 report.json；报告不会自动上传。'])
        (self.path / 'report.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        print(json.dumps({'finished': complete, 'report': str(self.path / 'report.json'),
                          'summary': str(self.path / 'report.txt')}, ensure_ascii=False), flush=True)


def read_text(path):
    try:
        return Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return None


def inventory():
    values = {'platform': platform.platform(), 'cpu_count': os.cpu_count(), 'os_release': read_text('/etc/os-release'),
              'memory': read_text('/proc/meminfo'), 'uptime': read_text('/proc/uptime'), 'load': read_text('/proc/loadavg')}
    for label, argv in [
        ('cpu', ['lscpu', '-J']), ('disk', ['lsblk', '-J', '-o', 'NAME,SIZE,ROTA,TYPE,MOUNTPOINTS,MODEL']),
        ('filesystem', ['df', '-B1', '-T']), ('addresses', ['ip', '-j', 'address']),
        ('routes_v4', ['ip', '-j', '-4', 'route']), ('routes_v6', ['ip', '-j', '-6', 'route']),
        ('link_counters', ['ip', '-j', '-s', 'link']), ('qdisc_actual', ['tc', '-s', 'qdisc', 'show']),
        ('tcp_settings', ['sysctl', 'net.ipv4.tcp_congestion_control', 'net.core.default_qdisc', 'net.core.rmem_max',
                          'net.core.wmem_max', 'net.ipv4.tcp_rmem', 'net.ipv4.tcp_wmem']),
    ]:
        values[label] = command(argv, 15)
    return {'status': 'info', 'data': values}


def cpu_counters():
    try:
        return [int(v) for v in Path('/proc/stat').read_text().splitlines()[0].split()[1:]]
    except (OSError, ValueError, IndexError):
        return None


def sysbench(kind, threads, seconds):
    before = cpu_counters()
    args = ['sysbench', kind, '--threads=' + str(threads), '--time=' + str(seconds)]
    if kind == 'cpu':
        args += ['--cpu-max-prime=20000', '--events=0']
    else:
        args += ['--memory-block-size=1M', '--memory-total-size=1T', '--memory-oper=write']
    result = command(args + ['run'], seconds + 15)
    output = result.get('stdout', '')
    metrics = {'threads': threads, 'requested_seconds': seconds}
    expression = r'events per second:\s*([0-9.]+)' if kind == 'cpu' else r'\(([0-9.]+) MiB/sec\)'
    match = re.search(expression, output)
    if match:
        metrics['events_per_second' if kind == 'cpu' else 'MiB_per_second'] = float(match.group(1))
    elif result['status'] == 'pass':
        result.update(status='error', reason='sysbench returned no expected measurement')
    after = cpu_counters()
    if before and after and len(before) > 7 and len(after) > 7:
        delta = [b - a for a, b in zip(before[:8], after[:8])]
        metrics['cpu_steal_percent_during_test'] = 100 * delta[7] / sum(delta) if sum(delta) else None
    result['metrics'] = metrics
    return result


def disk_test(directory, size_mib, seconds):
    if not hasattr(os, 'O_DIRECT'):
        return {'status': 'skip', 'reason': 'O_DIRECT unavailable; no buffered result substituted'}
    size = size_mib * MIB
    if shutil.disk_usage(directory).free < size + 512 * MIB:
        return {'status': 'skip', 'reason': 'insufficient free space for bounded disk test'}
    path = Path(directory) / ('disk-' + uuid.uuid4().hex + '.tmp')
    fd, view, small, buffer = None, None, None, None
    metrics = {'file_MiB': size_mib, 'direct_io': True, 'queue_depth': 1, 'random_block_bytes': 4096}
    try:
        buffer = mmap.mmap(-1, MIB)
        buffer[:] = os.urandom(MIB)
        view = memoryview(buffer)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_DIRECT, 0o600)
        for action in ('write', 'read'):
            started = time.perf_counter()
            for offset in range(0, size, MIB):
                n = os.pwrite(fd, view, offset) if action == 'write' else os.preadv(fd, [view], offset)
                if n != MIB:
                    raise IOError('short direct I/O')
            if action == 'write':
                os.fsync(fd)
            elapsed = time.perf_counter() - started
            metrics['sequential_' + action] = {'MiB_s': size_mib / elapsed, 'seconds': elapsed}
        small = view[:4096]
        rng = random.Random(20261005)
        for action in ('read', 'write'):
            latencies, count = [], 0
            started = time.perf_counter()
            while time.perf_counter() - started < seconds:
                offset = rng.randrange(size // 4096) * 4096
                tick = time.perf_counter()
                n = os.preadv(fd, [small], offset) if action == 'read' else os.pwrite(fd, small, offset)
                if n != 4096:
                    raise IOError('short random I/O')
                latency = (time.perf_counter() - tick) * 1000
                count += 1
                if len(latencies) < 100000:
                    latencies.append(latency)
                else:
                    position = rng.randrange(count)
                    if position < len(latencies):
                        latencies[position] = latency
            if action == 'write':
                os.fsync(fd)
            elapsed = time.perf_counter() - started
            metrics['random_' + action] = {'IOPS': count / elapsed, 'MiB_s': count * 4096 / MIB / elapsed,
                                          'seconds': elapsed, 'operations': count, 'latency_ms_sample': stats(latencies)}
        return {'status': 'pass', 'metrics': metrics}
    except OSError as exc:
        return {'status': 'skip' if exc.errno in (22, 95) else 'error', 'reason': str(exc), 'metrics': metrics}
    finally:
        if fd is not None:
            os.close(fd)
            path.unlink()
        if small is not None:
            small.release()
        if view is not None:
            view.release()
        if buffer is not None:
            buffer.close()


def curl_measure(url, family, body_path=None, upload_path=None, byte_limit=2 * MIB, timeout=20):
    fmt = ('{"http_code":%{http_code},"size_download":%{size_download},"size_upload":%{size_upload},'
           '"speed_download_Bps":%{speed_download},"speed_upload_Bps":%{speed_upload},'
           '"time_total":%{time_total},"time_namelookup":%{time_namelookup},"time_connect":%{time_connect},'
           '"time_appconnect":%{time_appconnect},"time_starttransfer":%{time_starttransfer},'
           '"remote_ip":"%{remote_ip}","http_version":"%{http_version}","ssl_verify_result":%{ssl_verify_result}}')
    args = ['curl', '-' + str(family), '--silent', '--show-error', '--noproxy', '*', '--proto', '=https',
            '--connect-timeout', '6', '--max-time', str(timeout), '--max-filesize', str(byte_limit),
            '--output', str(body_path) if body_path else os.devnull, '--write-out', fmt]
    if upload_path:
        args += ['--request', 'POST', '--data-binary', '@' + str(upload_path)]
    result = command(args + [url], timeout + 5)
    result['url'], result['family'] = url, family
    result['transfer'] = json_output(result)
    return result


def validate_http(result, expected, expected_bytes=None, upload=False):
    data = result.get('transfer') or {}
    code = data.get('http_code')
    valid = result.get('returncode') == 0 and code in expected and data.get('ssl_verify_result') == 0
    actual_bytes = data.get('size_upload' if upload else 'size_download')
    if expected_bytes is not None and actual_bytes != expected_bytes:
        valid = False
    result['status'] = 'pass' if valid else 'skip' if result.get('reason') == 'tool_missing' else 'fail'
    result['expected_status_codes'] = expected
    result['metrics'] = {'http_status': code, 'elapsed_seconds': data.get('time_total'),
                         'tcp_connect_seconds': data.get('time_connect'), 'tls_ready_seconds': data.get('time_appconnect'),
                         'bytes': actual_bytes, 'Mbps': None}
    if expected_bytes is not None:
        result['expected_bytes'] = expected_bytes
        if valid:
            result['metrics']['Mbps'] = data.get('speed_upload_Bps' if upload else 'speed_download_Bps', 0) * 8 / 1e6
            result['metrics']['short_sample_under_3_seconds'] = data.get('time_total', 0) < 3
    if not valid:
        result['reason'] = 'transport, HTTP status, certificate, or byte-count check failed; not a zero-bandwidth result'
    return result


def ping_test(target, family, count):
    result = command(['ping', '-' + str(family), '-n', '-c', str(count), '-i', '0.2', '-W', '2', '-w', str(count // 5 + 8), target], count // 5 + 12)
    output = result.get('stdout', '')
    match = re.search(r'(\d+) packets transmitted,\s*(\d+) (?:packets )?received.*?([\d.]+)% packet loss', output)
    rtt = re.search(r'(?:rtt|round-trip) min/avg/max/(?:mdev|stddev) = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)', output)
    if match:
        sent, received = int(match.group(1)), int(match.group(2))
        result['metrics'] = {'sent': sent, 'received': received, 'loss_percent': float(match.group(3))}
        if rtt:
            result['metrics']['rtt_ms'] = dict(zip(['min', 'mean', 'max', 'stddev'], map(float, rtt.groups())))
        result['status'] = 'pass' if received == sent and sent else 'partial' if received else 'fail'
    return result


def dns_test(hostname):
    code = 'import json,socket,sys;print(json.dumps(sorted({v[4][0] for v in socket.getaddrinfo(sys.argv[1],443,type=socket.SOCK_STREAM)})))'
    result = command([sys.executable, '-c', code, hostname], 10)
    result['metrics'] = {'addresses': json_output(result), 'elapsed_seconds': result.get('elapsed_seconds')}
    return result


def ipv6_available():
    route = command(['ip', '-j', '-6', 'route', 'get', '2606:4700:4700::1111'], 5)
    rows = json_output(route)
    return bool(route.get('returncode') == 0 and rows and any(x.get('dev') != 'lo' for x in rows))


def server_run(args):
    if platform.system() != 'Linux':
        raise SystemExit('Server checks require Linux. Use peer-client for Windows/macOS clients.')
    if hasattr(os, 'nice'):
        os.nice(10)
    report = Report(args.output, 'server', vars(args))
    try:
        report.run('inventory_before', inventory)
        for name, argv in [('failed_systemd_units', ['systemctl', '--failed', '--no-legend', '--no-pager']),
                           ('kernel_errors_this_boot', ['journalctl', '-k', '-b', '-p', '0..3', '-n', '80', '--no-pager'])]:
            row = command(argv, 15)
            if row['status'] == 'pass':
                row['status'] = 'info'
            report.add(name, row)
        seconds = 3 if args.quick else 10
        report.run('cpu_single', sysbench, 'cpu', 1, seconds)
        report.run('cpu_multi', sysbench, 'cpu', min(4, os.cpu_count() or 1), seconds)
        report.run('memory_write', sysbench, 'memory', 1, 2 if args.quick else 5)
        report.run('disk_direct', disk_test, report.path, 64 if args.quick else args.disk_mib, 1 if args.quick else 5)
        for host in ('example.com', 'github.com', 'www.cloudflare.com'):
            report.run('dns_' + host, dns_test, host)
        families = [4]
        if ipv6_available():
            families.append(6)
        else:
            report.add('ipv6_network_checks', {'status': 'skip', 'reason': 'no usable IPv6 route; IPv6 network tests not executed'})
        for family in families:
            targets = ['1.1.1.1', '8.8.8.8', '223.5.5.5'] if family == 4 else ['2606:4700:4700::1111', '2001:4860:4860::8888']
            for target in targets:
                report.run('icmp_v%d_%s' % (family, target), ping_test, target, family, 5 if args.quick else 30)
            for target in ['www.cloudflare.com', 'github.com'] + args.route_target:
                report.run('route_v%d_%s' % (family, target), command,
                           ['mtr', '-' + str(family), '-n', '-j', '-r', '-c', '3' if args.quick else '5', '-i', '.2', '-m', '20', '-T', '-P', '443', target], 30)
            for name, url, expected in PUBLIC_TARGETS:
                report.run('https_v%d_%s' % (family, name), lambda u=url, e=expected, f=family: validate_http(curl_measure(u, f), e))
            trace = report.path / ('trace-v%d.txt' % family)
            result = validate_http(curl_measure('https://www.cloudflare.com/cdn-cgi/trace', family, body_path=trace), [200])
            if result['status'] == 'pass':
                parsed = dict(line.split('=', 1) for line in trace.read_text().splitlines() if '=' in line)
                result['metrics'].update({key: parsed.get(key) for key in ('ip', 'loc', 'colo')})
                address = parsed.get('ip')
                try:
                    valid_address = ipaddress.ip_address(address).is_global
                except ValueError:
                    valid_address = False
                if valid_address:
                    asn_path = report.path / ('asn-v%d.json' % family)
                    asn = validate_http(curl_measure('https://stat.ripe.net/data/prefix-overview/data.json?resource=' + address, family, body_path=asn_path), [200])
                    if asn['status'] == 'pass':
                        asn['data'] = json.loads(asn_path.read_text()).get('data')
                    report.add('asn_v%d' % family, asn)
            report.add('public_egress_v%d' % family, result)
            download_size = (4 if args.quick else args.download_mib) * MIB
            upload_size = (1 if args.quick else args.upload_mib) * MIB
            report.run('external_download_v%d' % family, lambda f=family, n=download_size: validate_http(
                curl_measure('https://speed.cloudflare.com/__down?bytes=' + str(n), f, byte_limit=n, timeout=45), [200], n))
            upload = report.path / ('upload-v%d.tmp' % family)
            try:
                with upload.open('xb') as stream:
                    stream.truncate(upload_size)
                report.run('external_upload_v%d' % family, lambda f=family, n=upload_size: validate_http(
                    curl_measure('https://speed.cloudflare.com/__up', f, upload_path=upload, timeout=45), [200], n, upload=True))
            finally:
                if upload.exists():
                    upload.unlink()
        report.run('inventory_after', inventory)
        report.finish()
    except BaseException:
        report.finish(False)
        raise
    return 0


def normalize_ip(value):
    address = ipaddress.ip_address(value.split('%')[0])
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return str(address)


class PeerHTTP(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(25)

    def body(self, body, status=200, kind='application/json'):
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            if self.path == '/health':
                self.body(json.dumps({'service': 'vps-quality-peer', 'version': VERSION,
                                      'observed_client': normalize_ip(self.client_address[0]),
                                      'echo_token': self.server.echo_token,
                                      'echo_port': self.server.echo_port}).encode())
            elif self.path.startswith('/bytes/'):
                remaining = int(self.path.split('/')[-1])
                if not 0 < remaining <= 64 * MIB:
                    return self.body(b'bad size', 400)
                self.send_response(200)
                self.send_header('Content-Length', str(remaining))
                self.end_headers()
                while remaining:
                    block = PAYLOAD[:min(remaining, len(PAYLOAD))]
                    self.wfile.write(block)
                    remaining -= len(block)
            elif self.path == '/stream':
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('Connection', 'close')
                self.end_headers()
                for number in range(12):
                    self.wfile.write(('data: %d\n\n' % number).encode())
                    self.wfile.flush()
                    time.sleep(.5)
                self.close_connection = True
            elif self.path == '/ws' and self.headers.get('Upgrade', '').lower() == 'websocket':
                key = self.headers.get('Sec-WebSocket-Key', '')
                if len(key) > 100:
                    return self.body(b'bad key', 400)
                accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
                self.send_response(101)
                self.send_header('Upgrade', 'websocket')
                self.send_header('Connection', 'Upgrade')
                self.send_header('Sec-WebSocket-Accept', accept)
                self.end_headers()
                for _ in range(32):
                    header = self.rfile.read(2)
                    if len(header) != 2:
                        break
                    opcode, length = header[0] & 15, header[1] & 127
                    if not header[1] & 128 or length >= 126:
                        break
                    mask = self.rfile.read(4)
                    payload = self.rfile.read(length)
                    if len(mask) != 4 or len(payload) != length:
                        break
                    payload = bytes(v ^ mask[i % 4] for i, v in enumerate(payload))
                    if opcode == 8:
                        self.wfile.write(b'\x88\x00')
                        break
                    self.wfile.write(bytes([128 | (10 if opcode == 9 else opcode), length]) + payload)
                    self.wfile.flush()
                self.close_connection = True
            else:
                self.body(b'not found', 404)
        except (ValueError, OSError):
            self.close_connection = True

    def do_POST(self):
        try:
            remaining = int(self.headers.get('Content-Length', '-1'))
            if self.path != '/upload' or not 0 <= remaining <= 64 * MIB:
                return self.body(b'bad request', 400)
            expected = remaining
            digest = hashlib.sha256()
            started = time.monotonic()
            while remaining:
                block = self.rfile.read(min(remaining, 65536))
                if not block:
                    break
                digest.update(block)
                remaining -= len(block)
            self.body(json.dumps({'bytes': expected - remaining, 'sha256': digest.hexdigest(),
                                  'seconds': time.monotonic() - started}).encode())
        except (ValueError, OSError):
            self.close_connection = True


class RestrictedHTTP(http.server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def verify_request(self, request, client_address):
        return normalize_ip(client_address[0]) in self.allowed


def peer_server(args):
    if platform.system() != 'Linux':
        raise SystemExit('The temporary peer server requires Linux.')
    bind = ipaddress.ip_address(args.bind)
    allowed = {normalize_ip(v) for v in args.allow_client}
    if not allowed:
        raise SystemExit('--allow-client is required; anonymous public test servers are not supported')
    if any(ipaddress.ip_address(v).version != bind.version for v in allowed):
        raise SystemExit('Bind and client address families must match')
    if args.no_firewall and not bind.is_loopback:
        raise SystemExit('--no-firewall is allowed only for a loopback test server')
    if not args.no_firewall and (os.geteuid() != 0 or not shutil.which('nft')):
        raise SystemExit('Public peer server requires root and nft for source restrictions')
    if not shutil.which(args.iperf):
        raise SystemExit('iperf3 is not installed')
    if len({args.port, args.http_port, args.echo_port}) != 3:
        raise SystemExit('Choose three different port numbers')
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    state_path = output / 'peer-state.json'
    if state_path.exists():
        raise SystemExit('Use a new output directory for each peer session')
    family = socket.AF_INET if bind.version == 4 else socket.AF_INET6
    # Check every listener before installing any firewall rules.
    for port, kind in [(args.port, socket.SOCK_STREAM), (args.port, socket.SOCK_DGRAM),
                       (args.http_port, socket.SOCK_STREAM), (args.echo_port, socket.SOCK_DGRAM)]:
        with socket.socket(family, kind) as probe:
            probe.bind((str(bind), port))
    token = os.urandom(16)
    table = 'vpsq_' + uuid.uuid4().hex[:10]
    stop = threading.Event()
    process, httpd, udp, log = None, None, None, None
    firewall_created = False
    state = {'pid': os.getpid(), 'bind': str(bind), 'allowed_clients': sorted(allowed),
             'iperf_port': args.port, 'http_port': args.http_port, 'echo_port': args.echo_port,
             'started_utc': utc(), 'ttl_seconds': args.ttl, 'firewall_table': None, 'ready': False}

    def stopped(signum, frame):
        stop.set()
    signal.signal(signal.SIGINT, stopped)
    signal.signal(signal.SIGTERM, stopped)
    try:
        if not args.no_firewall:
            keyword = 'ip' if bind.version == 4 else 'ip6'
            sources = '{ ' + ', '.join(sorted(allowed)) + ' }'
            rules = ('table inet %s {\n chain guard {\n type filter hook input priority -20; policy accept;\n'
                     ' tcp dport { %d, %d } %s saddr %s accept\n'
                     ' tcp dport { %d, %d } drop\n'
                     ' udp dport { %d, %d } %s saddr %s accept\n'
                     ' udp dport { %d, %d } drop\n }\n}\n') % (
                         table, args.port, args.http_port, keyword, sources, args.port, args.http_port,
                         args.port, args.echo_port, keyword, sources, args.port, args.echo_port)
            check = command(['nft', '--check', '-f', '-'], 10, rules.encode())
            if check['status'] != 'pass':
                raise RuntimeError('nft validation failed: ' + check.get('stderr', ''))
            applied = command(['nft', '-f', '-'], 10, rules.encode())
            if applied['status'] != 'pass':
                raise RuntimeError('nft source restriction failed: ' + applied.get('stderr', ''))
            firewall_created = True
            state['firewall_table'] = table
        server_type = type('PeerHTTPFamily', (RestrictedHTTP,), {'address_family': family})
        httpd = server_type((str(bind), args.http_port), PeerHTTP)
        httpd.allowed = allowed | ({'127.0.0.1'} if bind.version == 4 else {'::1'})
        httpd.echo_token, httpd.echo_port = token.hex(), args.echo_port
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        udp = socket.socket(family, socket.SOCK_DGRAM)
        udp.bind((str(bind), args.echo_port))
        udp.settimeout(.25)

        def echo():
            while not stop.is_set():
                try:
                    data, address = udp.recvfrom(1500)
                    if normalize_ip(address[0]) in allowed and 32 <= len(data) <= 1400 and data[:24] == MAGIC + token:
                        udp.sendto(data, address)
                except socket.timeout:
                    pass
                except OSError:
                    break
        threading.Thread(target=echo, daemon=True).start()
        log = (output / 'iperf-server.jsonl').open('xb')
        process = subprocess.Popen([args.iperf, '-s', '-' + str(bind.version), '-B', str(bind), '-p', str(args.port),
                                    '-J', '--forceflush', '--rcv-timeout', '120000'], stdout=log, stderr=subprocess.STDOUT)

        def telemetry():
            with (output / 'tcp-telemetry.jsonl').open('x', encoding='utf-8') as stream:
                while not stop.is_set():
                    sample = command(['ss', '-tinmp', 'sport', '=', ':' + str(args.port)], 5)
                    stream.write(json.dumps({'recorded_utc': utc(), 'sockets': sample.get('stdout', ''),
                                             'memory': read_text('/proc/meminfo'),
                                             'memory_pressure': read_text('/proc/pressure/memory')}) + '\n')
                    stream.flush()
                    stop.wait(2)
        threading.Thread(target=telemetry, daemon=True).start()
        time.sleep(.4)
        if process.poll() is not None:
            raise RuntimeError('iperf server exited; inspect its log')
        state.update(ready=True, iperf_pid=process.pid)
        atomic_json(state_path, state)
        print(json.dumps(state), flush=True)
        stop.wait(args.ttl)
    finally:
        stop.set()
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if httpd:
            httpd.shutdown()
            httpd.server_close()
        if udp:
            udp.close()
        if log:
            log.close()
        cleanup = command(['nft', 'delete', 'table', 'inet', table], 10) if firewall_created else {'status': 'pass'}
        state.update(ready=False, finished_utc=utc(), cleanup={'firewall_removed': cleanup['status'] == 'pass',
                                                           'iperf_stopped': not process or process.poll() is not None})
        atomic_json(state_path, state)
        print(json.dumps({'cleanup': state['cleanup']}), flush=True)
    return 0


def client_socket(host, port, args, datagram=False):
    address = ipaddress.ip_address(host)
    family = socket.AF_INET if address.version == 4 else socket.AF_INET6
    stream = socket.socket(family, socket.SOCK_DGRAM if datagram else socket.SOCK_STREAM)
    try:
        stream.settimeout(8)
        if args.interface_index is not None:
            if os.name != 'nt':
                raise ValueError('--interface-index is a Windows option; use --interface on Linux')
            level = socket.IPPROTO_IP if family == socket.AF_INET else socket.IPPROTO_IPV6
            index = socket.htonl(args.interface_index) if family == socket.AF_INET else args.interface_index
            stream.setsockopt(level, 31, index)
        if args.interface:
            if not hasattr(socket, 'SO_BINDTODEVICE'):
                raise ValueError('SO_BINDTODEVICE unavailable')
            stream.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, args.interface.encode() + b'\0')
        if args.bind:
            stream.bind((args.bind, 0))
        stream.connect((str(address), port))
        return stream
    except BaseException:
        stream.close()
        raise


def http_open(args, path, method='GET', payload=None, connector=None):
    stream = connector() if connector else client_socket(args.host, args.http_port, args)
    host = '[' + args.host + ']' if ':' in args.host else args.host
    headers = '%s %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n' % (method, path, host)
    if payload is not None:
        headers += 'Content-Length: %d\r\n' % len(payload)
    stream.sendall((headers + '\r\n').encode())
    if payload is not None:
        stream.sendall(payload)
    response = http.client.HTTPResponse(stream)
    response.begin()
    return stream, response


class Echo:
    def __init__(self, args, token, seconds=None):
        self.args, self.token, self.seconds = args, bytes.fromhex(token), seconds
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.sent, self.received, self.errors = {}, {}, []

    def run(self):
        stream = None
        try:
            stream = client_socket(self.args.host, self.args.echo_port, self.args, True)
            stream.setblocking(False)
            started, next_send, drain, number = time.monotonic(), 0, None, 0
            while True:
                now = time.monotonic()
                if self.seconds is not None and now - started >= self.seconds:
                    self.stop.set()
                if self.stop.is_set():
                    if drain is None:
                        drain = now + 1
                    if now >= drain:
                        break
                elif now >= next_send:
                    message = MAGIC + self.token + struct.pack('!I', number) + bytes(1172)
                    stream.send(message)
                    self.sent[number] = time.perf_counter()
                    number += 1
                    next_send = now + .5
                readable, _, _ = select.select([stream], [], [], .05)
                if readable:
                    data = stream.recv(1500)
                    if len(data) == 1200 and data[:24] == MAGIC + self.token:
                        number_received = struct.unpack('!I', data[24:28])[0]
                        if number_received in self.sent:
                            self.received[number_received] = (time.perf_counter() - self.sent[number_received]) * 1000
        except (OSError, ValueError) as exc:
            self.errors.append(str(exc))
        finally:
            if stream:
                stream.close()

    def start(self):
        self.thread.start()

    def finish(self):
        self.stop.set()
        self.thread.join(timeout=10)
        sent, received = len(self.sent), len(self.received)
        return {'status': 'pass' if sent and sent == received else 'partial' if received else 'fail',
                'metrics': {'sent': sent, 'received': received, 'loss_percent': 100 * (sent - received) / sent if sent else None,
                            'rtt_ms': stats(list(self.received.values())), 'packet_bytes': 1200, 'packets_per_second': 2},
                'errors': self.errors}


def summarize_iperf(data, reverse=False, udp=False):
    server = data.get('server_output_json', {})
    if isinstance(server, str):
        try:
            server = json.loads(server)
        except ValueError:
            server = {}
    if not isinstance(server, dict):
        server = {}
    client_end, server_end = data.get('end', {}), server.get('end', {})
    preferred = [client_end, server_end] if reverse else [server_end, client_end]
    receiver = None
    for end in preferred:
        if isinstance(end.get('sum_received'), dict) and 'bits_per_second' in end['sum_received']:
            receiver = end['sum_received']
            break
        if udp and isinstance(end.get('sum'), dict) and end['sum'].get('sender') is False:
            receiver = end['sum']
            break
    sender = None
    for end in ([server_end, client_end] if reverse else [client_end, server_end]):
        if isinstance(end.get('sum_sent'), dict):
            sender = end['sum_sent']
            if sender.get('retransmits') is not None:
                break
    result = {'received_Mbps': receiver['bits_per_second'] / 1e6 if receiver else None,
              'receiver_bytes': receiver.get('bytes') if receiver else None,
              'receiver_seconds': receiver.get('seconds') if receiver else None,
              'retransmits': sender.get('retransmits') if sender and not udp else None,
              'udp_loss_percent': receiver.get('lost_percent') if receiver and udp else None,
              'udp_lost_packets': receiver.get('lost_packets') if receiver and udp else None,
              'udp_jitter_ms': receiver.get('jitter_ms') if receiver and udp else None}
    peers = server.get('start', {}).get('connected', [])
    result['server_observed_sources'] = sorted({x['remote_host'] for x in peers if x.get('remote_host')})
    return result


def peer_plan(profile):
    jobs = []
    def add(name, flags, reverse=False, udp=False):
        jobs.append({'name': name, 'flags': flags, 'reverse': reverse, 'udp': udp})
    for parallel in ([1] if profile == 'quick' else [1, 2, 4, 8]):
        for reverse in (False, True):
            add('tcp_p%d_%s' % (parallel, 'download' if reverse else 'upload'), ['-P', str(parallel)], reverse)
    if profile == 'full':
        for window in ('128K', '512K', '2M', '8M'):
            for reverse in (False, True):
                add('tcp_window_%s_%s' % (window, 'download' if reverse else 'upload'), ['-w', window], reverse)
        for block in ('8K', '1M'):
            for reverse in (False, True):
                add('tcp_block_%s_%s' % (block, 'download' if reverse else 'upload'), ['-l', block], reverse)
        add('tcp_nodelay_download', ['-N', '-l', '8K'], True)
        add('tcp_mss1200_download', ['-M', '1200'], True)
        add('tcp_bidirectional_p4', ['--bidir', '-P', '4'])
    for rate in ([1] if profile == 'quick' else [1, 10, 50, 100]):
        for reverse in (False, True):
            add('udp_%dM_1200B_%s' % (rate, 'download' if reverse else 'upload'),
                ['-u', '-b', str(rate) + 'M', '-l', '1200', '--udp-counters-64bit'], reverse, True)
    if profile == 'full':
        for size in (200, 400, 600, 800, 1000, 1300):
            add('udp_10M_%dB_upload' % size, ['-u', '-b', '10M', '-l', str(size), '--udp-counters-64bit'], False, True)
        for rate in (2, 5):
            add('udp_%dM_1200B_upload' % rate, ['-u', '-b', str(rate) + 'M', '-l', '1200', '--udp-counters-64bit'], False, True)
    return jobs


def exact(stream, size):
    chunks = bytearray()
    while len(chunks) < size:
        part = stream.recv(size - len(chunks))
        if not part:
            raise EOFError('connection ended early')
        chunks.extend(part)
    return bytes(chunks)


def controlled_test(args, kind, connector=None):
    started = time.monotonic()
    stream = None
    try:
        if kind == 'download':
            count = 4 * MIB
            stream, response = http_open(args, '/bytes/' + str(count), connector=connector)
            data = response.read(count + 1)
            expected = (PAYLOAD * (count // len(PAYLOAD)))
            passed = response.status == 200 and data == expected
            return {'status': 'pass' if passed else 'fail', 'metrics': {'bytes': len(data), 'sha256_ok': passed,
                    'Mbps_including_setup': len(data) * 8 / 1e6 / (time.monotonic() - started)}}
        if kind == 'upload':
            data = PAYLOAD * 16
            stream, response = http_open(args, '/upload', 'POST', data, connector)
            reply = json.loads(response.read(65536))
            passed = response.status == 200 and reply.get('bytes') == len(data) and reply.get('sha256') == hashlib.sha256(data).hexdigest()
            return {'status': 'pass' if passed else 'fail', 'metrics': {'bytes': len(data), 'sha256_ok': passed,
                    'Mbps_including_setup': len(data) * 8 / 1e6 / (time.monotonic() - started)}}
        if kind == 'sse':
            stream, response = http_open(args, '/stream', connector=connector)
            events = [line.decode().strip() for line in response if line.startswith(b'data:')]
            passed = response.status == 200 and events == ['data: %d' % x for x in range(12)]
            return {'status': 'pass' if passed else 'fail', 'metrics': {'events': len(events), 'seconds': time.monotonic() - started}}
        if kind == 'websocket':
            stream = connector() if connector else client_socket(args.host, args.http_port, args)
            key = base64.b64encode(os.urandom(16)).decode()
            request = ('GET /ws HTTP/1.1\r\nHost: peer\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                       'Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: %s\r\n\r\n') % key
            stream.sendall(request.encode())
            header = bytearray()
            while not header.endswith(b'\r\n\r\n'):
                header.extend(exact(stream, 1))
                if len(header) > 8192:
                    raise ValueError('oversized websocket response')
            expected = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
            if not header.startswith(b'HTTP/1.1 101') or expected not in header:
                raise ValueError('websocket handshake mismatch')
            latencies = []
            for number in range(11):
                if number == 10:
                    time.sleep(10)
                data = ('echo-%d' % number).encode()
                mask = os.urandom(4)
                tick = time.perf_counter()
                stream.sendall(bytes([0x81, 0x80 | len(data)]) + mask + bytes(v ^ mask[i % 4] for i, v in enumerate(data)))
                returned = exact(stream, 2)
                if returned != bytes([0x81, len(data)]) or exact(stream, len(data)) != data:
                    raise ValueError('websocket payload mismatch')
                latencies.append((time.perf_counter() - tick) * 1000)
            return {'status': 'pass', 'metrics': {'echoes': 11, 'idle_seconds': 10, 'rtt_ms': stats(latencies)}}
        raise ValueError('unknown controlled test')
    finally:
        if stream:
            stream.close()


def peer_client(args):
    host = ipaddress.ip_address(args.host)
    if args.bind and ipaddress.ip_address(args.bind).version != host.version:
        raise SystemExit('Local bind address and peer IP family differ')
    report = Report(args.output, 'peer-client', vars(args))
    try:
        report.add('iperf_version', command([args.iperf, '--version'], 5))
        stream, response = http_open(args, '/health')
        try:
            health = json.loads(response.read(65536))
        finally:
            stream.close()
        if response.status != 200 or health.get('service') != 'vps-quality-peer':
            raise RuntimeError('The endpoint is not a compatible test peer')
        observed = normalize_ip(health['observed_client'])
        source_ok = not args.expect_source or observed == normalize_ip(args.expect_source)
        report.add('source_proof', {'status': 'pass' if source_ok else 'fail', 'metrics': {'server_observed_source': observed,
                   'expected_source': args.expect_source, 'local_bind': args.bind, 'interface_index': args.interface_index}})
        if not source_ok:
            raise RuntimeError('Traffic did not follow the expected source path; no throughput tests run')
        args.echo_port = int(health['echo_port'])
        echo = Echo(args, health['echo_token'], 2 if args.profile == 'quick' else 10)
        echo.start()
        echo.thread.join(timeout=15)
        report.add('udp_idle_latency', echo.finish())
        for kind in ('download', 'upload', 'sse', 'websocket'):
            report.run('controlled_' + kind, controlled_test, args, kind)
        for job in peer_plan(args.profile):
            argv = [args.iperf, '-c', str(host), '-' + str(host.version), '-p', str(args.port), '-t', str(args.seconds),
                    '-O', '0' if job['udp'] else '1', '-J', '--get-server-output', '--connect-timeout', '6000']
            if args.bind:
                argv += ['-B', args.bind]
            if args.interface:
                argv += ['--bind-dev', args.interface]
            if job['reverse']:
                argv.append('-R')
            argv += job['flags']
            echo = Echo(args, health['echo_token'])
            echo.start()
            result = command(argv, args.seconds + 25)
            result['echo_under_load'] = echo.finish()
            data = json_output(result)
            result['test_parameters'] = job
            if isinstance(data, dict):
                result['raw'] = data
                result.pop('stdout', None)
                result['metrics'] = summarize_iperf(data, job['reverse'], job['udp'])
                sources = result['metrics']['server_observed_sources']
                result['source_verified'] = bool(sources) and all(normalize_ip(v) == observed for v in sources)
                if data.get('error'):
                    result.update(status='fail', reason=data['error'])
                elif result['metrics']['received_Mbps'] is None:
                    result.update(status='error', reason='no authoritative receiver measurement')
                elif not result['source_verified']:
                    result.update(status='partial', reason='server-side source proof unavailable or mismatched')
            report.add(job['name'], result)
        report.finish()
    except BaseException:
        report.finish(False)
        raise
    return 0


def integer_range(low, high):
    def parse(value):
        number = int(value)
        if not low <= number <= high:
            raise argparse.ArgumentTypeError('must be in %d..%d' % (low, high))
        return number
    return parse


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='VPS 质量与双端连通性检测，结果仅保存在本机。')
    parser.add_argument('--version', action='version', version=VERSION)
    sub = parser.add_subparsers(dest='mode', required=True)
    root_output = '/var/log/vps-quality-check' if os.name != 'nt' and getattr(os, 'geteuid', lambda: 1)() == 0 else './vps-quality-results'
    server = sub.add_parser('run', help='检测当前 VPS：系统、CPU、磁盘、DNS、路由、HTTPS 和上下行')
    server.add_argument('--output', default=root_output)
    server.add_argument('--quick', action='store_true', help='缩短 CPU、磁盘和网络样本')
    server.add_argument('--disk-mib', type=integer_range(64, 2048), default=512)
    server.add_argument('--download-mib', type=integer_range(1, 256), default=32)
    server.add_argument('--upload-mib', type=integer_range(1, 64), default=8)
    server.add_argument('--route-target', action='append', default=[], help='增加指定目标的回程 MTR')
    peer = sub.add_parser('peer-server', help='启动有来源限制、自动过期的临时双端检测服务')
    peer.add_argument('--bind', default='0.0.0.0')
    peer.add_argument('--allow-client', action='append', required=True, help='允许的客户端公网 IP，必须是单个地址')
    peer.add_argument('--port', type=integer_range(1024, 65533), default=45201)
    peer.add_argument('--http-port', type=integer_range(1024, 65535), default=45203)
    peer.add_argument('--echo-port', type=integer_range(1024, 65535), default=45202)
    peer.add_argument('--ttl', type=integer_range(10, 7200), default=1800)
    peer.add_argument('--output', required=True, help='新的专用状态目录')
    peer.add_argument('--iperf', default='iperf3')
    peer.add_argument('--no-firewall', action='store_true', help='仅用于本地回环验证')
    client = sub.add_parser('peer-client', help='在另一台电脑运行 TCP/UDP、内容完整性、SSE/WebSocket 检测')
    client.add_argument('--host', required=True, help='服务端 IPv4/IPv6 地址，使用明确 IP 验证地址族')
    client.add_argument('--bind', help='本机物理网卡地址；使用 TUN 时建议指定')
    client.add_argument('--interface-index', type=integer_range(1, 65535), help='Windows 物理网卡接口序号')
    client.add_argument('--interface', help='Linux 物理网卡名')
    client.add_argument('--expect-source', help='服务端应该看到的客户端公网 IP，不符则停止')
    client.add_argument('--port', type=integer_range(1, 65535), default=45201)
    client.add_argument('--http-port', type=integer_range(1, 65535), default=45203)
    client.add_argument('--echo-port', type=integer_range(1, 65535), default=45202)
    client.add_argument('--profile', choices=['quick', 'standard', 'full'], default='standard')
    client.add_argument('--seconds', type=integer_range(1, 60), default=10)
    client.add_argument('--iperf', default='iperf3')
    client.add_argument('--output', default='./vps-quality-results')
    args = parser.parse_args()
    if args.mode == 'run':
        return server_run(args)
    if args.mode == 'peer-server':
        return peer_server(args)
    return peer_client(args)


if __name__ == '__main__':
    sys.exit(main())
