"""LM Studio review boundary tests; subprocesses are explicit test doubles."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from agentflow.adapters import ProviderNotConfiguredError
from agentflow.opencode_adapter import OpenCodeAdapter
from resource_budget_fixtures import budget_metadata
from test_remote_reviewer import review_request
from test_reviewer_resource_budgets import config_run, step_process


def local_request(directory, role='review'):
    original = review_request(role=role)
    model = replace(original.model, provider='lmstudio', is_local=True)
    return replace(original, model=model, metadata={
        'worktree': directory, **budget_metadata(model, role)})


class LMStudioReviewIsolationTests(unittest.TestCase):
    def test_review_and_rereview_cannot_grant_reads_of_stale_files(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'old.py').write_text('STALE_BASELINE_SECRET')
            for role in ('review', 'rereview'):
                request = local_request(directory, role)
                captured = []
                def launch(command, **kwargs):
                    config = json.loads(kwargs['env']['OPENCODE_CONFIG_CONTENT'])
                    for rules in (config['permission'], config['agent']['agentflow-sandbox']['permission']):
                        for tool in ('*', 'read', 'glob', 'grep', 'edit', 'write', 'bash', 'external_directory', 'task', 'skill'):
                            self.assertEqual('deny', rules[tool], (role, tool))
                    self.assertNotIn('STALE_BASELINE_SECRET', command[-1])
                    return step_process(1, captured)(command, **kwargs)
                with self.subTest(role=role), mock.patch('subprocess.run', side_effect=config_run(request)), mock.patch('subprocess.Popen', side_effect=launch):
                    result = OpenCodeAdapter().invoke(request)
                    self.assertIn('approved', result.output)
                    self.assertEqual(1, len(captured))

    def test_review_rejects_write_enabled_request_before_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            for role in ('review', 'rereview'):
                request = replace(local_request(directory, role), read_only=False)
                with self.subTest(role=role), mock.patch('subprocess.run', side_effect=config_run(request)) as discover, mock.patch('subprocess.Popen', side_effect=step_process(1, [])) as launch:
                    with self.assertRaises(ValueError):
                        OpenCodeAdapter().invoke(request)
                    discover.assert_not_called()
                    launch.assert_not_called()

    def test_managed_global_and_agent_read_grants_stop_before_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            request = local_request(directory)
            for layer in ('global', 'agent'):
                for tool, grant in (('read', 'allow'), ('glob', 'ask'), ('grep', {'*': 'allow'}), ('custom_tool', 'allow')):
                    def override(config):
                        rules = config['permission'] if layer == 'global' else config['agent']['agentflow-sandbox']['permission']
                        rules[tool] = grant
                    with self.subTest(layer=layer, tool=tool), mock.patch('subprocess.run', side_effect=config_run(request, override)), mock.patch('subprocess.Popen') as launch:
                        with self.assertRaises(ProviderNotConfiguredError):
                            OpenCodeAdapter().invoke(request)
                        launch.assert_not_called()

    def test_implementation_and_revision_keep_existing_file_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            for role in ('implementation', 'revision'):
                request = replace(local_request(directory, role), read_only=False)
                captured = []
                with mock.patch('subprocess.run', side_effect=config_run(request)), mock.patch('subprocess.Popen', side_effect=step_process(1, captured)):
                    OpenCodeAdapter().invoke(request)
                rules = captured[0]['permission']
                self.assertEqual('allow', rules['read']['*'])
                self.assertEqual('deny', rules['read']['.env'])
                self.assertEqual('allow', rules['edit'])
                self.assertEqual('deny', rules['bash'])

    def test_version_diagnostic_identifies_verified_and_found_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            request = local_request(directory)
            def run(args, **kwargs):
                if args[1] == '--version':
                    return subprocess.CompletedProcess(args, 0, stdout='1.18.30\n')
                return config_run(request)(args, **kwargs)
            with mock.patch('subprocess.run', side_effect=run), mock.patch('subprocess.Popen') as launch:
                with self.assertRaises(ProviderNotConfiguredError) as error:
                    OpenCodeAdapter().invoke(request)
                self.assertIn('1.18.29', str(error.exception))
                self.assertIn('1.18.30', str(error.exception))
                launch.assert_not_called()

    def test_override_diagnostic_names_key_but_never_value(self):
        with tempfile.TemporaryDirectory() as directory:
            request = local_request(directory)
            for key in ('no_thinking', 'thinking_mode', 'max_completion_tokens'):
                def run(args, **kwargs):
                    result = config_run(request)(args, **kwargs)
                    if args[1] == 'debug':
                        config = json.loads(result.stdout)
                        config['provider']['lmstudio']['options'][key] = 'SENSITIVE_VALUE'
                        result.stdout = json.dumps(config)
                    return result
                with self.subTest(key=key), mock.patch('subprocess.run', side_effect=run), mock.patch('subprocess.Popen') as launch:
                    with self.assertRaises(ProviderNotConfiguredError) as error:
                        OpenCodeAdapter().invoke(request)
                    self.assertIn(key, str(error.exception))
                    self.assertNotIn('SENSITIVE_VALUE', str(error.exception))
                    launch.assert_not_called()
