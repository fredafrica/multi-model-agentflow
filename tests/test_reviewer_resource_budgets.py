"""Budget regressions using deterministic, configuration-consuming process doubles."""
from dataclasses import replace
import json
import tempfile
import unittest
import subprocess
from copy import deepcopy
from unittest import mock

from agentflow.adapters import InvocationIncompleteError, ProviderNotConfiguredError
from agentflow.authorization import issue_authorization, validate_authorization
from agentflow.opencode_adapter import OpenCodeAdapter, LocalOllamaReviewerAdapter, RemoteOpenCodeReviewerAdapter
from agentflow.serialization import canonical_json, load_plan_json, plan_hash
from test_core import make_plan, make_task
from test_remote_reviewer import review_request, _CompletedProcess
from test_local_ollama_reviewer import ollama_review_request, make_run_stub
from agentflow import contracts
from resource_budget_fixtures import budget_metadata, capability, budgeted_plan
from agentflow.resource_budgets import invocation_budgets, freeze_capability
from agentflow.serialization import to_primitive, model_record_from_mapping
from agentflow.opencode_adapter import RemoteOpenCodeWorkerAdapter
from agentflow.database import Database
from agentflow.fake_adapter import FakeAdapter
from agentflow.adapters import AdapterRouter
from agentflow.runner import Runner
from agentflow.states import RunState
import test_runner as runner_fixtures
from test_runner import make_task as runner_task, make_plan as runner_plan, approved_response


def config_run(request, override=None, base_output=65536, api_id=None):
    def stub(args, **kwargs):
        if args[1] == '--version':
            return subprocess.CompletedProcess(args, 0, stdout='1.18.29\n')
        if args[1] == 'models':
            return subprocess.CompletedProcess(args, 0, stdout=f'{request.model.provider}/{request.model.model_id}\n')
        if args[1] != 'debug':
            raise AssertionError(args)
        base = {'provider': {request.model.provider: {'npm': '@ai-sdk/openai-compatible',
                'options': {'baseURL': 'http://127.0.0.1:11434/v1'},
                'models': {request.model.model_id: {'name': 'fictional', 'id': api_id or request.model.model_id,
                    'options': {'temperature': 0.1},
                    'limit': {'context': 262144, 'output': base_output, 'input': 200000}}}}}}
        overlay = json.loads((kwargs.get('env') or {}).get('OPENCODE_CONFIG_CONTENT', '{}'))
        def merge(a, b):
            for key, value in b.items():
                if isinstance(value, dict) and isinstance(a.get(key), dict):
                    merge(a[key], value)
                else:
                    a[key] = deepcopy(value)
        merge(base, overlay)
        if override and (kwargs.get('env') or {}).get('OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX'):
            override(base)
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(base))
    return stub


def step_process(required, captured):
    def popen(command, **kwargs):
        config = json.loads(kwargs['env']['OPENCODE_CONFIG_CONTENT'])
        agent = config['agent'][command[command.index('--agent') + 1]]
        steps = agent['steps']
        captured.append(config)
        events = [json.dumps({'type': 'step_finish', 'sessionID': 'budget-session',
                             'part': {'tokens': {'input': 5, 'output': 2}, 'cost': 0.01}})
                  for _ in range(min(required, steps))]
        text = ('Maximum steps for this agent have been reached.' if required > steps
                else '{"approved":true,"findings":[]}')
        events.append(json.dumps({'type': 'text', 'sessionID': 'budget-session', 'part': {'type': 'text', 'text': text}}))
        return _CompletedProcess('\n'.join(events))
    return popen


