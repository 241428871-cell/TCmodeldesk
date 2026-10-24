"""Offline governance tests: temporary project records and no model processes."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from pydantic import JsonValue

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dynamic_plan
import continuation_clock
import engineering_policy
import human_decisions
import model_registry as registry
from plan_contracts import PlanError
import stage_store


class DynamicGovernanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        (self.project / 'a.py').write_text('print(1)\n', encoding='utf-8')
        (self.project / 'b.py').write_text('print(2)\n', encoding='utf-8')
        saved = registry.HERE, registry.REGISTRY, registry.RUNS
        self.addCleanup(self.restore_registry, saved)
        registry.HERE, registry.REGISTRY, registry.RUNS = self.root, self.root / 'models.json', self.root / 'runs'
        self.goal = stage_store.create_goal('目标', 'AI 负责拆分', str(self.project), store='project')['id']
        self.configure()

    @staticmethod
    def restore_registry(saved: tuple[Path, Path, Path]) -> None:
        registry.HERE, registry.REGISTRY, registry.RUNS = saved

    def configure(self, review_policy: str = 'exceptions') -> None:
        dynamic_plan.configure_goal(str(self.project), self.goal,
                                    {'criteria': ['行为可验证', '独立验收通过'], 'review_policy': review_policy,
                                     'allowed_files': ['a.py', 'b.py']})

    def patch(self, revision: int = 0, stages: list[dict[str, JsonValue]] | None = None, **values: str) -> dict[str, JsonValue]:
        return {'base_revision': revision, 'reason': '根据当前证据重新拆分',
                'stages': stages or [self.spec('a'), self.spec('b', ['a'])], **values}

    @staticmethod
    def spec(key: str, depends: list[str] | None = None, **values: str) -> dict[str, JsonValue]:
        return {'key': key, 'title': key, 'objective': '验证 ' + key,
                'depends_on': depends or [], 'acceptance': ['可复核证据'], **values}

    def revise(self, payload: dict[str, JsonValue] | None = None) -> dict[str, JsonValue]:
        return dynamic_plan.revise_plan(str(self.project), self.goal, payload or self.patch())

    def view(self) -> dict[str, JsonValue]:
        return dynamic_plan.describe_goal(str(self.project), self.goal)

    def decision(self, stage_ids: list[str], **values: str) -> dict[str, JsonValue]:
        return human_decisions.create_decision(str(self.project), self.goal, {
            'question': '是否按建议继续？', 'reason': '需要确认范围例外', 'options': ['同意', '调整方案'], 'approval_option': '同意',
            'recommendation': '同意', 'evidence_refs': ['task:fixture'], 'affected_stage_ids': stage_ids, **values})

    def answer(self, decision: dict[str, JsonValue], option: int = 0, actor: str = 'human') -> dict[str, JsonValue]:
        return human_decisions.answer_decision(str(self.project), decision['id'], {
            'subject_hash': decision['subject_hash'],
            'authority_epoch': decision['fingerprint']['authority_epoch'],
            'option_id': decision['options'][option]['id']}, actor=actor)

    def accept_all(self) -> None:
        with stage_store.connection(str(self.project)) as conn:
            conn.execute("UPDATE stages SET status='accepted' WHERE goal_id=?", (self.goal,))

    def test_ai_owns_decomposition_and_defaults_to_read_only(self) -> None:
        result = self.revise()
        self.assertEqual(result['plan_revision'], 1)
        self.assertEqual([row['kind'] for row in result['stages']], ['chat', 'chat'])
        self.assertEqual(result['stages'][1]['depends_on'], [result['stages'][0]['id']])
        self.assertEqual(result['history'][0]['changes']['added'], [row['id'] for row in result['stages']])

    def test_human_criteria_edit_rejects_stale_or_terminal_goal(self) -> None:
        revision = self.view()['contract_revision']
        dynamic_plan.configure_goal(str(self.project), self.goal,
            {'criteria': ['新准则'], 'base_revision': revision})
        with self.assertRaisesRegex(PlanError, '准则已更新'):
            dynamic_plan.configure_goal(str(self.project), self.goal,
                {'criteria': ['旧窗口覆盖'], 'base_revision': revision})
        stage_store.set_goal_status(self.goal, 'cancelled', project=str(self.project))
        with self.assertRaisesRegex(PlanError, '不能改写准则'):
            self.configure()

    def test_stale_plan_conflict_is_atomic(self) -> None:
        self.revise()
        with self.assertRaisesRegex(PlanError, '版本已变化'):
            self.revise(self.patch(0, [self.spec('uncommitted')]))
        self.assertEqual(self.view()['plan_revision'], 1)
        self.assertEqual(len(self.view()['stages']), 2)

    def test_cycle_and_foreign_dependency_rejected_without_partial_rows(self) -> None:
        for stages in ([self.spec('a', ['b']), self.spec('b', ['a'])], [self.spec('a', ['outside'])]):
            with self.assertRaises(PlanError):
                self.revise(self.patch(stages=stages))
            self.assertEqual(self.view()['stages'], [])
            self.assertEqual(self.view()['plan_revision'], 0)

    def test_human_criteria_cannot_be_changed_by_ai_patch(self) -> None:
        with self.assertRaises(ValueError):
            self.revise({**self.patch(), 'criteria': ['AI 自己降低准则']})
        with self.assertRaisesRegex(PlanError, '只能由人类'):
            dynamic_plan.configure_goal(str(self.project), self.goal, {'criteria': ['降低准则']}, actor='brain')
        self.assertEqual(self.view()['criteria'], ['行为可验证', '独立验收通过'])

    def test_ai_cannot_expand_file_scope(self) -> None:
        for values in ({'kind': 'edit'}, {'kind': 'edit', 'files': ['secret.py']},
                       {'kind': 'edit', 'files': ['../outside.py']}):
            with self.assertRaises(PlanError):
                self.revise(self.patch(stages=[self.spec('edit', **values)]))
        result = self.revise(self.patch(stages=[self.spec('edit', kind='edit', files=['a.py'])]))
        self.assertEqual(result['stages'][0]['files'], ['a.py'])

    def test_completed_history_preserved_and_pending_steps_can_be_superseded(self) -> None:
        result = self.revise()
        first = result['stages'][0]
        with stage_store.connection(str(self.project)) as conn:
            conn.execute("UPDATE stages SET status='accepted' WHERE id=?", (first['id'],))
        changed = self.revise(self.patch(1, [self.spec('c', [first['id']])]))
        by_key = {row['key']: row for row in changed['stages']}
        self.assertEqual(by_key['a']['status'], 'accepted')
        self.assertEqual(by_key['b']['status'], 'cancelled')
        self.assertEqual(by_key['b']['data']['dynamic_plan']['superseded_by'], 2)
        with self.assertRaisesRegex(PlanError, '不能改写'):
            self.revise(self.patch(2, [self.spec('a', objective='改写历史'), self.spec('c', ['a'])]))

    def test_revision_records_changed_dependencies(self) -> None:
        result = self.revise()
        result = self.revise(self.patch(1, [self.spec('a'), self.spec('b')]))
        changes = result['history'][-1]['changes']['dependency_changes']
        self.assertEqual(changes[0]['before'], [result['stages'][0]['id']])
        self.assertEqual(changes[0]['after'], [])

    def test_stop_and_old_epoch_prevent_ai_revision(self) -> None:
        continuation_clock.configure_clock(str(self.project), self.goal)
        continuation_clock.control(str(self.project), self.goal, 'stop')
        with self.assertRaisesRegex(PlanError, '已停止'):
            self.revise()
        continuation_clock.control(str(self.project), self.goal, 'resume')
        with self.assertRaisesRegex(ValueError, '代次已变化'):
            self.revise({**self.patch(), 'authority_epoch': 0})

    def test_same_blocker_deduplicates_and_only_affected_branch_waits(self) -> None:
        result = self.revise(self.patch(stages=[self.spec('a'), self.spec('b')]))
        first, second = result['stages']
        decision = self.decision([first['id']])
        self.assertEqual(self.decision([first['id']])['id'], decision['id'])
        self.assertFalse(stage_store.readiness(first['id'], str(self.project))['ready'])
        self.assertTrue(stage_store.readiness(second['id'], str(self.project))['ready'])
        with self.assertRaisesRegex(ValueError, '审核'):
            stage_store.claim_dispatch(first['id'], 'forbidden', project=str(self.project))
        self.answer(decision)
        self.assertTrue(stage_store.readiness(first['id'], str(self.project))['ready'])

    def test_declined_exception_requires_ai_replan(self) -> None:
        result = self.revise(self.patch(stages=[self.spec('a')]))
        stage_id = result['stages'][0]['id']
        self.answer(self.decision([stage_id]), option=1)
        self.assertFalse(stage_store.readiness(stage_id, str(self.project))['ready'])
        self.revise(self.patch(1, [self.spec('a', objective='按人类意见调整')]))
        self.assertTrue(stage_store.readiness(stage_id, str(self.project))['ready'])

    def test_stale_decision_rejects_plan_policy_epoch_or_artifact_change(self) -> None:
        result = self.revise()
        decision = self.decision([result['stages'][0]['id']])
        continuation_clock.configure_clock(str(self.project), self.goal)
        with self.assertRaisesRegex(PlanError, '已变化'):
            self.answer(decision)
        (self.project / 'a.py').write_text('print(99)\n', encoding='utf-8')
        with self.assertRaisesRegex(PlanError, '已变化'):
            self.answer(decision)
        refreshed = self.decision([result['stages'][0]['id']])
        self.assertNotEqual(refreshed['id'], decision['id'])
        engineering_policy.save_policy({'global_enabled': True})
        with self.assertRaisesRegex(PlanError, '已变化'):
            self.answer(refreshed)

    def test_only_human_route_can_answer(self) -> None:
        result = self.revise()
        with self.assertRaisesRegex(PlanError, '不能代替人类'):
            self.answer(self.decision([result['stages'][0]['id']]), actor='brain')

    def test_final_acceptance_cannot_be_self_approved_and_expires_on_content_change(self) -> None:
        self.configure('final')
        self.revise()
        self.accept_all()
        with self.assertRaisesRegex(ValueError, '人类审核'):
            stage_store.set_goal_status(self.goal, 'completed', project=str(self.project))
        decision = human_decisions.request_final_review(str(self.project), self.goal, ['test:independent'])
        self.answer(decision)
        self.assertTrue(human_decisions.check_completion(str(self.project), self.goal)['approved'])
        (self.project / 'a.py').write_text('changed after acceptance\n', encoding='utf-8')
        with self.assertRaisesRegex(PlanError, '人类审核'):
            human_decisions.check_completion(str(self.project), self.goal)

    def test_denied_branch_does_not_unblock_from_unrelated_revision(self) -> None:
        result = self.revise(self.patch(stages=[self.spec('a'), self.spec('b')]))
        stage_id = result['stages'][0]['id']
        self.answer(self.decision([stage_id]), option=1)
        self.revise(self.patch(1, [self.spec('a'), self.spec('b', objective='无关修改')]))
        self.assertFalse(stage_store.readiness(stage_id, str(self.project))['ready'])

    def test_goal_snapshot_and_completion_obey_pending_exception(self) -> None:
        result = self.revise(self.patch(stages=[self.spec('a')]))
        stage_id = result['stages'][0]['id']
        decision = self.decision([stage_id])
        snapshot = stage_store.get_goal(self.goal, str(self.project))
        self.assertFalse(snapshot['stages'][0]['readiness']['ready'])
        self.accept_all()
        with self.assertRaisesRegex(ValueError, '人类审核'):
            stage_store.set_goal_status(self.goal, 'completed', project=str(self.project))
        with self.assertRaisesRegex(PlanError, '已变化'):
            self.answer(decision)

    def test_plan_revision_invalidates_legacy_whole_goal_human_subject(self) -> None:
        from review_fixture import legacy
        result = self.revise()
        decision = self.decision([result['stages'][0]['id']])
        previous = legacy(str(self.project), self.goal, decision['id'])
        decision = {**decision, 'subject_hash': previous.subject_hash, 'fingerprint': previous.fingerprint}
        self.revise(self.patch(1, [self.spec('a'), self.spec('b', objective='新目标')]))
        with self.assertRaisesRegex(PlanError, '已变化'):
            self.answer(decision)

    def test_real_clock_epoch_plan_and_decision_share_one_transaction_authority(self) -> None:
        continuation_clock.configure_clock(str(self.project), self.goal)
        state = continuation_clock.control(str(self.project), self.goal, 'start')
        result = self.revise({**self.patch(), 'authority_epoch': state['authority_epoch']})
        decision = self.decision([result['stages'][0]['id']])
        self.assertEqual(decision['fingerprint']['authority_epoch'], state['authority_epoch'])
        self.answer(decision)

    def test_project_records_are_not_visible_in_another_project(self) -> None:
        self.revise()
        other = self.root / 'other'
        other.mkdir()
        with self.assertRaisesRegex(PlanError, '当前项目'):
            dynamic_plan.describe_goal(str(other), self.goal)
        self.assertEqual(human_decisions.list_decisions(str(other)), [])
        self.assertFalse((other / '.model-desk').exists())


if __name__ == '__main__':
    unittest.main()
