import copy
import io
import json
import subprocess
import unittest
from unittest.mock import patch

from automation_kit import capacity as c


class CapacityTests(unittest.TestCase):
    def setUp(self):
        self.observation = {"schema_version": 1, "sampled_at": 1000,
            "filesystem": {"available_bytes": 8*c.GIB, "total_bytes": 42*c.GIB, "read_only": False},
            "thin_pool": {"size_bytes": 364*c.GIB, "data_percent": 82.25,
                          "metadata_percent": 3.35, "healthy": True}}

    def check(self, observation=None, peak=None, now=1010):
        return c.evaluate(self.observation if observation is None else observation,
                          peak or 3*c.GIB//2, now=now)

    def test_good_evidence_admits_bounded_slice(self):
        r = self.check()
        self.assertEqual(r['status'], 'ready')
        self.assertEqual(r['required_available_bytes'], 6*c.GIB)
        self.assertEqual(r['source_as_of'], c.utc(1000))

    def test_growth_budget_includes_overlap_and_concurrent_writers(self):
        r = self.check(peak=4*c.GIB)
        self.assertEqual(r['required_available_bytes'], 11*c.GIB)
        self.assertIn('FILESYSTEM_HEADROOM_LOW', r['reasons'])

    def test_exact_available_floor_and_pool_boundaries(self):
        self.observation['filesystem']['available_bytes'] = 6*c.GIB
        self.assertEqual(self.check()['status'], 'ready')
        self.observation['filesystem']['available_bytes'] -= 1
        self.assertIn('FILESYSTEM_HEADROOM_LOW', self.check()['reasons'])
        self.observation['thin_pool']['data_percent'] = 89
        self.assertIn('THIN_POOL_HEADROOM_LOW', self.check()['reasons'])
        self.observation['thin_pool']['metadata_percent'] = 70
        self.assertIn('THIN_POOL_METADATA_HIGH', self.check()['reasons'])

    def test_stale_future_and_unknown_never_admit(self):
        for now in (999, 1061):
            with self.subTest(now=now):
                self.assertEqual(self.check(now=now)['completeness'], 'unknown')
        for data in (None, {}, [], {'schema_version': True}):
            with self.subTest(data=data):
                self.assertEqual(c.evaluate(data, c.GIB, now=1010)['status'], 'blocked')

    def test_malformed_numbers_and_readonly_or_unhealthy_fail_closed(self):
        for group, key, values in [
            ('filesystem', 'available_bytes', [True, -1, float('nan'), 1.5, 99*c.GIB]),
            ('filesystem', 'read_only', [True, 0, 'false']),
            ('thin_pool', 'data_percent', [True, -1, 101, float('inf'), '20']),
            ('thin_pool', 'healthy', [False, 1, 'true'])]:
            for value in values:
                obs = copy.deepcopy(self.observation); obs[group][key] = value
                with self.subTest(group=group, key=key, value=value):
                    self.assertEqual(self.check(obs)['completeness'], 'unknown')

    def test_private_input_and_diagnostics_cannot_reach_output(self):
        self.observation['customer_email'] = 'private@example.invalid'
        self.observation['filesystem']['raw_error'] = 'secret diagnostic'
        self.assertNotIn('private', json.dumps(self.check()))
        self.assertNotIn('secret', json.dumps(self.check()))
        with patch.object(c, 'collect', side_effect=OSError('secret credential')), patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(c.main(['--thin-pool', 'vg/pool', '--peak-bytes', '1']), 2)
            self.assertNotIn('secret', output.getvalue())

    def test_live_collector_uses_service_user_and_no_shell(self):
        pool = {'report': [{'lv': [{'lv_size': str(364*c.GIB), 'data_percent': '82',
            'metadata_percent': '3', 'lv_health_status': '', 'segtype': 'thin-pool'}]}]}
        with patch.object(c, 'command', side_effect=[json.dumps(self.observation['filesystem']), json.dumps(pool)]) as run:
            r = c.collect('vg/pool', container=123, user='worker')
            self.assertEqual(r['filesystem']['available_bytes'], 8*c.GIB)
            self.assertEqual(run.call_args_list[0].args[0][:8], ['pct','exec','123','--','runuser','-u','worker','--'])
        for pool_name in ('vg/pool;echo bad', '../../foo', 'vg/pool extra'):
            with self.assertRaises(ValueError): c.collect(pool_name)

    def test_collector_rejects_nonpool_and_missing_metadata(self):
        for row in ({'segtype': 'linear'}, {'segtype':'thin-pool'}):
            with patch.object(c, 'filesystem', return_value=self.observation['filesystem']), patch.object(c, 'command', return_value=json.dumps({'report':[{'lv':[row]}]})):
                with self.assertRaises((ValueError, KeyError)): c.collect('vg/pool')

    def test_timeout_does_not_emit_raw_output(self):
        with patch.object(c, 'collect', side_effect=subprocess.TimeoutExpired('sensitive', 15)), patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(c.main(['--thin-pool','vg/pool','--peak-bytes','10']), 2)
            self.assertEqual(json.loads(output.getvalue())['reasons'], ['CAPACITY_EVIDENCE_UNAVAILABLE'])

    def test_unusable_workload_budget_and_broken_lvm_structure_block(self):
        for peak in ('0', '-1', str(2**65)):
            with patch.object(c, 'collect', return_value=self.observation), patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(c.main(['--thin-pool','vg/pool','--peak-bytes',peak]), 2)
                self.assertEqual(json.loads(output.getvalue())['status'], 'blocked')
        with patch.object(c, 'collect', side_effect=IndexError()), patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(c.main(['--thin-pool','vg/pool','--peak-bytes','1']), 2)
        with self.assertRaises(ValueError): c.collect('vg/pool', user='worker')

    def test_deep_json_returns_fixed_blocked_report(self):
        with patch('sys.stdin', io.StringIO('['*2000+'0'+']'*2000)), patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(c.main(['--observation','-','--peak-bytes','1']), 2)
            self.assertEqual(json.loads(output.getvalue())['reasons'], ['CAPACITY_EVIDENCE_UNAVAILABLE'])

    def test_null_lvm_health_returns_fixed_blocked_report(self):
        row = {'lv_size': str(364*c.GIB), 'data_percent': '82', 'metadata_percent':'3',
               'lv_health_status': None, 'segtype':'thin-pool'}
        with patch.object(c, 'filesystem', return_value=self.observation['filesystem']), patch.object(c, 'command', return_value=json.dumps({'report':[{'lv':[row]}]})), patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(c.main(['--thin-pool','vg/pool','--peak-bytes','1']), 2)
            self.assertEqual(json.loads(output.getvalue())['reasons'], ['CAPACITY_EVIDENCE_UNAVAILABLE'])

    def test_invalid_cli_arguments_do_not_echo_private_values(self):
        for args in (['--peak-bytes','secret-value','--thin-pool','vg/pool'],
                     ['--peak-bytes','1','--observation','-','--secret-private-value'],
                     ['--peak-bytes','1']):
            with patch('sys.stdout', new_callable=io.StringIO) as output, patch('sys.stderr', new_callable=io.StringIO) as errors:
                self.assertEqual(c.main(args), 2)
                self.assertEqual(json.loads(output.getvalue())['reasons'], ['CAPACITY_EVIDENCE_UNAVAILABLE'])
                self.assertNotIn('secret', output.getvalue()+errors.getvalue())
                self.assertEqual(errors.getvalue(), '')

    def test_help_remains_useful_and_successful(self):
        with patch('sys.stdout', new_callable=io.StringIO) as output, self.assertRaises(SystemExit) as done:
            c.main(['--help'])
        self.assertEqual(done.exception.code, 0)
        self.assertIn('--peak-bytes', output.getvalue())


if __name__ == '__main__':
    unittest.main()