class ReviewerStepBudgetTests(unittest.TestCase):
    def test_three_round_review_and_single_round_completion(self):
        for local in (False, True):
            for role in ('review', 'rereview'):
                for steps, required in ((2, 3), (8, 3), (12, 3), (8, 1)):
                    with self.subTest(local=local, role=role, steps=steps, required=required):
                        request = ollama_review_request(role=role) if local else review_request(role=role)
                        request = replace(request, metadata={**request.metadata, 'review_max_steps': steps})
                        adapter = LocalOllamaReviewerAdapter() if local else RemoteOpenCodeReviewerAdapter(request.model.provider)
                        captured = []
                        discovery = config_run(request)
                        with mock.patch('subprocess.run', side_effect=discovery), mock.patch('subprocess.Popen', side_effect=step_process(required, captured)):
                            if steps < required:
                                with self.assertRaises(InvocationIncompleteError) as failure:
                                    adapter.invoke(request)
                                self.assertEqual(4, failure.exception.result.output_tokens)
                            else:
                                result = adapter.invoke(request)
                                self.assertEqual(required * 2, result.output_tokens)
                                self.assertTrue(json.loads(result.output)['approved'])
                        self.assertEqual(steps, next(iter(captured[0]['agent'].values()))['steps'])

    def test_contract_roundtrip_hash_and_strict_validation(self):
        plan = make_plan()
        for steps in (2, 8, 12, 32):
            changed = replace(plan, tasks=(replace(plan.tasks[0], review_max_steps=steps),))
            self.assertEqual(steps, load_plan_json(canonical_json(changed)).tasks[0].review_max_steps)
            if steps != 8:
                self.assertNotEqual(plan_hash(plan), plan_hash(changed))
                with self.assertRaises(ValueError):
                    validate_authorization(issue_authorization(plan), changed)
        for value in (True, 8.0, '8', None, 0, -1, 1, 33):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(make_task(), review_max_steps=value)

    def test_invalid_request_budget_stops_before_process(self):
        for value in (True, 8.0, '8', None, 0, -1, 1, 33):
            for adapter, request in ((LocalOllamaReviewerAdapter(), ollama_review_request()),
                                     (RemoteOpenCodeReviewerAdapter('review-provider'), review_request())):
                with self.subTest(value=value), mock.patch('subprocess.Popen') as process, mock.patch('subprocess.run'):
                    with self.assertRaises(ValueError):
                        adapter.invoke(replace(request, metadata={'review_max_steps': value}))
                    process.assert_not_called()

    def test_lmstudio_review_uses_review_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            request = review_request()
            request = replace(request, model=replace(request.model, provider='lmstudio', is_local=True),
                              metadata={'worktree': directory, 'review_max_steps': 12, 'implementation_max_steps': 2})
            request = replace(request, metadata={**request.metadata, **budget_metadata(request.model, request.role)})
            captured = []
            with mock.patch('subprocess.Popen', side_effect=step_process(3, captured)), mock.patch('subprocess.run', side_effect=config_run(request)):
                result = OpenCodeAdapter().invoke(request)
            self.assertTrue(json.loads(result.output)['approved'])
            self.assertEqual(12, captured[0]['agent']['agentflow-sandbox']['steps'])


