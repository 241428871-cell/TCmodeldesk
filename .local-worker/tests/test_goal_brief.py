from pathlib import Path
from unittest.mock import Mock

import continuation_clock
import dynamic_plan
import governance_api
import model_registry as registry
import pytest
import stage_store
from plan_contracts import PlanError


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    worker = tmp_path / 'worker'
    worker.mkdir()
    for name, value in (('HERE', worker), ('REGISTRY', worker / 'models.json'), ('RUNS', worker / 'runs')):
        monkeypatch.setattr(registry, name, value)
    folder = tmp_path / 'project'
    folder.mkdir()
    return str(folder)


def create(project: str, **values):
    return governance_api.post('/api/governance/goals', values,
                               {'mode': 'project', 'project': project}, Mock())


def proposal(brief=None, revision=0):
    return {'base_revision': revision, 'reason': 'Inspect requested behavior',
            **({'brief': brief} if brief is not None else {}),
            'stages': [{'key': 'inspect', 'title': 'Inspect inputs', 'objective': 'Read relevant project files',
                        'acceptance': ['Relevant inputs identified']}]}


@pytest.mark.parametrize('values', [{'title': 'Improve export'}, {'objective': 'Export portable reports'},
                                   {'criteria': ['Exported report opens offline']}])
def test_partial_input_saves_without_starting_a_brain(project, values):
    result = create(project, **values)
    assert result['plan']['completion_pending']
    assert result['plan']['criteria'] == values.get('criteria', [])
    assert result['clock']['enabled'] is False
    assert result['clock']['active_intent'] is None


def test_empty_intent_is_rejected_without_creating_goal_records(project):
    with pytest.raises(governance_api.GovernanceError):
        create(project, title=' ', objective=' ', criteria=[])
    assert not (Path(project) / '.model-desk').exists()


def test_brain_fills_missing_content_with_persistent_closeout_without_changing_authority(project):
    created = create(project, title='Improve export', file_scope='selected', allowed_files=[],
                     review_policy='final', interval_seconds=2400)
    goal_id = created['goal']['id']
    before = continuation_clock.snapshot(project, goal_id)
    plan = dynamic_plan.revise_plan(project, goal_id, proposal({
        'title': '', 'objective': 'Produce a portable report', 'criteria': ['Report opens offline'],
        'obligations': ['Archive the verification report']}))
    after = continuation_clock.snapshot(project, goal_id)
    assert stage_store.get_goal(goal_id, project)['objective'] == 'Produce a portable report'
    assert plan['criteria'] == []
    assert plan['brief']['criteria'] == ['Report opens offline']
    assert plan['completion_pending'] == []
    assert plan['allowed_files'] == [] and plan['file_scope'] == 'selected'
    assert plan['review_policy'] == 'final'
    assert plan['contract_revision'] == created['plan']['contract_revision']
    assert {key: after[key] for key in ('enabled', 'user_intent', 'authority_epoch', 'interval_seconds')} == {
        key: before[key] for key in ('enabled', 'user_intent', 'authority_epoch', 'interval_seconds')}
    assert after['obligations'][0]['status'] == 'pending'
    assert after['obligations'][0]['source'] == 'brain'


def test_brain_cannot_replace_human_text_or_inject_authorization(project):
    created = create(project, title='Human title', objective='Human result', criteria=['Human criterion'])
    goal_id = created['goal']['id']
    with pytest.raises(PlanError):
        dynamic_plan.revise_plan(project, goal_id, proposal({
            'title': 'Different goal', 'objective': '', 'criteria': [], 'obligations': []}))
    assert dynamic_plan.describe_goal(project, goal_id)['plan_revision'] == 0
    with pytest.raises(ValueError):
        dynamic_plan.revise_plan(project, goal_id, proposal({
            'title': '', 'objective': '', 'criteria': [], 'obligations': [], 'file_scope': 'project'}))


def test_invalid_plan_does_not_partially_fill_goal_or_obligations(project):
    created = create(project, title='Improve export')
    goal_id = created['goal']['id']
    patch = proposal({'title': '', 'objective': 'New result', 'criteria': ['Report opens'],
                      'obligations': ['Archive report']})
    patch['stages'][0]['depends_on'] = ['missing-stage']
    with pytest.raises(PlanError):
        dynamic_plan.revise_plan(project, goal_id, patch)
    assert stage_store.get_goal(goal_id, project)['objective'] == created['goal']['objective']
    assert continuation_clock.snapshot(project, goal_id)['obligations'] == []


def test_ai_closeout_survives_later_replan_and_human_criteria_edits(project):
    created = create(project, title='Improve export')
    goal_id = created['goal']['id']
    dynamic_plan.revise_plan(project, goal_id, proposal({
        'title': '', 'objective': 'Portable report', 'criteria': ['Report opens'],
        'obligations': ['Archive report']}))
    original = continuation_clock.snapshot(project, goal_id)['obligations']
    dynamic_plan.configure_goal(project, goal_id, {'criteria': ['Human criterion']})
    dynamic_plan.revise_plan(project, goal_id, proposal(revision=1))
    plan = dynamic_plan.describe_goal(project, goal_id)
    assert plan['criteria'] == ['Human criterion']
    assert plan['brief']['objective'] == 'Portable report'
    assert plan['brief']['criteria'] == ['Report opens']
    assert continuation_clock.snapshot(project, goal_id)['obligations'] == original


def test_legacy_configured_goals_keep_their_original_plan_protocol(project):
    goal = stage_store.create_goal('Existing goal', project=project, store='project')
    dynamic_plan.configure_goal(project, goal['id'], {'criteria': ['Existing criterion']})
    plan = dynamic_plan.revise_plan(project, goal['id'], proposal())
    assert plan['plan_revision'] == 1
    assert plan['brief'] is None
