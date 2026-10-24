"""Native planning lifecycle tests; all providers and generation are mocked."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cli_worker
import continuation_clock as clock
import delegation_policy
import dynamic_plan
import engineering_policy
import goal_brain_context
import goal_brain_contract
import goal_brain_runner as runner
import goal_controller as controller
import goal_recovery
import goal_requirements
import model_registry as registry
import model_router
import panel_server
import stage_store
import workflow_settings
import workspace_config


class GoalControllerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.project = self.root / 'project'
        self.project.mkdir()
        self.project_text = str(self.project)
        for name, value in [('HERE', self.root / 'desk'), ('REGISTRY', self.root / 'desk/models.json'),
                            ('RUNS', self.root / 'desk/runs')]:
            self.enterContext(patch.object(registry, name, value))
        self.enterContext(patch.object(workspace_config.project_views, 'project_bookmarks', return_value=[]))
        self.enterContext(patch.object(goal_brain_context.execution_endpoints, 'gate', return_value='codex_cli'))
        # Native CLI discovery is host state; these mocked tests must not require an installed CLI.
        self.enterContext(patch.object(cli_worker.shutil, 'which',
                                       side_effect=lambda name: '/fixture/bin/' + name))
        self.enterContext(patch.object(controller, '_panel', None))
        controller._STOP.clear()
        model = registry.upsert_model({'provider': 'codex_cli', 'name': 'Fixture', 'model': 'fixture-model'})
        self.model_id = model['id']
        workspace_config.update_project({'action': 'add', 'path': self.project_text})
        workspace_config.update_binding({'project': self.project_text, 'model_id': self.model_id,
            'role': 'decision', 'is_default': True, 'effort': 'high', 'fast': True,
            'context_window': 180000, 'autocompact_tokens': 140000})
        workflow_settings.save_settings({'enabled': True, 'paid_allowed': True,
            'enforce_paid_budget': False, 'enforce_model_call_limit': False})
        self.goal = stage_store.create_goal('Build a fixture', 'Create an evidence-bound document',
                                            self.project_text, store='project')['id']
        dynamic_plan.configure_goal(self.project_text, self.goal, {'criteria': ['Document meets the request']})
        clock.configure_clock(self.project_text, self.goal, interval_seconds=1800,
                              brain_model_id=self.model_id)
        clock.control(self.project_text, self.goal, 'start')
        self.panel = Mock()

    def actions(self, with_plan=True):
        return {'plan': {'base_revision': 0, 'reason': 'Inspect requirements before writing', 'stages': [
            {'key': 'inspect', 'title': 'Inspect fixture inputs', 'objective': 'Read the project inputs',
             'kind': 'chat', 'depends_on': [], 'files': [], 'acceptance': ['Relevant inputs identified'],
             'candidates': [], 'fixed_model': ''}]} if with_plan else None,
            'questions': [], 'dispatch_stage_ids': [], 'reviews': [], 'obligations': [], 'complete': False,
            'summary': 'Inputs require inspection'}

    def fake_run(self, actions=None):
        def execute(context, engine, model, runtime, directory, on_started, should_stop, **kwargs):
            with kwargs['spawn_guard']():
                on_started(900001, 1234.0, 'native-fixture-session')
            return {'actions': actions or self.actions(), 'native_session_id': 'native-fixture-session',
                    'receipt': str(directory / 'actions.json')}
        return execute

    def accepted_stage(self, accept=True):
        plan = dynamic_plan.revise_plan(self.project_text, self.goal, self.actions()['plan'])
        stage = plan['stages'][0]
        task_id = 'accepted-fixture'
        run = registry.prepare_task_dir(task_id, self.project_text)
        report = self.project / 'report.md'
        report.write_text('Fixture inputs inspected; requested document is available.\n', encoding='utf-8')
        registry.patch_task(run, id=task_id, project=self.project_text, goal_id=self.goal,
            stage_id=stage['id'], kind='chat', engine='codex_cli', model_id=self.model_id,
            status='completed', files=[], delegation={'schema': 1, 'mode': 'outcome', 'rework_count': 0},
            route={'mode': 'auto', 'requirements': {'acceptance': stage['acceptance']}})
        stage_store.claim_dispatch(stage['id'], task_id, project=self.project_text)
        delegation_policy.record_executor(run, {'completed': True})
        review = delegation_policy.load(run)
        criteria = [{'id': item['id'], 'status': 'pass', 'evidence': [
            {'source': 'reviewer_file', 'detail': 'Read report.md: ' + report.read_text(encoding='utf-8')}]
            } for item in review['criteria']]
        if accept:
            delegation_policy.accept(run, 'main', criteria)
            stage_store.sync_from_tasks(self.goal, project=self.project_text)
            self.assertEqual(stage_store.get_stage(stage['id'], self.project_text)['status'], stage_store.ACCEPTED)
        return report

    def test_read_and_import_do_not_start_or_generate(self):
        with patch.object(runner, 'run') as invoke:
            clock.snapshot(self.project_text, self.goal)
            dynamic_plan.describe_goal(self.project_text, self.goal)
            self.assertIsNone(controller._driver)
        invoke.assert_not_called()

    def test_valid_native_plan_applies_without_human_approval_and_preserves_cadence(self):
        with patch.object(runner, 'run', side_effect=self.fake_run()) as invoke:
            finished = controller.run_once(self.project_text, self.goal, self.panel)
        plan = dynamic_plan.describe_goal(self.project_text, self.goal)
        self.assertEqual(plan['plan_revision'], 1)
        self.assertEqual(plan['criteria'], ['Document meets the request'])
        self.assertEqual(plan['stages'][0]['title'], 'Inspect fixture inputs')
        self.assertEqual(finished['native_session_id'], 'native-fixture-session')
        self.assertEqual(finished['interval_seconds'], 1800)
        self.assertEqual(invoke.call_args.args[3]['effort'], 'high')
        self.assertTrue(invoke.call_args.args[3]['fast'])
        self.assertEqual(invoke.call_args.kwargs['permission_mode'], 'auto')
        self.assertFalse(finished['active_intent'])
        with sqlite3.connect(stage_store.db_path(self.project_text)) as conn:
            phases = conn.execute('SELECT step,status FROM goal_graph_steps ORDER BY step').fetchall()
        self.assertEqual(phases, [('actions', 'done'), ('native', 'done'), ('settle', 'done'), ('snapshot', 'done')])
        self.assertTrue((self.project / '.model-desk/langgraph/checkpoints.sqlite').is_file())

    def test_control_cannot_inject_unverified_session_from_another_project(self):
        with patch.object(runner, 'run') as invoke, self.assertRaises(goal_brain_contract.BrainError):
            controller.control_goal(self.project_text, self.goal, 'resume',
                                    {'native_session_id': 'foreign-session'})
        invoke.assert_not_called()

    def test_paid_gate_prevents_generation(self):
        workflow_settings.save_settings({'paid_allowed': False})
        with patch.object(runner, 'run') as invoke:
            result = controller.run_once(self.project_text, self.goal, self.panel)
        invoke.assert_not_called()
        self.assertEqual(result['hold']['kind'], 'approval')
        self.assertEqual(dynamic_plan.describe_goal(self.project_text, self.goal)['plan_revision'], 0)

    def test_execution_scope_does_not_filter_authorized_decision_brain(self):
        workflow_settings.save_settings({'model_scope': 'local_only'})
        selected = controller.select_brain(self.project_text, self.model_id)
        self.assertEqual(selected.model_id, self.model_id)
        self.assertEqual(selected.engine, 'codex_cli')
        settings = workflow_settings.snapshot()
        models = registry.list_models()['models']
        with self.assertRaisesRegex(ValueError, '执行模型范围'):
            model_router.choose({'route_mode': 'manual', 'model_id': self.model_id,
                                 'kind': 'chat', 'prompt': 'Read fixture inputs'}, models, settings)
        local = registry.upsert_model({'provider': 'local', 'name': 'Local executor fixture',
            'model': 'local-fixture', 'api_base': 'http://127.0.0.1:9999/v1'})
        routed = model_router.choose({'route_mode': 'manual', 'model_id': local['id'],
                                      'kind': 'chat', 'prompt': 'Read fixture inputs'},
                                     registry.list_models()['models'], settings)
        self.assertEqual(routed['model_id'], local['id'])
        self.assertEqual(workflow_settings.snapshot()['model_scope'], 'local_only')

    def test_fixed_contract_cannot_be_lowered(self):
        actions = self.actions()
        actions['plan']['criteria'] = ['Weaker requirement']
        with self.assertRaises(goal_brain_contract.BrainError):
            goal_brain_contract.parse_actions(json.dumps(actions))

    def test_malformed_native_output_never_fabricates_stages(self):
        def malformed(*args, **kwargs):
            args[5](900001, 1234.0, 'native-fixture-session')
            goal_brain_contract.parse_actions('please just use the standard phases')
        with patch.object(runner, 'run', side_effect=malformed):
            result = controller.run_once(self.project_text, self.goal, self.panel)
        self.assertEqual(result['hold']['kind'], 'unknown')
        self.assertEqual(dynamic_plan.describe_goal(self.project_text, self.goal)['stages'], [])
        with patch.object(runner, 'run') as invoke:
            self.assertIsNone(controller.run_once(self.project_text, self.goal, self.panel))
        invoke.assert_not_called()

    def test_stop_race_fences_plan_and_dispatch(self):
        entered = threading.Event()
        release = threading.Event()
        def racing(*args, **kwargs):
            args[5](900001, 1234.0, 'native-fixture-session')
            entered.set()
            release.wait(3)
            return {'actions': self.actions(), 'native_session_id': 'native-fixture-session', 'receipt': 'fixture'}
        with patch.object(runner, 'run', side_effect=racing), patch.object(runner, 'stop_owned', return_value=False):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(controller.run_once, self.project_text, self.goal, self.panel)
                self.assertTrue(entered.wait(3))
                controller.control_goal(self.project_text, self.goal, 'stop')
                release.set()
                result = future.result(timeout=5)
        self.assertEqual(result['user_intent'], 'STOP')
        self.assertEqual(dynamic_plan.describe_goal(self.project_text, self.goal)['plan_revision'], 0)
        self.panel.dispatch_stage.assert_not_called()

    def test_requirement_arriving_after_snapshot_supersedes_old_actions(self):
        first_context = []
        _ = goal_requirements.insert(
            self.project_text,
            self.goal,
            "Keep the existing evidence boundary",
            request_id="before-claim",
        )

        def insert_during_native(context, *args, **kwargs):
            first_context.append(context)
            _ = goal_requirements.insert(
                self.project_text,
                self.goal,
                "Replace the pending layout with a compact text flow",
                kind="direction_change",
                request_id="during-native",
            )
            return self.fake_run()(context, *args, **kwargs)

        with patch.object(runner, "run", side_effect=insert_during_native):
            superseded = controller.run_once(self.project_text, self.goal, self.panel)
        self.assertEqual(
            [item["text"] for item in first_context[0]["latest_requirements"]],
            ["Keep the existing evidence boundary"],
        )
        self.assertEqual(superseded["last_result"]["status"], "superseded")
        self.assertFalse(superseded["hold"])
        self.assertFalse(superseded["active_intent"])
        self.assertEqual(dynamic_plan.describe_goal(self.project_text, self.goal)["plan_revision"], 0)
        self.panel.dispatch_stage.assert_not_called()
        with sqlite3.connect(stage_store.db_path(self.project_text)) as database:
            first_phases = database.execute(
                "SELECT step,status FROM goal_graph_steps ORDER BY step"
            ).fetchall()
        self.assertEqual(
            first_phases,
            [("actions", "done"), ("native", "done"), ("settle", "done"), ("snapshot", "done")],
        )
        pending = goal_requirements.list_for_goal(self.project_text, self.goal)
        self.assertEqual([item["status"] for item in pending], ["pending", "pending"])

        second_context = []

        def replan(context, *args, **kwargs):
            second_context.append(context)
            return self.fake_run()(context, *args, **kwargs)

        with patch.object(runner, "run", side_effect=replan):
            completed = controller.run_once(self.project_text, self.goal, self.panel)
        self.assertEqual(
            [item["text"] for item in second_context[0]["latest_requirements"]],
            [
                "Keep the existing evidence boundary",
                "Replace the pending layout with a compact text flow",
            ],
        )
        self.assertEqual(completed["last_result"]["status"], "completed")
        self.assertEqual(dynamic_plan.describe_goal(self.project_text, self.goal)["plan_revision"], 1)
        applied = goal_requirements.list_for_goal(self.project_text, self.goal)
        self.assertEqual([item["status"] for item in applied], ["applied", "applied"])

    def test_direction_change_stops_the_old_native_round_without_an_unknown_hold(self):
        observed = []

        def interrupted(*args, **kwargs):
            args[5](900001, 1234.0, 'native-fixture-session')
            _ = goal_requirements.insert(
                self.project_text,
                self.goal,
                "Replace the active plan now",
                kind="direction_change",
                request_id="stop-native-round",
            )
            observed.append(args[6]())
            raise goal_brain_contract.BrainError("fixture native process observed stop")

        with patch.object(runner, "run", side_effect=interrupted):
            result = controller.run_once(self.project_text, self.goal, self.panel)
        self.assertEqual(observed, [True])
        self.assertEqual(result["last_result"]["status"], "superseded")
        self.assertFalse(result["hold"])
        self.assertFalse(result["active_intent"])
        self.assertEqual(dynamic_plan.describe_goal(self.project_text, self.goal)["plan_revision"], 0)
        self.panel.dispatch_stage.assert_not_called()

    def test_policy_snapshot_is_immutable_during_native_round(self):
        engineering_policy.save_policy({'global_enabled': True, 'global_custom_rules': 'Original habit'})
        seen = []
        def change_after_snapshot(context, *args, **kwargs):
            seen.append(context['engineering_policy'])
            engineering_policy.save_policy({'global_custom_rules': 'Changed habit'})
            return self.fake_run()(context, *args, **kwargs)
        with patch.object(runner, 'run', side_effect=change_after_snapshot):
            controller.run_once(self.project_text, self.goal, self.panel)
        self.assertEqual(seen[0]['snapshot']['global_custom_rules'], 'Original habit')
        current = engineering_policy.task_context(self.project_text)
        self.assertNotEqual(seen[0]['snapshot_id'], current['snapshot_id'])
        status = engineering_policy.goal_status(self.project_text, self.goal, current['snapshot_id'])
        self.assertEqual(status['status'], 'stale')
        self.assertEqual(status['loaded_snapshot_id'], seen[0]['snapshot_id'])
        self.assertTrue(status['confirmed'])
        self.assertTrue(status['is_current_round'])

    def test_historical_read_only_commands_resume_original_session_and_disable_customizations(self):
        with patch.object(runner.cli_worker, 'availability', return_value={'executable': '/fixture/cli'}):
            args, session = runner.command('codex_cli', 'fixture', self.project, self.root/'out',
                                           self.root/'schema', {}, 'original-session', permission_mode='read_only')
            self.assertEqual(args[1:3], ['exec', 'resume'])
            self.assertIn('--ignore-user-config', args)
            self.assertIn('sandbox_mode="read-only"', args)
            self.assertEqual(session, 'original-session')
            args, session = runner.command('claude_cli', 'fixture', self.project, self.root/'out',
                                           self.root/'schema', {}, 'original-session', permission_mode='read_only')
            self.assertIn('--safe-mode', args)
            self.assertIn('--restricted', args)
            self.assertEqual(args[-2:], ['--resume', 'original-session'])

    def test_stop_only_exact_process_birth(self):
        with patch.object(runner, 'identity', return_value=runner.ProcessIdentity(7, 20.0)), \
                patch.object(runner.platform_runtime, 'stop_tree') as terminate:
            self.assertFalse(runner.stop_owned(7, 10.0))
            terminate.assert_not_called()
            self.assertTrue(runner.stop_owned(7, 20.0))
            terminate.assert_called_once_with(7)

    def test_verified_exit_with_unparsed_auto_output_retains_unknown_owner(self):
        def invalid(*args, **kwargs):
            args[5](900001, 1234.0, 'native-fixture-session')
            kwargs['on_exited']()
            goal_brain_contract.parse_actions('invalid JSON')
        with patch.object(runner, 'run', side_effect=invalid):
            result = controller.run_once(self.project_text, self.goal, self.panel)
        self.assertEqual(result['hold']['kind'], 'unknown')
        self.assertEqual(result['active_intent']['status'], 'unknown')
        self.assertEqual(result['native_session_id'], 'native-fixture-session')
        with patch.object(runner, 'inspect_owned', return_value=('exited', 'Fixture exit')):
            inspected = controller.control_goal(self.project_text, self.goal, 'inspect')
        self.assertEqual(inspected['active_intent']['id'], result['active_intent']['id'])
        with self.assertRaises(clock.ClockError):
            controller.control_goal(self.project_text, self.goal, 'clear_hold',
                {'authorized': True, 'evidence': 'Exit alone cannot account for native auto effects'})

    def test_inspect_reconciles_proven_exit_without_resuming_user_pause(self):
        claim = clock.claim_due(self.project_text, self.goal, 'fixture')
        clock.record_runner(self.project_text, self.goal, claim['id'], claim['authority_epoch'],
                            900001, 1234.0, native_session_id='same-session')
        controller.control_goal(self.project_text, self.goal, 'pause')
        with patch.object(runner, 'inspect_owned', return_value=('exited', 'Original PID no longer exists')):
            result = controller.control_goal(self.project_text, self.goal, 'inspect')
        self.assertEqual(result['user_intent'], 'USER_PAUSE')
        self.assertFalse(result['enabled'])
        self.assertFalse(result['active_intent'])
        self.assertEqual(result['native_session_id'], 'same-session')
        resumed = controller.control_goal(self.project_text, self.goal, 'resume')
        self.assertTrue(resumed['enabled'])
        self.assertEqual(resumed['native_session_id'], 'same-session')

    def test_inspect_live_or_unreadable_process_never_releases_owner(self):
        claim = clock.claim_due(self.project_text, self.goal, 'fixture')
        clock.record_runner(self.project_text, self.goal, claim['id'], claim['authority_epoch'], 900001, 1234.0)
        for verdict in ('running', 'unknown'):
            with self.subTest(verdict=verdict), patch.object(runner, 'inspect_owned', return_value=(verdict, 'proof unavailable')):
                result = controller.control_goal(self.project_text, self.goal, 'reconcile_owned')
            self.assertEqual(result['active_intent']['id'], claim['id'])
            self.assertEqual(result['process_inspection']['state'], verdict)

    def test_inspect_missing_process_identity_cannot_use_human_exit_checkbox(self):
        claim = clock.claim_due(self.project_text, self.goal, 'fixture')
        clock.control(self.project_text, self.goal, 'stop')
        result = controller.control_goal(self.project_text, self.goal, 'reconcile',
                                        {'worker_exited': True, 'evidence': 'unchecked human assertion'})
        self.assertEqual(result['process_inspection']['state'], 'unknown')
        self.assertEqual(result['active_intent']['id'], claim['id'])
        self.assertFalse(result['enabled'])

    def test_changed_human_criteria_fences_old_native_proposal(self):
        def changed(context, *args, **kwargs):
            dynamic_plan.configure_goal(self.project_text, self.goal, {'criteria': ['New stronger criterion']})
            return self.fake_run()(context, *args, **kwargs)
        with patch.object(runner, 'run', side_effect=changed):
            controller.run_once(self.project_text, self.goal, self.panel)
        plan = dynamic_plan.describe_goal(self.project_text, self.goal)
        self.assertEqual(plan['plan_revision'], 0)
        self.assertEqual(plan['criteria'], ['New stronger criterion'])

    def test_human_pause_before_runner_call_releases_only_proven_unlaunched_intent(self):
        real_select = controller.select_brain
        def pause_during_selection(project, model_id=''):
            selected = real_select(project, model_id)
            clock.control(project, self.goal, 'pause')
            return selected
        with patch.object(controller, 'select_brain', side_effect=pause_during_selection), \
                patch.object(runner, 'run') as native:
            result = controller.run_once(self.project_text, self.goal, self.panel)
        native.assert_not_called()
        self.assertEqual(result['user_intent'], 'USER_PAUSE')
        self.assertFalse(result['active_intent'])
        self.assertFalse(result['enabled'])

    def test_completion_waits_for_verified_obligations_and_preserves_clock_until_then(self):
        report = self.accepted_stage()
        clock.configure_clock(self.project_text, self.goal, enabled=True,
            obligations=[{'id': 'report', 'title': '归档交付报告'}])
        actions = self.actions(with_plan=False)
        actions['complete'] = True
        seen = []
        def pending_round(context, *args, **kwargs):
            seen.append(context['clock'])
            return self.fake_run(actions)(context, *args, **kwargs)
        with patch.object(runner, 'run', side_effect=pending_round):
            waiting = controller.run_once(self.project_text, self.goal, self.panel)
        self.assertEqual(seen[0]['obligations'][0]['status'], 'pending')
        self.assertEqual(stage_store.get_goal(self.goal, self.project_text)['status'], stage_store.GOAL_ACTIVE)
        self.assertTrue(waiting['enabled'])
        self.assertFalse(waiting['closed'])
        self.assertEqual(waiting['next_due_at'], seen[0]['next_due_at'])
        self.assertEqual(waiting['interval_seconds'], 1800)
        self.assertIn('收尾义务尚未全部核验', waiting['last_result']['summary'])
        original_clock_id = waiting['clock_id']
        clock.control(self.project_text, self.goal, 'run_now')
        evidence = '独立读取 report.md：' + report.read_text(encoding='utf-8').strip()
        actions['obligations'] = [{'id': 'report', 'status': 'fulfilled', 'evidence': evidence}]
        with patch.object(runner, 'run', side_effect=self.fake_run(actions)):
            finished = controller.run_once(self.project_text, self.goal, self.panel)
        self.assertEqual(finished['clock_id'], original_clock_id)
        self.assertEqual(finished['obligations'][0]['status'], 'fulfilled')
        self.assertEqual(finished['obligations'][0]['evidence'], evidence)
        self.assertTrue(finished['closed'])
        self.assertEqual(stage_store.get_goal(self.goal, self.project_text)['status'], stage_store.GOAL_COMPLETED)

    def test_native_actions_cannot_cancel_human_obligations(self):
        actions = self.actions()
        actions['obligations'] = [{'id': 'report', 'status': 'cancelled', 'evidence': 'AI considers it optional'}]
        with self.assertRaises(goal_brain_contract.BrainError):
            goal_brain_contract.parse_actions(json.dumps(actions))

    def test_obligation_requires_concrete_nonempty_evidence(self):
        clock.configure_clock(self.project_text, self.goal, enabled=True,
            obligations=[{'id': 'report', 'title': '归档交付报告'}])
        actions = self.actions(with_plan=False)
        actions['obligations'] = [{'id': 'report', 'status': 'fulfilled', 'evidence': '   '}]
        with patch.object(runner, 'run', side_effect=self.fake_run(actions)):
            result = controller.run_once(self.project_text, self.goal, self.panel)
        self.assertEqual(result['obligations'][0]['status'], 'pending')
        self.assertEqual(stage_store.get_goal(self.goal, self.project_text)['status'], stage_store.GOAL_ACTIVE)

    def test_ai_fulfillment_does_not_rewrite_existing_human_evidence(self):
        clock.configure_clock(self.project_text, self.goal, enabled=True,
            obligations=[{'id': 'report', 'title': '归档交付报告'}])
        clock.control(self.project_text, self.goal, 'obligation',
                      {'id': 'report', 'status': 'fulfilled', 'evidence': 'Human verified original archive digest'})
        actions = self.actions(with_plan=False)
        actions['obligations'] = [{'id': 'report', 'status': 'fulfilled', 'evidence': 'New model summary'}]
        with patch.object(runner, 'run', side_effect=self.fake_run(actions)):
            result = controller.run_once(self.project_text, self.goal, self.panel)
        self.assertEqual(result['obligations'][0]['evidence'], 'Human verified original archive digest')

    def test_native_review_passes_real_panel_acceptance_boundary(self):
        report = self.accepted_stage(accept=False)
        run = registry.task_dir('accepted-fixture')
        review = delegation_policy.load(run)
        actions = self.actions(with_plan=False)
        actions['reviews'] = [{'task_id': 'accepted-fixture', 'decision': 'accept', 'criteria': [
            {'id': item['id'], 'status': 'pass', 'evidence': [{'source': 'reviewer_file',
             'detail': 'Independent read report.md: ' + report.read_text(encoding='utf-8'),
             'exit_code': None}], 'note': ''} for item in review['criteria']],
            'problems': '', 'boundaries': '', 'notes': 'Main brain inspected actual report file'}]
        self.panel.update_review.side_effect = panel_server.update_review
        with patch.object(runner, 'run', side_effect=self.fake_run(actions)):
            result = controller.run_once(self.project_text, self.goal, self.panel)
        self.assertFalse(result['hold'])
        self.panel.update_review.assert_called_once()
        stage = dynamic_plan.describe_goal(self.project_text, self.goal)['stages'][0]
        self.assertEqual(stage['status'], stage_store.ACCEPTED)
        self.assertEqual(delegation_policy.load(run)['state'], delegation_policy.ACCEPTED)

    def test_context_loads_only_actual_project_rules_with_byte_hashes(self):
        raw = '项目规则：先检查证据，再决定阶段。\r\n'.encode('utf-8')
        (self.project / 'AGENTS.md').write_bytes(raw)
        (self.project / 'CLAUDE.md').write_text('Respect project boundaries.\n', encoding='utf-8')
        selection = controller.select_brain(self.project_text, self.model_id)
        context = goal_brain_context.build_context(self.project_text, self.goal, selection)
        self.assertEqual(context['project_guidance'][0], {
            'path': 'AGENTS.md', 'sha256': hashlib.sha256(raw).hexdigest(), 'text': raw.decode('utf-8')})
        self.assertEqual([rule['path'] for rule in context['project_guidance']], ['AGENTS.md', 'CLAUDE.md'])
        self.assertNotIn(str(Path.home()), json.dumps(context['project_guidance']))

    def test_project_guidance_rejects_escaped_links_and_oversized_rules(self):
        outside = self.root / 'outside.md'
        outside.write_text('Other project or global instructions', encoding='utf-8')
        instruction = self.project / 'AGENTS.md'
        instruction.symlink_to(outside)
        with self.assertRaises(goal_brain_contract.BrainError):
            goal_brain_context.project_guidance(self.project_text)
        instruction.unlink()
        instruction.write_bytes(b'x' * (goal_brain_context.GUIDANCE_LIMIT_BYTES + 1))
        with self.assertRaises(goal_brain_contract.BrainError):
            goal_brain_context.project_guidance(self.project_text)

    def test_native_question_must_explicitly_name_approval_option(self):
        actions = self.actions(with_plan=False)
        question = {'question': 'Should the current proposal continue?', 'reason': 'A human tradeoff is needed',
            'options': ['Request more evidence', 'Continue the current proposal'],
            'recommendation': 'Request more evidence', 'evidence_refs': ['report.md'],
            'affected_stage_ids': ['inspect']}
        actions['questions'] = [question]
        with self.assertRaises(goal_brain_contract.BrainError):
            goal_brain_contract.parse_actions(json.dumps(actions))
        question['approval_option'] = 'Continue the current proposal'
        parsed = goal_brain_contract.parse_actions(json.dumps(actions))
        self.assertEqual(parsed['questions'][0]['approval_option'], question['options'][1])


if __name__ == '__main__':
    unittest.main()