class OutputBudgetContractTests(unittest.TestCase):
    def test_capability_and_role_authorization_are_frozen_and_independent(self):
        self.assertTrue(hasattr(contracts, 'ModelCapabilitySnapshot'), 'missing frozen capability contract')
        from agentflow.resource_budgets import invocation_budgets
        model = make_task().implementation_model
        snapshot = contracts.ModelCapabilitySnapshot(model, 262144, 65536, 'registry_config', 'fixture-v1')
        task = replace(make_task(), implementation_max_output_tokens=16000, review_max_output_tokens=100000)
        review_snapshot = capability(task.review_model)
        plan = replace(make_plan(task=task), model_capabilities=(snapshot, review_snapshot))
        budget = invocation_budgets(plan, task, model, 'implementation')
        self.assertEqual(16000, budget['effective_max_output_tokens'])
        self.assertEqual(262144, budget['capability']['context_length'])
        self.assertEqual(65536, invocation_budgets(plan, task, model, 'review')['effective_max_output_tokens'])
        self.assertEqual(plan, load_plan_json(canonical_json(plan)))
        bigger = replace(snapshot, max_output_tokens=384000)
        changed = replace(plan, model_capabilities=(bigger, review_snapshot))
        self.assertEqual(100000, invocation_budgets(changed, task, model, 'review')['effective_max_output_tokens'])
        with self.assertRaises(ValueError):
            validate_authorization(issue_authorization(plan), changed)

    def test_unknown_output_cannot_launch_real_adapter(self):
        with mock.patch('subprocess.run') as discover, mock.patch('subprocess.Popen') as process:
            with self.assertRaisesRegex(ValueError, 'output|capability'):
                RemoteOpenCodeReviewerAdapter('review-provider').invoke(replace(review_request(), metadata={}))
            process.assert_not_called()

    def test_role_fields_and_capabilities_reject_nonintegers(self):
        for value in (True, 16000.0, '16000', None, 0, -1, 2**31):
            for field in ('review_max_output_tokens', 'implementation_max_output_tokens'):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    replace(make_task(), **{field: value})
            for field in ('context_length', 'max_output_tokens'):
                if field == 'context_length' and value is None:
                    continue
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    replace(capability(make_task().implementation_model), **{field: value})

    def test_record_roundtrip_and_unknown_source_fail_closed(self):
        ref = make_task().implementation_model
        record = contracts.ModelRecord(ref, False, 262144, False, None, None, None,
            contracts.TrustLevel.UNVERIFIED, contracts.BusinessImportance.NORMAL,
            max_output_tokens=65536, capability_source='registry_config', capability_source_version='fixture')
        self.assertEqual(record, model_record_from_mapping(to_primitive(record)))
        self.assertEqual(65536, freeze_capability(record).max_output_tokens)
        self.assertFalse(record.available)
        for changed in (replace(record, max_output_tokens=None), replace(record, capability_source=None, capability_source_version=None)):
            with self.assertRaises(ValueError):
                freeze_capability(changed)
        for source in ('model_said_so', '', None):
            with self.assertRaises(ValueError):
                replace(capability(ref), source=source)

    def test_fallback_has_own_snapshot_and_context(self):
        task = make_task()
        fallback = replace(task.implementation_model, model_id='fallback')
        task = replace(task, fallback_model=fallback)
        plan = replace(make_plan(task=task), model_capabilities=(capability(task.implementation_model),
            contracts.ModelCapabilitySnapshot(fallback, 32768, 4096, 'conservative_fallback', 'fixture-policy-v1')))
        actual = invocation_budgets(plan, task, fallback, 'revision')
        self.assertEqual(4096, actual['effective_max_output_tokens'])
        self.assertEqual(32768, actual['capability']['context_length'])
        self.assertNotEqual(actual['capability_id'], invocation_budgets(plan, task, task.implementation_model, 'revision')['capability_id'])

    def test_each_budget_changes_authorization_and_db_roundtrip(self):
        plan = budgeted_plan(make_plan())
        auth = issue_authorization(plan)
        for field, value in (('review_max_steps', 12), ('review_max_output_tokens', 17000), ('implementation_max_output_tokens', 18000)):
            changed = replace(plan, tasks=(replace(plan.tasks[0], **{field: value}),))
            with self.assertRaises(ValueError):
                validate_authorization(auth, changed)
        with tempfile.TemporaryDirectory() as root:
            db = Database(root + '/fixture.db')
            db.initialize()
            db.save_plan(plan)
            self.assertEqual(plan, db.load_plan(plan.plan_id, plan.version))
            db.close()


