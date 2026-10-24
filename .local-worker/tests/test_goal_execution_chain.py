"""Real panel dispatch/review/handover/rework chains, with model executors stopped."""
import hashlib
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import continuation_clock as clock
import cli_worker
import delegation_policy
import dynamic_plan
import execution_endpoints
import goal_brain_runner as runner
import goal_controller as controller
import model_registry as registry
import panel_server as panel
import task_handover
import workflow_settings
import workspace_config
import test_goal_controller as fixtures

REAL_ENDPOINT_GATE = execution_endpoints.gate


class GoalExecutionChainTests(unittest.TestCase):
    actions = fixtures.GoalControllerTests.actions
    fake_run = fixtures.GoalControllerTests.fake_run

    def setUp(self):
        fixtures.GoalControllerTests.setUp(self)
        # Replace the fixture's endpoint stub with the real configured guard.
        self.enterContext(patch.object(execution_endpoints, 'gate', REAL_ENDPOINT_GATE))
        execution_endpoints.update('codex_cli', {'enabled': True, 'current_model': self.model_id}, {})
        workspace_config.update_binding({'project': self.project_text, 'model_id': self.model_id,
                                        'role': 'execution', 'is_default': True})
        self.report = self.project / 'report.md'
        self.report.write_text('Verified fixture findings and interface decisions.\n', encoding='utf-8')
        dynamic_plan.configure_goal(self.project_text, self.goal,
            {'criteria': ['Document meets the request'], 'allowed_files': ['report.md']})
        self.queued = self.enterContext(patch.object(panel.EXECUTOR, 'submit'))

    def invoke(self, actions):
        with patch.object(runner, 'run', side_effect=self.fake_run(actions)):
            return controller.run_once(self.project_text, self.goal, panel)

    def first_stage(self):
        actions = self.actions()
        first = actions['plan']['stages'][0]
        first.update({'files': ['report.md'], 'candidates': [self.model_id], 'fixed_model': self.model_id})
        actions['plan']['stages'].append({**first, 'key': 'consume', 'title': 'Use verified inputs',
            'objective': 'Use the verified fixture findings', 'depends_on': ['inspect']})
        actions['dispatch_stage_ids'] = ['inspect']
        result = self.invoke(actions)
        self.assertFalse(result['hold'], result.get('last_result'))
        plan = dynamic_plan.describe_goal(self.project_text, self.goal)
        first, second = plan['stages']
        task_id = first['task_ids'][0]
        run = registry.task_dir(task_id)
        registry.patch_task(run, status='completed', ended_at=registry.now())
        delegation_policy.record_executor(run, {'completed': True})
        clock.control(self.project_text, self.goal, 'run_now')
        return task_id, first, second

    def review_action(self, task_id, decision='accept'):
        review = delegation_policy.load(registry.task_dir(task_id))
        return {'task_id': task_id, 'decision': decision, 'criteria': [
            {'id': item['id'], 'status': 'pass' if decision == 'accept' else 'fail',
             'evidence': [{'source': 'reviewer_file', 'detail': 'Read report.md: ' +
              self.report.read_text(encoding='utf-8'), 'exit_code': None}], 'note': ''}
            for item in review['criteria']],
            'problems': '' if decision == 'accept' else 'The report does not explain the required decision',
            'boundaries': '' if decision == 'accept' else 'Only inspect report.md and explain the missing decision',
            'notes': 'Verified report.md contains the fixture findings and interface decisions'}

    def test_actual_acceptance_creates_hash_checked_handover_and_dispatches_dependency(self):
        task_id, first, second = self.first_stage()
        actions = self.actions(with_plan=False)
        actions['reviews'] = [self.review_action(task_id)]
        actions['dispatch_stage_ids'] = ['consume']
        result = self.invoke(actions)
        self.assertFalse(result['hold'], result.get('last_result'))
        self.assertEqual(self.queued.call_count, 2)
        handover = task_handover.load(registry.task_dir(task_id))
        self.assertTrue(handover['verification']['evidence_independent'])
        self.assertEqual(handover['files'][0]['sha256'], hashlib.sha256(self.report.read_bytes()).hexdigest())
        self.assertTrue(task_handover.verify(registry.task_dir(task_id))['valid'])
        consumer = dynamic_plan.describe_goal(self.project_text, self.goal)['stages'][1]
        consumer_run = registry.task_dir(consumer['active_task_id'])
        prompt = (consumer_run / 'prompt.txt').read_text(encoding='utf-8')
        self.assertIn('前置阶段成果交接', prompt)
        self.assertIn('fixture findings and interface decisions', prompt)

    def test_stale_accepted_handover_still_blocks_dependency(self):
        task_id, first, second = self.first_stage()
        actions = self.actions(with_plan=False)
        actions['reviews'] = [self.review_action(task_id)]
        accepted = self.invoke(actions)
        self.assertFalse(accepted['hold'])
        old_hash = task_handover.load(registry.task_dir(task_id))['files'][0]['sha256']
        self.report.write_text('Changed after independent acceptance.\n', encoding='utf-8')
        clock.control(self.project_text, self.goal, 'run_now')
        actions = self.actions(with_plan=False)
        actions['dispatch_stage_ids'] = ['consume']
        blocked = self.invoke(actions)
        self.assertIn('失效', blocked['last_result']['summary'])
        self.assertEqual(self.queued.call_count, 1)
        self.assertEqual(task_handover.load(registry.task_dir(task_id))['files'][0]['sha256'], old_hash)

    def test_rework_initialization_interruption_keeps_the_same_single_child(self):
        task_id, first, second = self.first_stage()
        actions = self.actions(with_plan=False)
        actions['reviews'] = [self.review_action(task_id, 'return')]
        actions['dispatch_stage_ids'] = ['inspect']
        prepare = registry.prepare_task_dir
        attempts = []

        def interrupted(child_id, project):
            attempts.append(child_id)
            if len(attempts) == 1:
                raise OSError('Fixture child initialization interrupted after parent claim')
            return prepare(child_id, project)

        with patch.object(registry, 'prepare_task_dir', side_effect=interrupted):
            result = self.invoke(actions)
        self.assertFalse(result['hold'], result.get('last_result'))
        self.assertEqual(len(set(attempts)), 1)
        child_id = attempts[0]
        original = delegation_policy.load(registry.task_dir(task_id))
        self.assertEqual(original['rework']['dispatched_task_id'], child_id)
        self.assertEqual(original['rework_count'], delegation_policy.MAX_AUTO_REWORK)
        child = registry.read_task(registry.task_dir(child_id))
        self.assertEqual(child['model_id'], self.model_id)
        self.assertEqual(child['rework_of'], task_id)
        self.assertEqual(child['delegation']['rework_count'], delegation_policy.MAX_AUTO_REWORK)
        self.assertEqual(child['stage_id'], first['id'])
        self.assertEqual(self.queued.call_count, 1)
        panel.resume_queued(self.project_text, self.goal)
        self.assertEqual(self.queued.call_count, 2)

    def test_changed_parent_review_blocks_interrupted_rework_initialization(self):
        task_id, first, second = self.first_stage()
        actions = self.actions(with_plan=False)
        actions['reviews'] = [self.review_action(task_id, 'return')]
        actions['dispatch_stage_ids'] = ['inspect']
        prepare = registry.prepare_task_dir
        attempts = []

        def interrupted(child_id, project):
            attempts.append(child_id)
            if len(attempts) == 1:
                parent_run = registry.task_dir(task_id)
                review = delegation_policy.load(parent_run)
                review['rework']['problems'] = 'Changed after the original initialization admission'
                delegation_policy.save(parent_run, review)
                raise OSError('Fixture parent evidence changed before task publication')
            return prepare(child_id, project)

        with patch.object(registry, 'prepare_task_dir', side_effect=interrupted):
            result = self.invoke(actions)
        self.assertTrue(result['hold'])
        self.assertEqual(self.queued.call_count, 1)
        self.assertEqual(len(attempts), 1)
        self.assertFalse((self.project / '.model-desk/runs' / attempts[0] / 'task.json').exists())
        original = delegation_policy.load(registry.task_dir(task_id))
        self.assertEqual(original['rework']['dispatched_task_id'], attempts[0])
        self.assertEqual(original['rework_count'], delegation_policy.MAX_AUTO_REWORK)

    def test_returned_stage_dispatches_one_real_rework_with_original_identity(self):
        task_id, first, second = self.first_stage()
        actions = self.actions(with_plan=False)
        actions['reviews'] = [self.review_action(task_id, 'return')]
        actions['dispatch_stage_ids'] = ['inspect']
        result = self.invoke(actions)
        self.assertFalse(result['hold'], result.get('last_result'))
        self.assertEqual(self.queued.call_count, 2)
        original = delegation_policy.load(registry.task_dir(task_id))
        child_id = original['rework']['dispatched_task_id']
        child_run = registry.task_dir(child_id)
        child = registry.read_task(child_run)
        self.assertEqual(child['rework_of'], task_id)
        self.assertEqual(child['model_id'], self.model_id)
        self.assertEqual(child['stage_id'], first['id'])
        self.assertEqual(child['delegation']['rework_count'], delegation_policy.MAX_AUTO_REWORK)
        self.assertIn('missing decision', (child_run / 'prompt.txt').read_text(encoding='utf-8'))
        registry.patch_task(child_run, status='completed', ended_at=registry.now())
        delegation_policy.record_executor(child_run, {'completed': True})
        clock.control(self.project_text, self.goal, 'run_now')
        again = self.actions(with_plan=False)
        again['reviews'] = [self.review_action(child_id, 'return')]
        again['dispatch_stage_ids'] = ['inspect']
        blocked = self.invoke(again)
        self.assertIn('不能继续循环', blocked['last_result']['summary'])
        self.assertEqual(self.queued.call_count, 2)
        self.assertEqual(original['rework_count'], delegation_policy.MAX_AUTO_REWORK)

    def test_workflow_disabled_during_native_round_refuses_manual_routed_rework(self):
        task_id, first, second = self.first_stage()
        actions = self.actions(with_plan=False)
        actions['reviews'] = [self.review_action(task_id, 'return')]
        actions['dispatch_stage_ids'] = ['inspect']
        was_enabled = workflow_settings.snapshot()['enabled']
        def disable_before_dispatch(context, *args, **kwargs):
            result = self.fake_run(actions)(context, *args, **kwargs)
            workflow_settings.save_settings({'enabled': False})
            return result
        try:
            with patch.object(runner, 'run', side_effect=disable_before_dispatch):
                result = controller.run_once(self.project_text, self.goal, panel)
            self.assertIn('总开关', result['last_result']['summary'])
            self.assertEqual(self.queued.call_count, 1)
            review = delegation_policy.load(registry.task_dir(task_id))
            self.assertEqual(review['state'], delegation_policy.RETURNED)
            self.assertFalse(review['rework']['dispatched_task_id'])
            stage = dynamic_plan.describe_goal(self.project_text, self.goal)['stages'][0]
            self.assertEqual(stage['task_ids'], [task_id])
        finally:
            workflow_settings.save_settings({'enabled': was_enabled})

    def test_queued_rework_cannot_spawn_after_workflow_is_disabled(self):
        task_id, first, second = self.first_stage()
        actions = self.actions(with_plan=False)
        actions['reviews'] = [self.review_action(task_id, 'return')]
        actions['dispatch_stage_ids'] = ['inspect']
        result = self.invoke(actions)
        self.assertFalse(result['hold'], result.get('last_result'))
        review = delegation_policy.load(registry.task_dir(task_id))
        child_id = review['rework']['dispatched_task_id']
        child_run = registry.task_dir(child_id)
        queued = registry.read_task(child_run)
        self.assertEqual(queued['route']['mode'], 'manual')
        self.assertTrue(queued['automatic_goal'])
        was_enabled = workflow_settings.snapshot()['enabled']
        try:
            workflow_settings.save_settings({'enabled': False})
            with patch.object(panel.subprocess, 'Popen') as spawn:
                callback, queued_id = self.queued.call_args.args
                self.assertEqual(queued_id, child_id)
                callback(queued_id)
            spawn.assert_not_called()
            stopped = registry.read_task(child_run)
            self.assertEqual(stopped['status'], 'recovery_pending')
            self.assertEqual(stopped['recovery']['kind'], 'queue_blocked')
            self.assertIn('总开关', stopped['recovery']['reason'])
            self.assertEqual(self.queued.call_count, 2)
        finally:
            workflow_settings.save_settings({'enabled': was_enabled})

    def test_project_authority_delivers_ai_selected_new_file_through_actual_cli_copy(self) -> None:
        dynamic_plan.configure_goal(self.project_text, self.goal,
            {'criteria': ['Create the requested new output'], 'file_scope': 'project', 'allowed_files': []})
        actions = self.actions()
        actions['plan']['stages'][0].update({'kind': 'edit', 'files': ['generated.py'],
            'acceptance': ['The new output contains the requested implementation'],
            'candidates': [self.model_id], 'fixed_model': self.model_id})
        actions['dispatch_stage_ids'] = ['inspect']
        planned = self.invoke(actions)
        self.assertFalse(planned['hold'], planned.get('last_result'))
        stage = dynamic_plan.describe_goal(self.project_text, self.goal)['stages'][0]
        task_id = stage['active_task_id']
        run = registry.task_dir(task_id)
        queued = registry.read_task(run)
        script = ("import sys;from pathlib import Path;sys.stdin.read();"
                  "Path('generated.py').write_text('VALUE = 42\\n');print('Created requested output')")
        with patch.object(cli_worker, 'command', return_value=[sys.executable, '-c', script]):
            cli_worker.run_task(run, queued, registry.get_model(self.model_id))
        completed = registry.read_task(run)
        self.assertEqual(completed['status'], 'completed')
        self.assertEqual((self.project / 'generated.py').read_text(), 'VALUE = 42\n')
        self.assertEqual(completed['changed_selected_files'], ['generated.py'])
        self.assertEqual(queued['files'], ['generated.py'])
        self.assertEqual(dynamic_plan.describe_goal(self.project_text, self.goal)['allowed_files'], [])

    def test_created_task_recovers_lost_dispatch_response_in_actual_graph(self) -> None:
        actions = self.actions()
        actions['plan']['stages'][0].update({'candidates': [self.model_id], 'fixed_model': self.model_id})
        actions['dispatch_stage_ids'] = ['inspect']
        actual = panel.dispatch_stage
        def lose_response(*args, **kwargs):
            actual(*args, **kwargs)
            raise OSError('Fixture lost the dispatch response after durable task creation')
        with patch.object(panel, 'dispatch_stage', side_effect=lose_response) as dispatch:
            result = self.invoke(actions)
        self.assertFalse(result['hold'], result.get('last_result'))
        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(self.queued.call_count, 1)
        task_id = result['last_result']['dispatched_task_ids'][0]
        self.assertTrue(registry.read_task(registry.task_dir(task_id))['goal_dispatch_request_id'])

    def test_independent_review_recovers_lost_response_without_reaccepting(self) -> None:
        task_id, first, second = self.first_stage()
        actions = self.actions(with_plan=False)
        actions['reviews'] = [self.review_action(task_id)]
        actual = panel.update_review
        def lose_response(*args, **kwargs):
            actual(*args, **kwargs)
            raise OSError('Fixture lost the review response after independent acceptance')
        with patch.object(panel, 'update_review', side_effect=lose_response) as review:
            result = self.invoke(actions)
        self.assertFalse(result['hold'], result.get('last_result'))
        self.assertEqual(review.call_count, 1)
        decisions = delegation_policy.load(registry.task_dir(task_id))['decisions']
        self.assertEqual(len([row for row in decisions if row['action'] == 'accept']), 1)
        self.assertTrue(task_handover.verify(registry.task_dir(task_id))['valid'])

    def test_native_exit_does_not_release_unresolved_action_ownership(self) -> None:
        actions = self.actions()
        actions['plan']['stages'][0].update({'candidates': [self.model_id], 'fixed_model': self.model_id})
        actions['dispatch_stage_ids'] = ['inspect']
        actual = panel.dispatch_stage
        def unattributed_response(*args, **kwargs):
            result = actual(*args, **kwargs)
            registry.patch_task(registry.task_dir(result['task']['id']), goal_round_id='unattributed-fixture')
            raise OSError('Fixture cannot attribute the partially applied dispatch')
        def exited_native(*args, **kwargs):
            result = self.fake_run(actions)(*args, **kwargs)
            kwargs['on_exited']()
            return result
        with patch.object(panel, 'dispatch_stage', side_effect=unattributed_response), \
             patch.object(runner, 'run', side_effect=exited_native):
            result = controller.run_once(self.project_text, self.goal, panel)
        self.assertEqual(result['last_result']['status'], 'unknown')
        self.assertEqual(result['active_intent']['status'], 'unknown')
        self.assertEqual(self.queued.call_count, 1)


if __name__ == '__main__':
    unittest.main()
