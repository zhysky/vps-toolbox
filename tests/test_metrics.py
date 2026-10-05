import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('vps_check', ROOT / 'vps_check.py')
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)
spec = importlib.util.spec_from_file_location('network_optimize', ROOT / 'network_optimize.py')
tuning = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tuning)


class EvidenceTests(unittest.TestCase):
    def test_http_rejection_does_not_become_zero_bandwidth(self):
        record = {'returncode': 0, 'transfer': {'http_code': 403, 'ssl_verify_result': 0,
                  'size_download': 100, 'speed_download_Bps': 1000000, 'time_total': .01}}
        result = checks.validate_http(record, [200], 1024)
        self.assertEqual(result['status'], 'fail')
        self.assertIsNone(result['metrics']['Mbps'])

    def test_partial_body_is_not_success(self):
        record = {'returncode': 0, 'transfer': {'http_code': 200, 'ssl_verify_result': 0,
                  'size_download': 2048, 'speed_download_Bps': 1000000}}
        self.assertEqual(checks.validate_http(record, [200], 4096)['status'], 'fail')

    def test_unauthenticated_api_response_is_separate_from_throughput(self):
        record = {'returncode': 0, 'transfer': {'http_code': 401, 'ssl_verify_result': 0,
                  'size_download': 200, 'time_total': .1}}
        result = checks.validate_http(record, [401])
        self.assertEqual(result['status'], 'pass')
        self.assertIsNone(result['metrics']['Mbps'])

    def test_udp_upload_uses_receiver_not_sender(self):
        data = {'end': {'sum': {'sender': True, 'bits_per_second': 10000000, 'lost_percent': 0}},
                'server_output_json': {'end': {'sum': {'sender': False, 'bits_per_second': 1000000,
                  'lost_percent': 90, 'lost_packets': 900, 'jitter_ms': 1.2}}}}
        result = checks.summarize_iperf(data, False, True)
        self.assertEqual(result['received_Mbps'], 1)
        self.assertEqual(result['udp_loss_percent'], 90)
        self.assertIsNone(result['retransmits'])

    def test_missing_receiver_is_unknown(self):
        result = checks.summarize_iperf({'end': {'sum_sent': {'bits_per_second': 1000000}}})
        self.assertIsNone(result['received_Mbps'])
        self.assertIsNone(result['retransmits'])

    def test_unsupported_buffer_is_distinct_from_path_failure(self):
        self.assertEqual(checks.iperf_error_status('socket buffer size not set correctly'), 'skip')
        self.assertEqual(checks.iperf_error_status('unable to connect to server: Connection timed out'), 'fail')

    def test_receiver_source_is_retained(self):
        data = {'end': {'sum_received': {'bits_per_second': 2000000, 'bytes': 500000, 'seconds': 2}},
                'server_output_json': {'start': {'connected': [{'remote_host': '192.0.2.1'}]}}}
        result = checks.summarize_iperf(data, True)
        self.assertEqual(result['server_observed_sources'], ['192.0.2.1'])
        self.assertEqual(result['received_Mbps'], 2)

    def test_custom_or_complex_queues_are_rejected(self):
        self.assertFalse(tuning.factory_queue([{'root': True, 'kind': 'mq'}]))
        self.assertFalse(tuning.factory_queue([{'root': True, 'kind': 'tbf'}, {'kind': 'fq', 'parent': '1:1'}]))
        self.assertFalse(tuning.factory_queue([{'root': True, 'kind': 'pfifo_fast', 'options': {'bands': 4}}]))
        self.assertFalse(tuning.factory_queue([{'root': True, 'kind': 'pfifo_fast'}, {'kind': 'clsact'}]))

    def test_simple_factory_queue_parameters_are_recognized(self):
        self.assertTrue(tuning.factory_queue([{'root': True, 'kind': 'pfifo_fast', 'handle': '0:',
                                             'options': {'bands': 3, 'priomap': tuning.DEFAULT_PRIOMAP}}]))

    def test_codel_restore_preserves_explicit_factory_options(self):
        queue = [{'root': True, 'kind': 'fq_codel', 'options': {'limit': 10240, 'flows': 1024, 'quantum': 1514,
                 'target': 4999, 'interval': 99999, 'memory_limit': 33554432, 'ecn': True, 'drop_batch': 64}}]
        self.assertTrue(tuning.factory_queue(queue))
        command = tuning.restore_queue_command('ens5', queue)
        self.assertIn('5ms', command)
        self.assertIn('33554432', command)
        queue[0]['options']['ce_threshold'] = 100
        self.assertFalse(tuning.factory_queue(queue))


if __name__ == '__main__':
    unittest.main()