class OutputBudgetAdapterTests(unittest.TestCase):
    def test_newer_compatible_patch_version_runs_after_runtime_checks_pass(self):
        request = review_request()
        for version in ('1.18.30', '1.18.99'):
            def run(args, **kwargs):
                if args[1] == '--version':
                    return subprocess.CompletedProcess(args, 0, stdout=version + '\n')
                return config_run(request)(args, **kwargs)
            captured = []
            with self.subTest(version=version), mock.patch(
                'subprocess.run', side_effect=run
            ), mock.patch(
                'subprocess.Popen', side_effect=step_process(1, captured)
            ):
                result = RemoteOpenCodeReviewerAdapter(request.model.provider).invoke(request)
                self.assertTrue(json.loads(result.output)['approved'])
                self.assertEqual(1, len(captured))

    def test_incompatible_opencode_version_stops_before_inference(self):
        request = review_request()
        for version in ('1.18.28', '1.19.0', '2.0.0', '', '1.18.30-custom'):
            def run(args, **kwargs):
                if args[1] == '--version':
                    return subprocess.CompletedProcess(args, 0, stdout=version)
                return config_run(request)(args, **kwargs)
            with self.subTest(version=version), mock.patch('subprocess.run', side_effect=run), mock.patch('subprocess.Popen') as process:
                with self.assertRaises(ProviderNotConfiguredError):
                    RemoteOpenCodeReviewerAdapter(request.model.provider).invoke(request)
                process.assert_not_called()

    def test_unknown_transport_and_hidden_output_override_are_rejected(self):
        request = review_request()
        for mutation in ('transport', 'override', 'alias'):
            def run(args, **kwargs):
                result = config_run(request)(args, **kwargs)
                if args[1] == 'debug':
                    data = json.loads(result.stdout)
                    provider = data['provider'][request.model.provider]
                    if mutation == 'transport':
                        provider['npm'] = 'unverified-transport'
                    elif mutation == 'override':
                        provider['options']['max_completion_tokens'] = 99999
                    else:
                        provider['models'][request.model.model_id]['id'] = 'changed-alias'
                    result.stdout = json.dumps(data)
                return result
            with self.subTest(mutation=mutation), mock.patch('subprocess.run', side_effect=run), mock.patch('subprocess.Popen') as process:
                with self.assertRaises(ProviderNotConfiguredError):
                    RemoteOpenCodeReviewerAdapter(request.model.provider).invoke(request)
                process.assert_not_called()

    def test_discovery_rejects_boolean_and_float_context(self):
        for value in (True, 262144.0, '262144', 0):
            payload = [{'type': 'llm', 'identifier': 'fixture', 'modelKey': 'fixture', 'contextLength': value}]
            with self.subTest(value=value), mock.patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, stdout=json.dumps(payload))):
                with self.assertRaises(ValueError):
                    OpenCodeAdapter().discover()

    def requests(self, directory):
        for role in ('review', 'rereview'):
            for provider in ('review-provider', 'ollama', 'lmstudio'):
                request = ollama_review_request(role=role) if provider == 'ollama' else review_request(role=role)
                if provider == 'lmstudio':
                    request = replace(request, model=replace(request.model, provider='lmstudio', is_local=True))
                yield request, (LocalOllamaReviewerAdapter() if provider == 'ollama' else
                                OpenCodeAdapter() if provider == 'lmstudio' else RemoteOpenCodeReviewerAdapter(provider))
        for role in ('implementation', 'revision'):
            for provider in ('worker-provider', 'lmstudio'):
                request = replace(review_request(), role=role, read_only=False,
                    model=contracts.ModelRef(provider, 'worker-model', '1', 'fixture', provider == 'lmstudio'))
                yield request, OpenCodeAdapter() if provider == 'lmstudio' else RemoteOpenCodeWorkerAdapter(provider)

    def test_all_paths_preserve_limits_alias_and_effective_runtime_ceiling(self):
        with tempfile.TemporaryDirectory() as directory:
            for original, adapter in self.requests(directory):
                for authorized, cap, expected in ((16000, 65536, 16000), (100000, 65536, 65536), (384000, 384000, 384000)):
                    with self.subTest(provider=original.model.provider, role=original.role, authorized=authorized):
                        task = replace(make_task(), implementation_model=original.model, review_model=original.model,
                            review_max_steps=12, review_max_output_tokens=authorized, implementation_max_output_tokens=authorized)
                        plan = replace(make_plan(task=task), model_capabilities=(replace(capability(original.model), max_output_tokens=cap, api_model_id='wire-alias'),))
                        request = replace(original, metadata={'worktree': directory, 'review_max_steps': 12,
                            'resource_budgets': invocation_budgets(plan, task, original.model, original.role)})
                        captured = []
                        def popen(command, **kwargs):
                            config = json.loads(kwargs['env']['OPENCODE_CONFIG_CONTENT'])
                            model = config['provider'][request.model.provider]['models'][request.model.model_id]
                            # Transport double consumes both knobs; this is not a real model claim.
                            generated_limit = min(model['limit']['output'], int(kwargs['env']['OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX']))
                            self.assertEqual(expected, generated_limit)
                            self.assertEqual(262144, model['limit']['context'])
                            self.assertEqual(200000, model['limit']['input'])
                            self.assertEqual('wire-alias', model['id'])
                            self.assertEqual(0.1, model['options']['temperature'])
                            return step_process(1, captured)(command, **kwargs)
                        with mock.patch('subprocess.run', side_effect=config_run(request, base_output=cap, api_id='wire-alias')), mock.patch('subprocess.Popen', side_effect=popen):
                            adapter.invoke(request)
                        self.assertEqual(1, len(captured))

    def test_managed_output_steps_endpoint_permission_overrides_stop_before_popen(self):
        for provider in ('review-provider', 'ollama'):
            original = review_request() if provider == 'review-provider' else ollama_review_request()
            request = replace(original, metadata={**original.metadata, 'review_max_steps': 12})
            def model(config):
                return config['provider'][provider]['models'][request.model.model_id]
            overrides = [lambda c: model(c)['limit'].update(output=17000),
                         lambda c: model(c)['limit'].update(output=16000.0),
                         lambda c: c['permission'].update(read='allow'),
                         lambda c: next(iter(c['agent'].values())).update(steps=12.0),
                         lambda c: c['provider'][provider]['options'].update(baseURL='https://remote.invalid/v1')]
            for override in overrides:
                adapter = LocalOllamaReviewerAdapter() if provider == 'ollama' else RemoteOpenCodeReviewerAdapter(provider)
                with self.subTest(provider=provider, override=overrides.index(override)), mock.patch('subprocess.run', side_effect=config_run(request, override)), mock.patch('subprocess.Popen') as process:
                    with self.assertRaises(ProviderNotConfiguredError):
                        adapter.invoke(request)
                    process.assert_not_called()

    def test_current_capability_growth_does_not_expand_and_decrease_rejects(self):
        request = review_request()
        for current, allowed in ((384000, True), (32000, False), (8000, False)):
            with self.subTest(current=current), mock.patch('subprocess.run', side_effect=config_run(request, base_output=current)), mock.patch('subprocess.Popen', side_effect=step_process(1, [])) as process:
                adapter = RemoteOpenCodeReviewerAdapter(request.model.provider)
                if allowed:
                    adapter.invoke(request)
                    config = json.loads(process.call_args.kwargs['env']['OPENCODE_CONFIG_CONTENT'])
                    self.assertEqual(16000, config['provider'][request.model.provider]['models'][request.model.model_id]['limit']['output'])
                else:
                    with self.assertRaises(ProviderNotConfiguredError):
                        adapter.invoke(request)
                    process.assert_not_called()


