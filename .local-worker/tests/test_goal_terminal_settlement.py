"""Crash recovery closes completed bookkeeping without starting any model work."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import continuation_clock as clock
import goal_brain_runner as runner
import goal_controller as controller
import stage_store
import test_goal_controller as fixtures


class GoalTerminalSettlementTests(unittest.TestCase):
    actions = fixtures.GoalControllerTests.actions
    accepted_stage = fixtures.GoalControllerTests.accepted_stage

    def setUp(self):
        fixtures.GoalControllerTests.setUp(self)
        self.accepted_stage()
        self.native = self.enterContext(patch.object(runner, 'run'))

    def completed_goal(self):
        stage_store.set_goal_status(self.goal, stage_store.GOAL_COMPLETED,
                                    actor='main', project=self.project_text)

    def test_crash_after_goal_commit_reconciles_exited_round_and_closes_without_generation(self):
        claim = clock.claim_due(self.project_text, self.goal, 'crashed-owner')
        clock.record_runner(self.project_text, self.goal, claim['id'], claim['authority_epoch'],
                            999999999, 1234.0, native_session_id='original-native-session')
        self.completed_goal()
        before = clock.snapshot(self.project_text, self.goal)
        self.assertTrue(before['active_intent'])
        result = controller.control_goal(self.project_text, self.goal, 'settle_terminal')
        self.assertTrue(result['closed'])
        self.assertFalse(result['enabled'])
        self.assertFalse(result['active_intent'])
        self.assertEqual(result['clock_id'], before['clock_id'])
        self.assertEqual(result['native_session_id'], 'original-native-session')
        self.assertEqual(result['terminal_settlement']['state'], 'closed')
        self.native.assert_not_called()
        self.panel.dispatch_stage.assert_not_called()

    def test_crash_after_round_receipt_closes_completed_goal_without_inspecting_or_generating(self):
        claim = clock.claim_due(self.project_text, self.goal, 'crashed-owner')
        self.completed_goal()
        clock.finish_round(self.project_text, self.goal, claim['id'], claim['authority_epoch'],
                           {'status': 'completed', 'summary': 'Completed receipt persisted'})
        with patch.object(runner, 'inspect_owned') as inspect:
            result = controller.settle_terminal(self.project_text, self.goal)
        self.assertTrue(result['closed'])
        inspect.assert_not_called()
        self.native.assert_not_called()

    def test_live_original_process_keeps_terminal_goal_held_and_user_pause_unchanged(self):
        claim = clock.claim_due(self.project_text, self.goal, 'live-owner')
        process = runner.identity(os.getpid())
        self.assertIsNotNone(process)
        clock.record_runner(self.project_text, self.goal, claim['id'], claim['authority_epoch'],
                            process.pid, process.birth)
        self.completed_goal()
        clock.control(self.project_text, self.goal, 'pause')
        with patch.object(runner, 'stop_owned') as stop:
            result = controller.settle_terminal(self.project_text, self.goal)
        self.assertFalse(result['closed'])
        self.assertEqual(result['user_intent'], 'USER_PAUSE')
        self.assertFalse(result['enabled'])
        self.assertEqual(result['hold']['kind'], 'unknown')
        self.assertEqual(result['terminal_settlement']['state'], 'waiting_process')
        self.assertEqual(result['active_intent']['id'], claim['id'])
        stop.assert_not_called()
        self.native.assert_not_called()

    def test_missing_process_identity_stays_unknown_instead_of_restarting(self):
        claim = clock.claim_due(self.project_text, self.goal, 'ambiguous-owner')
        self.completed_goal()
        result = controller.settle_terminal(self.project_text, self.goal)
        self.assertFalse(result['closed'])
        self.assertEqual(result['active_intent']['id'], claim['id'])
        self.assertEqual(result['process_inspection']['state'], 'unknown')
        self.assertEqual(result['hold']['kind'], 'unknown')
        self.native.assert_not_called()

    def test_pending_obligations_prevent_terminal_close_without_synthesized_fulfillment(self):
        clock.configure_clock(self.project_text, self.goal, enabled=True,
                              obligations=[{'id': 'archive', 'title': '归档最终报告'}])
        self.completed_goal()
        result = controller.settle_terminal(self.project_text, self.goal)
        self.assertFalse(result['closed'])
        self.assertEqual(result['terminal_settlement']['state'], 'pending_obligations')
        self.assertEqual(result['obligations'][0]['status'], 'pending')
        self.assertEqual(result['obligations'][0]['evidence'], '')
        self.native.assert_not_called()

    def test_driver_scans_completed_unclosed_goal_only_for_settlement(self):
        self.completed_goal()
        controller._STOP.clear()
        self.addCleanup(controller._STOP.clear)
        with patch.object(controller._STOP, 'wait', side_effect=lambda seconds: controller._STOP.set()), \
                patch.object(controller, 'run_once') as production:
            controller._scan(self.panel)
        self.assertTrue(clock.snapshot(self.project_text, self.goal)['closed'])
        production.assert_not_called()
        self.native.assert_not_called()
        self.panel.dispatch_stage.assert_not_called()


if __name__ == '__main__':
    unittest.main()
