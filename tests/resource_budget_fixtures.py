"""Explicit fictional capabilities for legacy process-double tests; never model facts."""
from dataclasses import replace
import json
from subprocess import CompletedProcess
from agentflow.contracts import InvocationRequest, ModelCapabilitySnapshot
from agentflow.serialization import digest_sha256, to_primitive
from agentflow.contracts import PlanContract


def budgeted_plan_contract(**kwargs):
    return budgeted_plan(PlanContract(**kwargs))


def capability(model):
    return ModelCapabilitySnapshot(model, 262144, 65536, 'registry_config', 'deterministic-fixture-v1')


def budget_metadata(model, role, authorized=16000):
    snapshot = capability(model)
    return {'resource_budgets': {
        'role': role, 'model': to_primitive(model), 'configured_steps': 8,
        'authorized_max_output_tokens': authorized,
        'capability': to_primitive(snapshot), 'capability_id': digest_sha256(snapshot),
        'effective_max_output_tokens': min(authorized, 65536),
        'policy': 'frozen_min_reject_decrease_v1',
    }}


def budgeted_request(**kwargs):
    kwargs['metadata'] = {**budget_metadata(kwargs['model'], kwargs['role']), **kwargs.get('metadata', {})}
    return InvocationRequest(**kwargs)


def budgeted_plan(plan):
    refs = {m.registry_key: m for t in plan.tasks for m in (t.implementation_model, t.review_model, t.fallback_model) if m}
    return replace(plan, model_capabilities=tuple(capability(refs[key]) for key in sorted(refs)))


def resolved_output_stub(command, root, timeout, *, environment=None):
    """Configuration resolver double for pre-existing tests of other behavior.

    New budget tests exercise the real resolver and explicit managed overrides.
    """
    config = json.loads(environment['OPENCODE_CONFIG_CONTENT'])
    for provider in config['enabled_providers']:
        record = config.setdefault('provider', {}).setdefault(provider, {})
        record.setdefault('npm', '@ai-sdk/openai-compatible')
        models = record.setdefault('models', {})
        # Common fictional and historical identifiers used by process doubles.
        for mid in ('review-model', 'worker-model', 'local-model', 'qwen/qwen3.8-27b', 'coder', 'reviewer', 'local-coder', 'local-reviewer'):
            models.setdefault(mid, {'limit': {'context': 262144, 'output': 65536}})
    return json.dumps(config)