class BudgetRunnerTests(unittest.TestCase):
    setUp = runner_fixtures.RunnerTests.setUp
    tearDown = runner_fixtures.RunnerTests.tearDown

    def test_joint_budgets_multi_file_review_and_rereview(self):
        from pathlib import Path
        paths = tuple(f'outputs/file-{index}.txt' for index in range(5))
        task = replace(runner_task('joint-budget'), allowed_files=paths, expected_outputs=paths,
            test_command=('/bin/sh', '-c', 'test -f outputs/file-4.txt'),
            review_model=ollama_review_request().model, review_max_steps=12,
            review_max_output_tokens=16000, implementation_max_output_tokens=8000)
        plan = runner_plan(tasks=(task,))
        auth = issue_authorization(plan)
        def implement(request):
            for name in paths:
                file = Path(request.metadata['worktree']) / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(f'{request.role} fixture\n')
            return approved_response(replace(request, metadata={**request.metadata, 'allowed_files': paths[:1]}))
        adapter = LocalOllamaReviewerAdapter(opencode_command='budget-stub', test_double=True)
        router = AdapterRouter({'fake': FakeAdapter(responder=implement)},
            {('ollama', 'review'): adapter, ('ollama', 'rereview'): adapter})
        captured = []
        real_run, real_popen = subprocess.run, subprocess.Popen
        def run(args, **kwargs):
            return config_run(ollama_review_request())(args, **kwargs) if args[0] == 'budget-stub' else real_run(args, **kwargs)
        def popen(args, **kwargs):
            if args[0] != 'budget-stub':
                return real_popen(args, **kwargs)
            for path in paths:
                self.assertIn(path, args[-1])
            process = step_process(3, captured)(args, **kwargs)
            if len(captured) == 1:
                process.stdout = process.stdout.replace('{\\"approved\\":true,\\"findings\\":[]}',
                    '{\\"approved\\":false,\\"findings\\":[{\\"severity\\":\\"P2\\",\\"title\\":\\"fixture change\\",\\"explanation\\":\\"fixture revision required\\"}]}')
            process.stdout = process.stdout.replace('budget-session', f'budget-session-{len(captured)}')
            return process
        with mock.patch('subprocess.run', side_effect=run), mock.patch('subprocess.Popen', side_effect=popen):
            result = Runner(self.database, router, self.workspace).start(plan, auth)
        self.assertEqual(RunState.COMPLETED, result.state)
        self.assertEqual(2, len(captured))
        calls = self.database.connection.execute('SELECT role, request_scope_json FROM model_calls ORDER BY rowid').fetchall()
        self.assertEqual(['implementation', 'review', 'revision', 'rereview'], [row['role'] for row in calls])
        for row in calls:
            budget = json.loads(row['request_scope_json'])['resource_budgets']
            self.assertEqual(16000 if row['role'] in ('review', 'rereview') else 8000, budget['effective_max_output_tokens'])
        for cfg in captured:
            self.assertEqual(12, cfg['agent']['agentflow-local-ollama-reviewer']['steps'])

    def test_actual_fallback_uses_its_frozen_output_capability(self):
        from pathlib import Path
        from agentflow.adapters import ModelUnavailableError
        fallback = contracts.ModelRef('lmstudio', 'fallback-fixture', '1', 'fallback', True)
        task = replace(runner_task('fallback-budget'), fallback_model=fallback)
        plan = runner_plan(tasks=(task,))
        plan = replace(plan, model_capabilities=tuple(replace(item, context_length=32768, max_output_tokens=4096)
            if item.ref == fallback else item for item in plan.model_capabilities))
        def fake(request):
            if request.role == 'implementation':
                raise ModelUnavailableError('fixture unavailable before generation')
            return approved_response(request)
        local_adapter = OpenCodeAdapter(opencode_command='budget-stub')
        local_adapter.test_double = True
        router = AdapterRouter({'fake': FakeAdapter(responder=fake), 'lmstudio': local_adapter})
        request = replace(review_request(), model=fallback, role='implementation', read_only=False)
        captured = []
        real_run, real_popen = subprocess.run, subprocess.Popen
        def run(args, **kwargs):
            return config_run(request, base_output=4096)(args, **kwargs) if args[0] == 'budget-stub' else real_run(args, **kwargs)
        def popen(args, **kwargs):
            if args[0] != 'budget-stub':
                return real_popen(args, **kwargs)
            root = Path(args[args.index('--dir') + 1])
            output = root / task.allowed_files[0]
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text('fallback fixture\n')
            return step_process(1, captured)(args, **kwargs)
        with mock.patch('subprocess.run', side_effect=run), mock.patch('subprocess.Popen', side_effect=popen):
            result = Runner(self.database, router, self.workspace).start(plan, issue_authorization(plan))
        self.assertEqual(RunState.COMPLETED, result.state)
        limit = captured[0]['provider']['lmstudio']['models']['fallback-fixture']['limit']
        self.assertEqual(4096, limit['output'])
        self.assertEqual(32768, limit['context'])

    def test_exhaustion_audit_no_review_and_resume_never_redispatches(self):
        task = replace(runner_task('budget'), review_model=ollama_review_request().model, review_max_steps=2)
        plan = runner_plan(tasks=(task,))
        auth = issue_authorization(plan)
        adapter = LocalOllamaReviewerAdapter(opencode_command='budget-stub', test_double=True)
        router = AdapterRouter({'fake': FakeAdapter(responder=approved_response)},
            {('ollama', 'review'): adapter, ('ollama', 'rereview'): adapter})
        runner = Runner(self.database, router, self.workspace)
        real_run, real_popen = subprocess.run, subprocess.Popen
        request = ollama_review_request()
        captured = []
        def run(args, **kwargs):
            return config_run(request)(args, **kwargs) if args[0] == 'budget-stub' else real_run(args, **kwargs)
        def popen(args, **kwargs):
            return step_process(3, captured)(args, **kwargs) if args[0] == 'budget-stub' else real_popen(args, **kwargs)
        with mock.patch('subprocess.run', side_effect=run), mock.patch('subprocess.Popen', side_effect=popen):
            result = runner.start(plan, auth)
            self.assertEqual(RunState.PAUSED, result.state)
            with self.assertRaises(ValueError):
                runner.resume(result.run_id, plan, auth)
        self.assertEqual(1, len(captured))
        self.assertEqual(0, self.database.fetch_one('SELECT count(*) n FROM reviews')['n'])
        call = self.database.fetch_one("SELECT * FROM model_calls WHERE role='review'")
        self.assertEqual('failed', call['state'])
        self.assertEqual(4, call['output_tokens'])
        self.assertEqual(0, call['remote_cost'])
        budget = json.loads(call['request_scope_json'])['resource_budgets']
        self.assertEqual(2, budget['configured_steps'])
        self.assertEqual(16000, budget['effective_max_output_tokens'])
        self.assertEqual('registry_config', budget['capability']['source'])

    def test_old_paused_database_reads_but_old_authorization_cannot_resume(self):
        import hashlib
        plan = runner_plan(tasks=(runner_task('old'),))
        auth = issue_authorization(plan)
        self.database.save_plan(plan)
        self.database.save_authorization(auth)
        self.database.create_run('old-run', plan, auth)
        self.database.force_pause('old-run')
        raw = to_primitive(plan)
        raw.pop('model_capabilities')
        for task in raw['tasks']:
            for field in ('review_max_steps', 'implementation_max_output_tokens', 'review_max_output_tokens'):
                task.pop(field)
        old_json = canonical_json(raw)
        old_hash = hashlib.sha256(old_json.encode()).hexdigest()
        old_auth = replace(auth, plan_hash=old_hash)
        with self.database.transaction() as connection:
            connection.execute('UPDATE plans SET canonical_json=?, content_hash=?', (old_json, old_hash))
            connection.execute('UPDATE authorizations SET plan_hash=?, snapshot_json=?', (old_hash, canonical_json(old_auth)))
        loaded = self.database.load_plan(plan.plan_id, plan.version)
        self.assertEqual(8, loaded.tasks[0].review_max_steps)
        self.assertEqual((), loaded.model_capabilities)
        with self.assertRaisesRegex(ValueError, 'content changed'):
            Runner(self.database, FakeAdapter(), self.workspace).resume('old-run', loaded, old_auth)
        self.assertEqual(old_json, self.database.fetch_one('SELECT canonical_json FROM plans')['canonical_json'])
        self.assertEqual(0, self.database.fetch_one('SELECT count(*) n FROM model_calls')['n'])

    def test_unknown_call_keeps_budgets_and_tokens_and_blocks_resume(self):
        from agentflow.adapters import InvocationOutcomeUnknown
        from agentflow.contracts import InvocationResult
        task = runner_task('unknown-budget')
        plan = runner_plan(tasks=(task,))
        auth = issue_authorization(plan)
        def respond(request):
            raise InvocationOutcomeUnknown('fixture transport loss', 'fixture-session',
                result=InvocationResult('fixture-session', '', 11, 7, None, 3, 0,
                    {'test_double': True, 'termination_reason': 'timeout'}))
        fake = FakeAdapter(responder=respond)
        runner = Runner(self.database, fake, self.workspace)
        result = runner.start(plan, auth)
        self.assertEqual(RunState.PAUSED, result.state)
        call = self.database.fetch_one('SELECT * FROM model_calls')
        self.assertEqual('unknown', call['state'])
        self.assertEqual(7, call['output_tokens'])
        self.assertEqual('', call['output_text'])
        self.assertEqual(16000, json.loads(call['request_scope_json'])['resource_budgets']['effective_max_output_tokens'])
        with self.assertRaises(ValueError):
            runner.resume(result.run_id, plan, auth)
        self.assertEqual(1, self.database.fetch_one('SELECT count(*) n FROM model_calls')['n'])

    def test_unknown_capability_pauses_without_inference(self):
        task = replace(runner_task('missing-budget'), review_model=ollama_review_request().model)
        plan = replace(runner_plan(tasks=(task,)), model_capabilities=())
        with self.assertRaisesRegex(ValueError, 'capability snapshots'):
            issue_authorization(plan)
        # Emulate a corrupt/old snapshot that reached dispatch; adapter still refuses.
        auth = replace(issue_authorization(budgeted_plan(plan)), plan_hash=plan_hash(plan))
        adapter = LocalOllamaReviewerAdapter(opencode_command='budget-stub')
        router = AdapterRouter({'fake': FakeAdapter(responder=approved_response)}, {('ollama', 'review'): adapter})
        real_popen = subprocess.Popen
        def process(args, **kwargs):
            self.assertNotEqual('budget-stub', args[0])
            return real_popen(args, **kwargs)
        with mock.patch('subprocess.Popen', side_effect=process):
            result = Runner(self.database, router, self.workspace).start(plan, auth)
        self.assertEqual(RunState.PAUSED, result.state)
        call = self.database.fetch_one("SELECT request_scope_json FROM model_calls WHERE role='review'")
        self.assertEqual('unknown_pause', json.loads(call['request_scope_json'])['resource_budgets']['policy'])

    def test_service_rejects_budget_metadata_outside_authorized_task(self):
        from agentflow.service import InvocationService, InvocationContext, PolicyDeniedError
        from test_core import request_for
        task = make_task()
        plan = budgeted_plan(make_plan(task=task))
        auth = issue_authorization(plan)
        request = request_for(task)
        forged = invocation_budgets(plan, task, request.model, request.role)
        forged['authorized_max_output_tokens'] = 99999
        request = replace(request, metadata={'resource_budgets': forged})
        with self.assertRaises(PolicyDeniedError):
            InvocationService(self.database, FakeAdapter()).invoke(request, InvocationContext(plan, task, auth, 'unused'))
