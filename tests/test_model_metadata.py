"""Model metadata must agree with itself.

AIngel describes each model in three places — `agent_config.MODELS` (label,
provider, cost), `agent_config.PRICING` (the cost the Counselor ranks on) and
`model_caps.json` (context window, output cap, behaviour). They have drifted
twice, and both times the symptom was silent:

* `scw-qwen3.5-397b` was priced 4x over Scaleway's rate, so the cost-weighted
  Counselor steered every EU-only project away from the EU catalogue;
* `mistral-large-latest` stayed at PAYG rates after moving into the Pro
  subscription, so a model already paid for was never recommended.

Neither raised an error anywhere. These are checks, not documentation.

Run: python3 -m pytest tests/test_model_metadata.py -q
  or: python3 tests/test_model_metadata.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_config  # noqa: E402
from model_caps import _CAPS, effective_max_tokens, get_cap  # noqa: E402


def test_pricing_agrees_with_models():
    """MODELS.cost_* and PRICING must not disagree for any registered model."""
    mismatches = []
    for model_id, meta in agent_config.MODELS.items():
        priced = agent_config.PRICING.get(model_id)
        if priced is None:
            continue  # resolved by the fuzzy fallback; covered separately below
        if (meta.get('cost_input') != priced['input']
                or meta.get('cost_output') != priced['output']):
            mismatches.append(
                f"{model_id}: MODELS={meta.get('cost_input')}/{meta.get('cost_output')} "
                f"PRICING={priced['input']}/{priced['output']}")
    assert not mismatches, 'MODELS and PRICING disagree:\n  ' + '\n  '.join(mismatches)


def test_every_model_resolves_a_price():
    """No registered model may fall through get_pricing() unpriced."""
    unpriced = [m for m in agent_config.MODELS if agent_config.get_pricing(m) is None]
    assert not unpriced, f'models with no resolvable price: {unpriced}'


def test_fuzzy_large_fallback_is_not_free():
    """An unknown 'large' model must not inherit the subscription price of 0.

    mistral-large-latest is Pro-covered and priced at 0. Pointing the substring
    fallback at it would price an unknown paid model at nothing, and silently
    defeat budget enforcement.
    """
    p = agent_config.get_pricing('some-unknown-large-model')
    assert p is not None and p['input'] > 0, (
        "the 'large' substring fallback resolves to a zero price")


def test_caps_cover_registered_models():
    """Every model in MODELS should have capabilities, or inherit safe defaults."""
    for model_id in agent_config.MODELS:
        cap = get_cap(model_id)
        assert cap['max_tokens'] > 0, f'{model_id}: non-positive max_tokens'
        assert cap['context_window'] > 0, f'{model_id}: non-positive context_window'
        assert isinstance(cap['reasoning'], bool), f'{model_id}: reasoning must be bool'


def test_caps_file_has_no_unknown_models():
    """model_caps.json must not describe models that no longer exist.

    A stale entry is how a wire-id rename goes unnoticed — the caps lookup keeps
    succeeding against a model nothing can route to.
    """
    unknown = [m for m in _CAPS if m not in agent_config.MODELS]
    assert not unknown, f'model_caps.json describes unregistered models: {unknown}'


def test_reasoning_models_get_a_usable_output_budget():
    """A small request against a reasoning model must be raised, not passed through.

    Measured on scw-qwen3.6-35b: 9 of 12 calls at max_tokens=800 returned empty
    content, because the reasoning consumed the budget before any answer. The
    Autopilot hooks ask for 800.
    """
    reasoning = [m for m in agent_config.MODELS if get_cap(m).get('reasoning')]
    assert reasoning, 'no reasoning models flagged — has the probe data been lost?'
    for model_id in reasoning:
        got = effective_max_tokens(model_id, 800)
        assert got > 800, f'{model_id}: 800-token request not raised (got {got})'
        assert got <= get_cap(model_id)['max_tokens'], (
            f'{model_id}: raised budget {got} exceeds the model cap')


def test_non_reasoning_models_are_left_alone():
    """The floor must not inflate budgets for models that do not need it."""
    for model_id in agent_config.MODELS:
        if not get_cap(model_id).get('reasoning'):
            assert effective_max_tokens(model_id, 800) == 800, (
                f'{model_id}: budget changed for a non-reasoning model')


def test_eu_models_are_consistently_classified():
    """is_eu_model must agree with the provider recorded in MODELS."""
    EU_PROVIDERS = {'mistral', 'vibe', 'scaleway'}
    wrong = []
    for model_id, meta in agent_config.MODELS.items():
        by_id = agent_config.is_eu_model(model_id)
        by_provider = meta.get('provider') in EU_PROVIDERS
        if by_id != by_provider:
            wrong.append(f"{model_id}: is_eu_model={by_id} provider={meta.get('provider')}")
    assert not wrong, 'EU classification disagrees with provider:\n  ' + '\n  '.join(wrong)


if __name__ == '__main__':
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith('test_') or not callable(fn):
            continue
        try:
            fn()
            print(f'  ok    {name}')
        except AssertionError as e:
            failures += 1
            print(f'  FAIL  {name}\n          {e}')
    print()
    print('ALL PASS' if not failures else f'{failures} FAILURE(S)')
    raise SystemExit(1 if failures else 0)
