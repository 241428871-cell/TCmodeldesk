"""Offline contracts for policy snapshots and durable continuation ownership."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import model_registry as registry
import engineering_policy as policy
import continuation_clock as clock
import continuation_store as store
import stage_store


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.enterContext(patch.object(registry, 'HERE', self.root / 'desk'))

    def test_read_is_side_effect_free_and_global_is_opt_in(self):
        snapshot = policy.policy_snapshot(str(self.project))
        self.assertEqual(snapshot['effective_rules'], [])
        self.assertFalse(snapshot['global_enabled'])
        self.assertFalse((self.root / 'desk').exists())
        self.assertFalse((self.project / '.model-desk').exists())
        status = policy.goal_status(str(self.project), 'missing-goal', snapshot['snapshot_id'])
        self.assertEqual(status['status'], 'not_loaded')
        self.assertFalse((self.project / '.model-desk').exists())

    def test_global_selection_and_project_overrides_are_versioned(self):
        initial = policy.save_policy({'global_enabled': True, 'global_rule_ids': ['evidence', 'continuation']})
        inherited = policy.task_context(str(self.project))
        self.assertEqual([rule['id'] for rule in inherited['snapshot']['effective_rules']], ['evidence', 'continuation'])
        disabled = policy.save_policy({'scope': 'project', 'project': str(self.project),
                                       'mode': 'off', 'base_revision': 0})
        self.assertEqual(disabled['effective_rules'], [])
        with self.assertRaises(policy.PolicyError):
            policy.save_policy({'scope': 'project', 'project': str(self.project),
                                'mode': 'inherit', 'base_revision': 0})
        custom = policy.save_policy({'scope': 'project', 'project': str(self.project),
                                     'mode': 'custom', 'rule_ids': ['records'],
                                     'custom_rules': '仅在现有授权内维护项目交接。'})
        self.assertEqual([rule['id'] for rule in custom['effective_rules']], ['records', 'custom'])
        self.assertNotEqual(custom['snapshot_id'], inherited['snapshot_id'])
        self.assertEqual([rule['id'] for rule in inherited['snapshot']['effective_rules']], ['evidence', 'continuation'])
        self.assertNotIn('/Users/', json.dumps(custom['available_rules'], ensure_ascii=False))
        with self.assertRaises(policy.PolicyError):
            policy.save_policy({'base_revision': 0, 'global_enabled': False})

    def test_project_override_is_owned_by_project_and_not_global_config(self):
        custom = policy.save_policy({'scope': 'project', 'project': str(self.project),
                                     'mode': 'custom', 'rule_ids': ['records'],
                                     'custom_rules': '项目私有方法'})
        project_file = self.project / '.model-desk' / policy.CONFIG_NAME
        self.assertTrue(project_file.is_file())
        self.assertEqual(custom['project_storage'], 'project')
        global_path = self.root / 'desk' / policy.CONFIG_NAME
        self.assertFalse(global_path.exists())
        self.assertNotIn(str(self.project), project_file.read_text(encoding='utf-8'))
        other = self.root / 'other'
        other.mkdir()
        foreign = policy.policy_snapshot(str(other))
        self.assertEqual(foreign['configured_mode'], 'inherit')
        self.assertEqual(foreign['custom_rules'], '')
        self.assertFalse((other / '.model-desk').exists())

    def test_legacy_global_override_migrates_on_explicit_project_save(self):
        global_path = self.root / 'desk' / policy.CONFIG_NAME
        registry.write_json(global_path, {'schema': 1, 'revision': 3, 'global_enabled': False,
            'global_rule_ids': list(policy._IDS), 'global_custom_rules': '', 'projects': {
                str(self.project.resolve()): {'mode': 'custom', 'rule_ids': ['records'],
                                              'custom_rules': '旧项目方法'}}})
        legacy = policy.policy_snapshot(str(self.project))
        self.assertEqual(legacy['project_storage'], 'legacy_global')
        self.assertEqual(legacy['custom_rules'], '旧项目方法')
        migrated = policy.save_policy({'scope': 'project', 'project': str(self.project),
                                       'base_revision': 0})
        self.assertEqual(migrated['project_storage'], 'project')
        self.assertNotIn(str(self.project.resolve()), registry.read_json(global_path)['projects'])
        self.assertEqual(migrated['custom_rules'], '旧项目方法')

    def test_governance_route_saves_global_and_project_scopes_separately(self):
        import governance_api
        view = {'mode': 'project', 'project': str(self.project)}
        global_result = governance_api.post('/api/engineering-policy', {
            'scope': 'global', 'base_revision': 0, 'global_enabled': True,
        }, view, object())
        project_result = governance_api.post('/api/engineering-policy', {
            'scope': 'project', 'base_revision': 0, 'mode': 'off',
        }, view, object())
        self.assertTrue(global_result['global_enabled'])
        self.assertEqual(project_result['configured_mode'], 'off')
        self.assertEqual(policy.policy_snapshot(str(self.project))['effective_rules'], [])
        self.assertEqual(registry.read_json(self.root / 'desk' / policy.CONFIG_NAME)['projects'], {})


class ClockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / 'project'
        self.project.mkdir()
        self.enterContext(patch.object(registry, 'HERE', self.root / 'desk'))
        self.enterContext(patch.object(registry, 'REGISTRY', self.root / 'desk' / 'models.json'))
        self.enterContext(patch.object(registry, 'RUNS', self.root / 'desk' / 'runs'))
        self.goal = stage_store.create_goal('Goal', 'deliver and verify', str(self.project), store='project')['id']

    def start(self, **values):
        clock.configure_clock(str(self.project), self.goal, **values)
        return clock.control(str(self.project), self.goal, 'start', {'model_id': 'test-brain'})

    def claim(self, now=None):
        return clock.claim_due(str(self.project), self.goal, 'fixture-owner', now=now)

    def test_legacy_reads_and_dispatch_preserve_unconfigured_goal(self):
        self.assertFalse(clock.snapshot(str(self.project), self.goal)['exists'])
        self.assertTrue(clock.dispatch_guard(str(self.project), self.goal)['allowed'])
        with stage_store.connection(str(self.project), create=False) as conn:
            self.assertFalse(store.exists(conn))

    def test_due_claim_is_single_owner_and_missed_periods_coalesce(self):
        self.start(interval_seconds=1800)
        at = time.time() + 20000
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda number: clock.claim_due(str(self.project), self.goal, f'owner-{number}', now=at), range(8)))
        claims = [record for record in results if record is not None]
        self.assertEqual(len(claims), 1)
        current = clock.snapshot(str(self.project), self.goal)
        self.assertAlmostEqual(store.timestamp(current['next_due_at']), at + 1800, places=3)
        self.assertEqual(current['active_intent']['id'], claims[0]['id'])

    def test_new_goal_claim_snapshots_native_auto(self):
        self.start()
        self.assertEqual(self.claim()['brain_permission_mode'], 'auto')

    def test_historical_clock_without_permission_field_retains_read_only(self):
        self.start()
        with store.mutation(str(self.project), self.goal) as conn:
            previous = store.read(conn, self.goal)
            previous.pop('brain_permission_mode')
            store.write(conn, previous)
        self.assertEqual(self.claim()['brain_permission_mode'], 'read_only')

    def test_round_completion_keeps_clock_and_does_not_shorten_period(self):
        self.start(interval_seconds=1800)
        record = self.claim()
        before = clock.snapshot(str(self.project), self.goal)
        after = clock.finish_round(str(self.project), self.goal, record['id'], record['authority_epoch'], {'status': 'completed'})
        self.assertTrue(after['enabled'])
        self.assertFalse(after['closed'])
        self.assertEqual(after['next_due_at'], before['next_due_at'])
        self.assertIsNone(self.claim())
        backed = clock.control(str(self.project), self.goal, 'backoff', {'interval_seconds': 3600})
        self.assertEqual(backed['interval_seconds'], 3600)
        with self.assertRaises(clock.ClockError):
            clock.control(str(self.project), self.goal, 'backoff', {'interval_seconds': 1800})

    def test_stop_revokes_old_receipts_and_cannot_be_undone_by_resource_clear(self):
        self.start()
        record = self.claim()
        stopped = clock.control(str(self.project), self.goal, 'stop')
        self.assertGreater(stopped['authority_epoch'], record['authority_epoch'])
        self.assertFalse(clock.dispatch_guard(str(self.project), self.goal, authority_epoch=record['authority_epoch'])['allowed'])
        with self.assertRaises(clock.ClockError):
            clock.finish_round(str(self.project), self.goal, record['id'], record['authority_epoch'], {'status': 'completed'})
        cleared = clock.control(str(self.project), self.goal, 'clear_hold', {'evidence': 'resource dropped'})
        self.assertEqual(cleared['user_intent'], 'STOP')
        self.assertFalse(cleared['enabled'])
        self.assertIsNone(self.claim(now=time.time() + 100000))

    def test_stop_retains_unconfirmed_claim_and_rejects_guessed_recovery(self):
        self.start()
        record = self.claim()
        stopped = clock.control(str(self.project), self.goal, 'stop')
        self.assertEqual(stopped['active_intent']['id'], record['id'])
        with self.assertRaises(clock.ClockError):
            clock.control(str(self.project), self.goal, 'start')
        with self.assertRaises(clock.ClockError):
            clock.control(str(self.project), self.goal, 'reconcile', {
                'intent_id': record['id'], 'worker_exited': True, 'evidence': 'unverified user assertion'})

    def test_original_host_can_release_its_proven_uninvoked_runner_without_resuming_stop(self):
        self.start()
        record = self.claim()
        stopped = clock.control(str(self.project), self.goal, 'stop')
        with self.assertRaises(clock.ClockError):
            clock.release_unstarted(str(self.project), self.goal, record['id'], record['authority_epoch'],
                                    'different-owner', 'never invoked')
        released = clock.release_unstarted(str(self.project), self.goal, record['id'], record['authority_epoch'],
                                          record['owner_id'], 'original host never invoked runner')
        self.assertIsNone(released['active_intent'])
        self.assertGreater(released['authority_epoch'], stopped['authority_epoch'])
        self.assertEqual(released['user_intent'], 'STOP')
        self.assertFalse(released['enabled'])

    def test_native_session_keeps_original_model_and_current_hold_keeps_receipt(self):
        self.start(brain_model_id='test-brain', native_session_id='native-session')
        with self.assertRaises(clock.ClockError):
            clock.configure_clock(str(self.project), self.goal, enabled=True, brain_model_id='different')
        record = self.claim()
        clock.control(str(self.project), self.goal, 'hold', {'kind': 'approval', 'reason': 'review submitted'})
        finished = clock.finish_round(str(self.project), self.goal, record['id'], record['authority_epoch'],
                                     {'status': 'completed', 'native_session_id': 'native-session'})
        self.assertIsNone(finished['active_intent'])
        self.assertEqual(finished['hold']['kind'], 'approval')
        self.assertEqual(finished['native_session_id'], 'native-session')
        self.assertEqual(finished['brain_model_id'], 'test-brain')

    def test_explicit_enabled_config_activates_new_clock(self):
        state = clock.configure_clock(str(self.project), self.goal, enabled=True)
        self.assertTrue(state['enabled'])
        self.assertEqual(state['user_intent'], 'RUN')
        self.assertIsNotNone(self.claim())

    def test_resource_resume_does_not_override_user_pause_or_original_guard(self):
        self.start()
        clock.control(str(self.project), self.goal, 'hold', {'kind': 'resource', 'reason': 'high water', 'guard_id': 'disk'})
        clock.control(str(self.project), self.goal, 'pause')
        with self.assertRaises(clock.ClockError):
            clock.control(str(self.project), self.goal, 'clear_hold', {'guard_id': 'other', 'evidence': 'low water'})
        state = clock.control(str(self.project), self.goal, 'clear_hold', {'guard_id': 'disk', 'evidence': 'same guard lower water'})
        self.assertEqual(state['user_intent'], 'USER_PAUSE')
        self.assertFalse(state['enabled'])

    def test_expired_ambiguous_launch_becomes_unknown_instead_of_replay(self):
        self.start()
        record = self.claim()
        self.assertIsNone(self.claim(now=time.time() + 2000))
        state = clock.snapshot(str(self.project), self.goal)
        self.assertEqual(state['hold']['kind'], 'unknown')
        self.assertEqual(state['active_intent']['id'], record['id'])
        with self.assertRaises(clock.ClockError):
            clock.control(str(self.project), self.goal, 'clear_hold', {'authorized': True, 'evidence': 'guess'})

    def test_close_requires_separate_obligations_and_accepted_stage(self):
        self.start(obligations=[{'id': 'report', 'title': '交付分析报告'}, {'id': 'cleanup', 'title': '清理授权临时产物'}])
        with self.assertRaises(clock.ClockError):
            clock.control(str(self.project), self.goal, 'close')
        clock.control(str(self.project), self.goal, 'obligation', {'id': 'report', 'status': 'fulfilled', 'evidence': 'report sha256'})
        clock.control(str(self.project), self.goal, 'obligation', {'id': 'cleanup', 'status': 'cancelled', 'evidence': 'user retained artifacts'})
        with self.assertRaises(clock.ClockError):
            clock.control(str(self.project), self.goal, 'close')
        stage = stage_store.add_stage(self.goal, 'verify', 'verify output', acceptance=['accepted'], project=str(self.project))
        with stage_store.connection(str(self.project), create=False) as conn, stage_store._transaction(conn):
            conn.execute("UPDATE stages SET status='accepted' WHERE id=?", (stage['id'],))
            conn.execute("UPDATE goals SET status='completed' WHERE id=?", (self.goal,))
        state = clock.control(str(self.project), self.goal, 'close')
        self.assertTrue(state['closed'])
        self.assertFalse(state['enabled'])
        self.assertIsNone(self.claim(now=time.time() + 100000))

    def test_authority_lock_serializes_stop_after_current_effect(self):
        state = self.start()
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        def effect():
            with clock.authority_lock(str(self.project), self.goal, state['authority_epoch']):
                entered.set()
                release.wait(2)
        def stop():
            clock.control(str(self.project), self.goal, 'stop')
            stopped.set()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(effect)
            self.assertTrue(entered.wait(2))
            second = pool.submit(stop)
            self.assertFalse(stopped.wait(.05))
            release.set()
            first.result(timeout=2)
            second.result(timeout=2)
        with self.assertRaises(clock.ClockError):
            with clock.authority_lock(str(self.project), self.goal, state['authority_epoch']):
                self.fail('stale authority entered')


if __name__ == '__main__':
    unittest.main()
