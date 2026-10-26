"""Money regressions: exact tariffs, cache semantics, copied sessions and visibility."""

import json
import shutil
import hashlib
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.app.models import Framework, Job, JobStatus, JobType, VerifyConfig
from backend.app.models.billing import BillingArchive, BillingSnapshot, ModelPrice, PriceSnapshot, PricingSettings, RateTier, UsageCharge
from backend.app.models.job import HarborAction
from backend.app.services import billing_service as billing
from backend.app.store import store
from backend.app.store.json_store import JsonCollection, JsonDocument
from .conftest import project_id, user_id_of


@pytest.fixture(autouse=True)
def isolated_billing(tmp_path, monkeypatch):
    # Drain background work before rebinding stores; individual concurrency tests start their own worker.
    billing.worker.stop()
    worker = billing.BillingWorker()
    monkeypatch.setattr(billing, 'worker', worker)
    monkeypatch.setattr(store, 'usage_charges', JsonCollection(tmp_path / 'ledger.json', UsageCharge))
    monkeypatch.setattr(store, 'billing_archives', JsonCollection(tmp_path / 'archived.json', BillingArchive))
    monkeypatch.setattr(store, 'pricing_doc', JsonDocument(tmp_path / 'pricing.json', PricingSettings))
    monkeypatch.setattr(store, 'jobs', JsonCollection(tmp_path / 'jobs.json', Job))
    yield
    worker.stop()


def test_finished_cost_is_persisted_and_reused_after_reload(tmp_path, monkeypatch):
    item, root = job(tmp_path)
    trajectory(root)
    result = billing.refresh(item, force=True)
    assert result.billing.input_signature
    # Simulate a restart: no in-memory collection or refresh timestamps survive.
    monkeypatch.setattr(store, 'jobs', JsonCollection(store.jobs.path, Job))
    monkeypatch.setattr(billing, '_scan_trial', lambda *_: pytest.fail('cached task must not scan logs'))
    restored = billing.refresh(store.jobs.get(item.id))
    assert restored.billing.amount_cny == result.billing.amount_cny
    assert restored.billing.updated_at == result.billing.updated_at
    assert not billing.needs_refresh(restored)
    renamed = restored.model_copy(deep=True)
    renamed.name = 'renamed'
    assert not billing.needs_refresh(renamed)
    renamed.status = JobStatus.RUNNING
    assert billing.needs_refresh(renamed)


def test_parallel_scan_does_not_block_lists_and_preserves_ledger(tmp_path, monkeypatch, client, member_headers):
    from backend.app.api import system
    monkeypatch.setattr(system.monitor, 'latest', lambda: {})
    monkeypatch.setattr(system.monitor, 'compact_history', lambda *_: [])
    monkeypatch.setattr(system, 'gpu_snapshot', lambda: {})
    first, root = job(tmp_path)
    trajectory(root, session='shared-session')
    second, root2 = job(tmp_path, name='two')
    # Ensure the lock stripes differ; the production worker count remains bounded.
    lock_index = lambda value: int(hashlib.sha256(value.encode()).hexdigest(), 16) % len(billing._JOB_LOCKS)
    while lock_index(first.id) == lock_index(second.id):
        store.jobs.delete(second.id)
        second, root2 = job(tmp_path, name=second.name + 'x')
    trajectory(root2, session='shared-session')
    barrier = threading.Barrier(2)
    release = threading.Event()
    entered = threading.Event()
    scan = billing._scan_trial
    calls = []

    def slow_scan(*args):
        calls.append(args[0])
        barrier.wait(timeout=5)
        entered.set()
        assert release.wait(5)
        return scan(*args)

    monkeypatch.setattr(billing, '_scan_trial', slow_scan)
    billing.worker.start()
    try:
        billing.worker.request([first, second])
        assert entered.wait(5), 'two tasks must enter scanning concurrently'
        billing.worker.request([first, second])
        response = client.get('/api/jobs', headers=member_headers)
        assert response.status_code == 200
        assert set(response.json()['billing_pending']) == {first.id, second.id}
        assert all(row['billing']['amount_cny'] is None for row in response.json()['items'])
        # Dashboard counters load independently, while costs have their own progressive endpoint.
        counters = client.get('/api/system/dashboard?include_billing=false', headers=member_headers)
        assert counters.status_code == 200 and 'billing' not in counters.json()
        summary = client.get('/api/system/billing', headers=member_headers)
        assert summary.status_code == 200 and summary.json()['updating_jobs'] == 2
        assert len(calls) == 2  # duplicate page requests never enqueue duplicate scans
    finally:
        release.set()
        billing.worker.stop()
    assert len(store.usage_charges.all()) == 1  # concurrent copies do not double bill
    saved = [store.jobs.get(first.id), store.jobs.get(second.id)]
    assert sum(row.billing.requests for row in saved) == 1
    assert all(not billing.needs_refresh(row) for row in saved)
    assert client.get('/api/system/billing', headers=member_headers).json()['updating_jobs'] == 0


def test_worker_capacity_and_failure_do_not_drop_saved_costs(tmp_path, monkeypatch):
    item, root = job(tmp_path)
    trajectory(root)
    saved = billing.refresh(item, force=True)
    saved.status = JobStatus.RUNNING
    store.jobs.put(saved)
    release = threading.Event()
    entered = threading.Event()

    def fail(*_args, **_kwargs):
        entered.set()
        assert release.wait(5)
        raise RuntimeError('temporary disk error')

    monkeypatch.setattr(billing, 'refresh', fail)
    worker = billing.BillingWorker(workers=1, capacity=1)
    worker.start()
    try:
        worker.request([saved])
        assert entered.wait(5)
        another, _ = job(tmp_path, name='other')
        worker.request([saved, another])
        assert worker._pending == {saved.id}
    finally:
        release.set()
        worker.stop()
    assert store.jobs.get(item.id).billing.amount_cny == saved.billing.amount_cny


def test_run_changed_during_scan_does_not_commit_stale_totals(tmp_path, monkeypatch):
    item, root = job(tmp_path)
    trajectory(root)
    scan = billing._scan_trial

    def changed(*args):
        store.jobs.update(item.id, lambda value: setattr(value, 'status', JobStatus.RUNNING))
        return scan(*args)

    monkeypatch.setattr(billing, '_scan_trial', changed)
    refreshed = billing.refresh(item, force=True)
    assert refreshed.status == JobStatus.RUNNING
    assert refreshed.billing.updated_at is None
    assert not store.usage_charges.all()


def component(vendor, model):
    price = next(p for p in billing.pricing().prices if p.vendor == vendor and billing._matches(p, model))
    return PriceSnapshot(vendor=vendor, model=model, price=price)


def snapshot(agent=None, judge=None):
    return BillingSnapshot(usd_cny=6.7195, exchange_date='2026-10-22', exchange_source='test',
                           agent=agent or component('atlascloud', 'openai/gpt-6.1-sol-codex'),
                           judge=judge or PriceSnapshot(free=True))


def job(tmp_path, name='one', snap=None, **kwargs):
    result = Job(name=name, type=JobType.VERIFY, framework=Framework.HARBOR, status=JobStatus.FAILED,
                 owner_id=user_id_of('dev@prt.local'), owner_email='dev@prt.local', project_id=project_id(),
                 verify=VerifyConfig(agent='codex', disable_verification=True), **kwargs)
    result.billing.snapshot = snap or snapshot()
    result.runtime.output_dir = str(tmp_path / name / 'outputs')
    result.runtime.work_dir = str(tmp_path / name / 'work')
    root = Path(result.runtime.output_dir) / result.id
    root.mkdir(parents=True)
    (root / 'config.json').write_text('{}')
    store.jobs.put(result)
    return result, root


def trajectory(root, session='s1', phase='agent', model='openai/gpt-6.1-sol-codex', prompt=100000, output=10000, cache=20000, extra=None):
    path = root / 'task__a' / ('agent' if phase == 'agent' else 'verifier') / 'trajectory.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    value = dict(schema_version='ATIF-v1.7', session_id=session, agent=dict(name='codex', model_name=model),
                 steps=[dict(step_id=1, source='agent', timestamp='2026-10-22T10:00:00Z',
                             metrics=dict(prompt_tokens=prompt, completion_tokens=output, cached_tokens=cache, extra=extra or {}))])
    path.write_text(json.dumps(value))
    return path


def test_catalog_discounts_exact_and_gemini_answer():
    settings = billing.pricing()
    PricingSettings.model_validate(settings.model_dump())
    expected = {
        'anthropic/claude-haiku-5.5-ccmax': .43, 'openai/gpt-6.1-sol-codex': .15,
        'anthropic/claude-opus-4.8': 1, 'anthropic/claude-opus-4.8-ccmax': .58,
        'anthropic/claude-opus-4.8-coding': .58, 'openai/gpt-6-sol-codex': .3,
        'anthropic/claude-opus-5.5-ccmax': .4, 'google/gemini-3.8-flash': .5,
        'openai/gpt-6-astra-codex': .28, 'anthropic/claude-fable-5': .95,
        'anthropic/claude-fable-5-ccmax': .58, 'anthropic/claude-fable-5.1-ccmax': .55,
        'anthropic/claude-opus-5-ccmax': .4, 'openai/gpt-5.6-sol': .96,
        'openai/gpt-5.6-sol-az': .5, 'openai/gpt-5.6-sol-codex': .5,
        'openai/gpt-6-astra': .75, 'anthropic/claude-sonnet-5.5-ccmax': .44,
    }
    for model, discount in expected.items():
        rate = component('atlascloud', model).price
        assert rate.discount == discount
        assert billing._matches(rate, model.split('/', 1)[1])
        assert not billing._matches(rate, model + '-unknown')
    assert all(p.discount == .6 for p in settings.prices if p.vendor == 'alibaba')


def test_qwen_max_and_long_context_tariffs():
    for model in ('qwen3.8-max', 'qwen3.7-max', 'qwen3.7-max-2026-06-08'):
        snap = snapshot(agent=component('alibaba', model))
        amount, _ = billing.estimate(dict(input=100000, output=10000), snap.agent, snap)
        assert amount == pytest.approx((.1 * 12 + .01 * 36) * .6)
    snap = snapshot(agent=component('alibaba', 'qwen3.7-plus'))
    low, _ = billing.estimate(dict(input=256000, output=1000), snap.agent, snap)
    high, _ = billing.estimate(dict(input=256001, output=1000), snap.agent, snap)
    assert low == pytest.approx((.256 * 2 + .001 * 8) * .6)
    assert high == pytest.approx((.256001 * 6 + .001 * 24) * .6)
    assert component('alibaba', 'qwen3.7-flash-2026-07-15').price.model == 'qwen3.7-flash'


def test_deepseek_claude_context_alias_is_billable(tmp_path):
    item, root = job(tmp_path)
    item.verify.agent = 'claude-code'
    item.verify.model = 'deepseek-flash[1m]'
    item.verify.agent_env = {'ANTHROPIC_BASE_URL': 'https://api.deepseek.com/anthropic'}
    item.billing.snapshot = billing.capture(item)
    store.jobs.put(item)
    assert item.billing.snapshot.agent.price.model == 'deepseek-flash'
    trajectory(root, model='deepseek-flash', prompt=100000, output=10000, cache=20000)
    updated = billing.refresh(item, force=True)
    # 10:00Z is 18:00 Beijing time, an off-peak half-price request.
    assert updated.billing.agent_cny == pytest.approx((.08 * 2 + .01 * 8 + .02 * .04) / 2)
    assert not any('价格待配置' in note for note in updated.billing.warnings)
    assert updated.billing.status == 'estimated'
    assert not billing._matches(item.billing.snapshot.agent.price, 'deepseek-flash-unknown')


def test_flash_price_supplements_old_catalog_without_overwriting_custom_rates():
    unrelated = component('atlascloud', 'openai/gpt-6.1-sol-codex').price
    store.pricing_doc.write(PricingSettings(prices=[unrelated]))
    assert component('deepseek', 'deepseek-flash[1m]').price.tiers[0].input == 2
    custom = component('deepseek', 'deepseek-flash').price.model_copy(deep=True)
    custom.aliases = []  # Older saved catalogs have no Claude context alias.
    custom.tiers[0].input = 7
    store.pricing_doc.write(PricingSettings(prices=[custom]))
    assert component('deepseek', 'deepseek-flash[1m]').price.tiers[0].input == 7
    store.pricing_doc.write(PricingSettings(prices=[]))
    assert billing.pricing().prices == []


@pytest.mark.parametrize('keep_logs', [True, False])
def test_flash_missing_historical_prices_repaired_without_duplicate_charges(tmp_path, keep_logs):
    item, root = job(tmp_path)
    item.verify.agent = 'claude-code'
    item.verify.model = 'deepseek-flash[1m]'
    item.verify.disable_verification = False
    item.verify.agent_env = {'ANTHROPIC_BASE_URL': 'https://api.deepseek.com/anthropic'}
    item.verify.verifier_env = {'JUDGE_BASE_URL': 'https://api.deepseek.com', 'JUDGE_MODEL': 'deepseek-flash[1m]'}
    store.pricing_doc.write(PricingSettings(prices=[]))
    item.billing.snapshot = billing.capture(item, historical=True)
    store.jobs.put(item)
    for phase in ('agent', 'judge'):
        trajectory(root, phase=phase, model='deepseek-flash')
    unpriced = billing.refresh(item, force=True)
    assert unpriced.billing.amount_cny is None
    ids = {row.id for row in store.usage_charges.all()}
    assert len(ids) == 2
    if not keep_logs:
        shutil.rmtree(root / 'task__a')
    # Simulate pre-fix cached signatures and an installed catalog missing DeepSeek.
    store.pricing_doc.write(PricingSettings())
    unrelated = component('atlascloud', 'openai/gpt-6.1-sol-codex').price
    store.pricing_doc.write(PricingSettings(prices=[unrelated]))
    store.jobs.update(item.id, lambda value: setattr(value.billing, 'input_signature', 'old-signature'))
    assert billing.needs_refresh(store.jobs.get(item.id))
    updated = billing.refresh(store.jobs.get(item.id))
    # Logs carry the 18:00 Beijing request time (off-peak); ledger-only repair has totals only, so peak.
    expected = (.08 * 2 + .01 * 8 + .02 * .04) / (2 if keep_logs else 1)
    assert updated.billing.agent_cny == pytest.approx(expected)
    assert updated.billing.judge_cny == pytest.approx(expected)
    assert updated.billing.amount_cny == pytest.approx(expected * 2)
    assert {row.id for row in store.usage_charges.all()} == ids
    assert not any('价格待配置' in note for note in updated.billing.warnings)
    assert updated.billing.snapshot.agent.price.model == 'deepseek-flash'
    assert updated.billing.snapshot.judge.price.model == 'deepseek-flash'
    assert not billing.needs_refresh(updated)
    repeated = billing.refresh(updated, force=True)
    assert repeated.billing.amount_cny == updated.billing.amount_cny
    assert {row.id for row in store.usage_charges.all()} == ids
    resumed, resumed_root = job(tmp_path, name='resumed', snap=updated.billing.snapshot,
                               harbor_action=HarborAction(mode='resume', source_job_id=item.id))
    resumed.verify = item.verify.model_copy(deep=True)
    store.jobs.put(resumed)
    for phase in ('agent', 'judge'):
        trajectory(resumed_root, phase=phase, model='deepseek-flash')
    billing.refresh(resumed, force=True)
    assert {row.id for row in store.usage_charges.all()} == ids
    assert all(row.job_id == item.id for row in store.usage_charges.all())


@pytest.mark.parametrize('constraint', ['disabled', 'changed_provider', 'subscription'])
def test_flash_repair_respects_account_and_pricing_constraints(tmp_path, monkeypatch, constraint):
    from backend.app.models.model_api import ModelApiConfig, ModelPlatformConfig
    item, _ = job(tmp_path, snap=snapshot(agent=PriceSnapshot(vendor='deepseek', model='deepseek-flash[1m]')))
    item.verify.agent = 'claude-code'
    item.verify.model = 'deepseek-flash[1m]'
    item.verify.agent_env = {'ANTHROPIC_BASE_URL': 'https://api.deepseek.com/anthropic'}
    if constraint == 'disabled':
        store.pricing_doc.write(PricingSettings(prices=[]))
    elif constraint == 'changed_provider':
        item.verify.agent_env['ANTHROPIC_BASE_URL'] = 'https://example.com/anthropic'
    else:
        monkeypatch.setattr(store, 'model_apis', JsonCollection(tmp_path / 'apis.json', ModelApiConfig))
        monkeypatch.setattr(store, 'model_platforms', JsonCollection(tmp_path / 'platforms.json', ModelPlatformConfig))
        platform = ModelPlatformConfig(vendor='deepseek', name='subscription', access_mode='subscription',
                                       endpoints={'anthropic': 'https://api.deepseek.com/anthropic'})
        store.model_platforms.put(platform)
        api = ModelApiConfig(name='deepseek-flash[1m]', platform_id=platform.id, provider='anthropic')
        store.model_apis.put(api)
        item.verify.model_api_id = api.id
    assert billing._repair_missing_snapshot(item.billing.snapshot, item, billing.pricing()).agent.price is None


def test_qwen_saved_wrong_region_and_missing_price_are_repaired(tmp_path):
    item, root = job(tmp_path, snap=snapshot(agent=PriceSnapshot(
        vendor='alibaba', model='qwen3.8-max', region='other', cache_mode='explicit')))
    item.verify.agent = 'claude-code'
    item.verify.model = 'qwen3.8-max'
    item.verify.agent_env = {'ANTHROPIC_BASE_URL': 'https://llm-example.cn-beijing.maas.aliyuncs.com/apps/anthropic'}
    store.jobs.put(item)
    trajectory(root, model='qwen3.8-max', cache=0)
    updated = billing.refresh(item, force=True)
    assert updated.billing.snapshot.agent.region == 'cn-beijing'
    assert updated.billing.snapshot.agent.price.model == 'qwen3.8-max'
    assert updated.billing.agent_cny == pytest.approx((.1 * 12 + .01 * 36) * .6)


def test_actual_usage_model_overrides_display_name_and_configured_model(tmp_path, monkeypatch):
    from backend.app.models.model_api import ModelApiConfig
    monkeypatch.setattr(store, 'model_apis', JsonCollection(tmp_path / 'apis.json', ModelApiConfig))
    api = ModelApiConfig(name='My expensive model', model='qwen3.8-max', base_url='https://dashscope.aliyuncs.com/compatible-mode/v1')
    store.model_apis.put(api)
    item, root = job(tmp_path, snap=snapshot(agent=PriceSnapshot(
        vendor='alibaba', model='qwen3.8-max', region='cn-beijing', price=component('alibaba', 'qwen3.8-max').price)))
    item.verify.model_api_id = api.id
    item.verify.model = 'qwen3.8-max'
    store.jobs.put(item)
    trajectory(root, model='qwen3.8-flash', cache=0)
    updated = billing.refresh(item, force=True)
    assert updated.billing.agent_cny == pytest.approx((.1 * .8 + .01 * 2.7) * .6)
    charge = store.usage_charges.all()[0]
    assert charge.model == 'qwen3.8-flash'
    assert charge.snapshot.agent.price.model == 'qwen3.8-flash'
    api.name = 'renamed as GPT'
    api.model = 'qwen3.7-plus'
    store.model_apis.put(api)
    assert billing.capture(item).agent.model == 'qwen3.8-max'  # immutable submitted model
    repeated = billing.refresh(updated, force=True)
    assert repeated.billing.amount_cny == updated.billing.amount_cny
    assert len(store.usage_charges.all()) == 1


def test_unknown_usage_model_does_not_inherit_displayed_price(tmp_path):
    item, root = job(tmp_path)
    trajectory(root, model='unverified-model')
    updated = billing.refresh(item, force=True)
    assert updated.billing.amount_cny is None
    assert any('价格待配置' in warning for warning in updated.billing.warnings)


def test_single_tier_aggregated_usage_can_exceed_per_request_context():
    snap = snapshot(agent=component('alibaba', 'qwen3.8-max'))
    amount, _ = billing.estimate({'input': 2000000, 'output': 100000}, snap.agent, snap, aggregate=True)
    assert amount == pytest.approx((2 * 12 + .1 * 36) * .6)


def test_new_server_model_rates_and_exact_aliases():
    assert component('deepseek', 'deepseek-v4-pro[1m]').price.model == 'deepseek-v4-pro'
    assert component('anthropic', 'claude-opus-4-8').price.tiers[0].input == 5
    assert component('atlascloud', 'deepseek-ai/deepseek-v3.2').price.tiers[0].output == .38
    terra = component('atlascloud', 'openai/gpt-5.6-terra').price
    assert terra.tiers[0].input == 2 and terra.tiers[1].output == 18


@pytest.mark.parametrize('model,discount', [('openai/gpt-6.1-sol', 1), ('openai/gpt-6.1-sol-codex', .15)])
def test_exact_reseller_route_precedes_shared_official_model(tmp_path, model, discount):
    item, _ = job(tmp_path)
    item.verify.model = model
    item.verify.agent_env = {'OPENAI_BASE_URL': 'https://api.atlascloud.ai/v1'}
    captured = billing.capture(item)
    assert captured.agent.price.model == model
    assert captured.agent.price.discount == discount


def test_flash_valid_historical_prices_are_not_repriced(tmp_path):
    item, root = job(tmp_path, snap=snapshot(agent=component('deepseek', 'deepseek-flash[1m]')))
    item.verify.agent = 'claude-code'
    item.verify.model = 'deepseek-flash[1m]'
    item.verify.agent_env = {'ANTHROPIC_BASE_URL': 'https://api.deepseek.com/anthropic'}
    item.billing.snapshot.agent.price.tiers[0].input = 7
    store.jobs.put(item)
    trajectory(root, model='deepseek-flash')
    updated = billing.refresh(item, force=True)
    assert updated.billing.agent_cny == pytest.approx((.08 * 7 + .01 * 8 + .02 * .04) / 2)  # off-peak request
    assert updated.billing.snapshot.agent.price.tiers[0].input == 7


def test_deepseek_vendor_and_direct_supply_prices_remain_distinct():
    rates = [('deepseek', 'deepseek-v4-pro', 9),
             ('tencent', 'deepseek-v4-pro', 12),
             ('tencent', 'deepseek/deepseek-v4-pro', 9),
             ('tencent', 'deepseek-v4-flash-0731', 3),
             ('tencent', 'deepseek/deepseek-v4-flash-0731', 2)]
    for vendor, model, expected in rates:
        snap = snapshot(agent=component(vendor, model))
        amount, _ = billing.estimate(dict(input=1000000), snap.agent, snap)
        assert amount == expected
    assert component('alibaba', 'deepseek-v4.1-flash').price.tiers[0].cache_read == .2
    assert component('deepseek', 'deepseek-flash').price.tiers[0].cache_read == .04


def test_beijing_workspace_endpoint_does_not_match_foreign_or_spoofed_hosts():
    assert billing._vendor('https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1') == ('alibaba', 'cn-beijing')
    assert billing._vendor('https://workspace.ap-southeast-1.maas.aliyuncs.com/v1') == ('alibaba', 'other')
    assert billing._vendor('https://workspace.cn-beijing.maas.aliyuncs.com.evil.test/v1') == ('custom', '')


def test_catalog_available_without_overwriting_saved_prices(client, member_headers):
    saved = PricingSettings(prices=[])
    store.pricing_doc.write(saved)
    response = client.get('/api/system/pricing/catalog', headers=member_headers)
    assert response.status_code == 200
    PricingSettings(prices=response.json())
    assert any(row['model'] == 'qwen3.7-max' for row in response.json())
    assert billing.pricing().prices == []
    assert client.get('/api/system/pricing/catalog').status_code == 401


def test_rmb_cache_and_reasoning_not_double_counted():
    snap = snapshot()
    tokens = billing.normalized_usage(dict(prompt_tokens=100000, completion_tokens=10000, cached_tokens=20000,
                                           extra={'cache_write_input_tokens': 5000, 'reasoning_output_tokens': 3000}))
    assert tokens == dict(input=75000, output=10000, cache_read=20000, cache_write=5000, cache_write_long=0)
    value, warnings = billing.estimate(tokens, snap.agent, snap)
    assert value == pytest.approx((.075*2 + .01*10 + .02*.1 + .005*2.5) * .15 * 6.7195)
    assert not warnings


def test_anthropic_long_cache_write_and_normalized_total():
    tokens = billing.normalized_usage(dict(prompt_tokens=10000, completion_tokens=1000, cached_tokens=1000,
                                          extra={'cache_creation_input_tokens': 5000,
                                                 'cache_creation': {'ephemeral_1h_input_tokens': 2000}}))
    assert tokens['input'] == 4000
    assert tokens['cache_write'] == 3000
    assert tokens['cache_write_long'] == 2000
    snap = snapshot(agent=component('atlascloud', 'anthropic/claude-opus-4.8-ccmax'))
    value, _ = billing.estimate(tokens, snap.agent, snap)
    assert value == pytest.approx((.004*5+.001*25+.001*.5+.003*6.25+.002*10)*.58*6.7195)


def test_tiers_are_per_request_not_total():
    snap = snapshot(agent=component('alibaba', 'qwen3.7-flash'))
    tokens = dict(input=20000, output=1000)
    value, _ = billing.estimate(tokens, snap.agent, snap)
    assert value * 100 == pytest.approx((.02*.2+.001*.8)*.6*100)
    high, notes = billing.estimate(dict(input=2000000, output=100000), snap.agent, snap, aggregate=True)
    assert high == pytest.approx((2*1.2+.1*4.8)*.6)
    assert any('最高输入阶梯' in note for note in notes)


def test_haiku_threshold_includes_cache():
    snap = snapshot(agent=component('atlascloud', 'anthropic/claude-haiku-5.5-ccmax'))
    value, _ = billing.estimate(dict(input=1, cache_read=100000, output=1000), snap.agent, snap)
    assert value == pytest.approx((.000001*.5+.1*.05+.001*2.5)*.43*6.7195)


def test_unknown_cache_is_partial_amount_not_free():
    snap = snapshot(agent=component('alibaba', 'deepseek-v4-pro'))
    value, notes = billing.estimate(dict(input=1000, cache_read=1000, output=100), snap.agent, snap)
    assert value == pytest.approx((.001*12+.0001*24)*.6)
    assert any('cache_read' in note for note in notes)
    value, notes = billing.estimate(dict(cache_read=1000), snap.agent, snap)
    assert value is None


@pytest.mark.parametrize('model,unit', [('qwen3.8-max', 12), ('qwen3.8-flash', .8)])
def test_qwen38_cache_hit_is_tenth_of_input(model, unit):
    snap = snapshot(agent=component('alibaba', model))
    value, notes = billing.estimate(dict(input=1000, cache_read=1000), snap.agent, snap)
    assert value == pytest.approx((.001*unit + .001*unit*.1)*.6)
    assert not any('cache_read' in note for note in notes)


def test_missing_and_expired_prices():
    snap = snapshot()
    assert billing.estimate(dict(input=100), PriceSnapshot(model='unknown'), snap)[0] is None
    assert billing.estimate(dict(input=100), snap.agent, snap, model='unknown')[0] is None
    snap.agent = component('atlascloud', 'google/gemini-3.8-flash')
    assert billing.estimate(dict(input=100), snap.agent, snap, timestamp='2027-01-01T00:00:00Z')[0] is None


def test_invalid_cache_and_catalog_rejected():
    with pytest.raises(ValueError):
        billing.normalized_usage(dict(prompt_tokens=10, cached_tokens=20))
    with pytest.raises(ValidationError):
        RateTier(input=float('nan'), output=1)
    for value in (0, -1, float('inf')):
        with pytest.raises(ValidationError):
            PricingSettings(usd_cny=value)
    price = component('atlascloud', 'google/gemini-3.8-flash').price
    with pytest.raises(ValidationError):
        PricingSettings(prices=[price, price])


def test_failure_rerun_preserves_prior_spend_and_snapshot(tmp_path):
    item, root = job(tmp_path)
    path = trajectory(root)
    first = billing.refresh(item, force=True)
    assert first.billing.amount_cny > 0
    backup = Path(item.runtime.work_dir) / 'rerun-history' / 'rerun1' / 'task__a'
    backup.parent.mkdir(parents=True)
    shutil.move(str(path.parent.parent), str(backup))
    trajectory(root, session='s2')
    config = billing.pricing()
    config.usd_cny = 99
    store.pricing_doc.write(config)
    updated = billing.refresh(first, force=True)
    assert updated.billing.amount_cny == pytest.approx(first.billing.amount_cny*2)
    assert updated.billing.snapshot.usd_cny == 6.7195
    assert len(store.usage_charges.all()) == 2


def test_regrade_copied_agent_not_counted_twice_and_judge_separate(tmp_path):
    snap = snapshot(judge=component('atlascloud', 'anthropic/claude-opus-4.8-ccmax'))
    source, source_root = job(tmp_path, snap=snap)
    trajectory(source_root)
    original = billing.refresh(source, force=True)
    derived, root = job(tmp_path, name='two', snap=snap,
                        harbor_action=HarborAction(mode='regrade', source_job_id=source.id))
    shutil.copytree(source_root / 'task__a', root / 'task__a')
    trajectory(root, session='judge-new', phase='judge', model='anthropic/claude-opus-4.8-ccmax')
    updated = billing.refresh(derived, force=True)
    assert len(store.usage_charges.all()) == 2
    assert updated.billing.agent_cny == 0
    assert updated.billing.amount_cny == updated.billing.judge_cny
    summary = billing.summary([original, updated])
    assert summary['amount_cny'] == pytest.approx(original.billing.amount_cny+updated.billing.amount_cny)
    assert summary['projects'][0]['jobs'] == 2
    assert len(summary['models']) == 2


def test_no_usage_does_not_become_zero(tmp_path):
    item, _ = job(tmp_path)
    updated = billing.refresh(item, force=True)
    assert updated.billing.amount_cny is None
    assert updated.billing.status == 'pending'
    assert any('用量缺失' in note for note in updated.billing.warnings)


def test_official_model_in_atlas_trajectory_uses_explicit_mapping(tmp_path):
    item, root = job(tmp_path)
    trajectory(root, model='gpt-6.1-sol')
    updated = billing.refresh(item, force=True)
    assert updated.billing.amount_cny > 0
    assert updated.billing.by_model[0]['model'] == 'openai/gpt-6.1-sol-codex'


def test_symlinks_and_task_output_are_not_billable(tmp_path):
    item, root = job(tmp_path)
    trajectory(root, session='external')
    target = root / 'task__a' / 'agent' / 'trajectory.json'
    other = tmp_path / 'external.json'
    target.rename(other)
    target.symlink_to(other)
    trajectory(root / 'workspace', session='fake')
    updated = billing.refresh(item, force=True)
    assert updated.billing.amount_cny is None


def test_codex_live_repeated_token_events_and_atif_upgrade(tmp_path):
    item, root = job(tmp_path)
    path = root / 'task__a' / 'agent' / 'sessions' / 's1.jsonl'
    path.parent.mkdir(parents=True)
    total = dict(input_tokens=100000, output_tokens=10000, cached_input_tokens=20000, reasoning_output_tokens=3000)
    event = dict(type='event_msg', payload=dict(type='token_count', info=dict(total_token_usage=total, last_token_usage=total)))
    path.write_text('\n'.join(json.dumps(row) for row in [dict(type='session_meta', payload={'id': 's1'}), event, event]))
    first = billing.refresh(item, force=True)
    assert first.billing.requests == 1
    trajectory(root)
    second = billing.refresh(first, force=True)
    assert len(store.usage_charges.all()) == 1
    assert second.billing.amount_cny == first.billing.amount_cny


def test_claude_streaming_message_last_usage_and_ttl_cache(tmp_path):
    item, root = job(tmp_path, snap=snapshot(agent=component('atlascloud', 'anthropic/claude-opus-4.8-ccmax')))
    path = root / 'task__a' / 'agent' / 'sessions' / 'main.jsonl'
    path.parent.mkdir(parents=True)
    def event(out):
        return dict(type='assistant', sessionId='s1', message=dict(id='m1', model='claude-opus-4.8', usage=dict(input_tokens=100, output_tokens=out, cache_read_input_tokens=50, cache_creation_input_tokens=20)))
    path.write_text('\n'.join(json.dumps(row) for row in [event(1), event(10)]))
    first = billing.refresh(item, force=True)
    assert first.billing.requests == 1
    entry = store.usage_charges.all()[0]
    assert entry.tokens == dict(input=100, output=10, cache_read=50, cache_write=20, cache_write_long=0)
    second = billing.refresh(item)
    assert second.billing.amount_cny == first.billing.amount_cny


def test_pricing_permission_and_dashboard_scope(client, admin_headers, member_headers, tmp_path, monkeypatch):
    from backend.app.api import system
    monkeypatch.setattr(system.monitor, 'latest', lambda: {})
    monkeypatch.setattr(system.monitor, 'compact_history', lambda *_: [])
    monkeypatch.setattr(system, 'gpu_snapshot', lambda: {})
    response = client.get('/api/system/pricing', headers=member_headers)
    assert response.status_code == 200
    payload = response.json()
    payload['usd_cny'] = 7
    assert client.put('/api/system/pricing', headers=member_headers, json=payload).status_code == 403
    assert client.put('/api/system/pricing', headers=admin_headers, json=payload).status_code == 200
    first, root = job(tmp_path)
    trajectory(root)
    second, root2 = job(tmp_path, name='admin')
    second.owner_id, second.owner_email = user_id_of('admin@prt.local'), 'admin@prt.local'
    store.jobs.put(second)
    trajectory(root2, session='admin-session')
    billing.refresh(first, force=True)
    billing.refresh(second, force=True)
    member = client.get('/api/system/dashboard', headers=member_headers).json()
    admin = client.get('/api/system/dashboard', headers=admin_headers).json()
    assert member['billing']['jobs'] == 1
    assert admin['billing']['jobs'] == 2
    assert all(row['name'] != 'admin@prt.local' for row in member['billing']['people'])
    assert admin['billing']['amount_cny'] == pytest.approx(member['billing']['amount_cny']*2)


def test_bailian_cache_mode_and_region_are_explicit(tmp_path):
    item, _ = job(tmp_path)
    item.verify.agent = 'claude-code'
    item.verify.agent_env = {'ANTHROPIC_BASE_URL': 'https://dashscope.aliyuncs.com/apps/anthropic'}
    item.verify.model = 'qwen3.7-plus'
    snap = billing.capture(item)
    assert snap.agent.price.discount == .6
    assert snap.agent.cache_mode == 'explicit'
    value, _ = billing.estimate(dict(input=1000, output=100, cache_read=1000, cache_write=100), snap.agent, snap)
    assert value == pytest.approx((.001*2+.0001*8+.001*.2+.0001*2.5)*.6)
    item.verify.agent_env['ANTHROPIC_BASE_URL'] = 'https://dashscope-intl.aliyuncs.com/apps/anthropic'
    assert billing.capture(item).agent.price is None


def test_native_fallback_is_replaced_by_trajectory(tmp_path):
    item, root = job(tmp_path)
    trial = root / 'task__a'
    trial.mkdir()
    (trial / 'result.json').write_text(json.dumps(dict(id='native-id', agent_result={'n_input_tokens': 100000, 'n_output_tokens': 10000, 'n_cache_tokens': 20000})))
    first = billing.refresh(item, force=True)
    assert len(store.usage_charges.all()) == 1
    assert first.billing.amount_cny > 0
    trajectory(root)
    second = billing.refresh(first, force=True)
    assert len(store.usage_charges.all()) == 1
    assert second.billing.amount_cny > 0


def test_judge_usage_recorder_keeps_only_counters_and_copies_deduplicate(tmp_path, monkeypatch):
    import importlib.util
    from datetime import datetime, timezone
    from backend.app.config import settings
    spec = importlib.util.spec_from_file_location('billing_rewardkit_runner', settings.resources_dir / 'rewardkit_runner.py')
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    snap = snapshot(judge=component('atlascloud', 'anthropic/claude-opus-4.8-ccmax'))
    item, root = job(tmp_path, snap=snap)
    path = root / 'task__a' / 'verifier' / 'prt-model-usage.jsonl'
    monkeypatch.setattr(runner, 'USAGE_PATH', path)
    response = dict(id='request-judge-1', choices=[{'content': 'private response'}], usage=dict(prompt_tokens=1000, completion_tokens=100, prompt_tokens_details={'cached_tokens': 500}))
    kwargs = dict(model='anthropic/claude-opus-4.8-ccmax', api_key='secret-key', messages=[{'content': 'private request'}])
    runner.record_usage(kwargs, response, datetime.now(timezone.utc), None)
    runner.record_usage(kwargs, response, datetime.now(timezone.utc), None)
    text = path.read_text()
    assert 'private' not in text and 'secret-key' not in text and 'messages' not in text
    first = billing.refresh(item, force=True)
    assert first.billing.judge_cny > 0
    assert first.billing.requests == 1
    child, root2 = job(tmp_path, name='copy', snap=snap, harbor_action=HarborAction(mode='regrade', source_job_id=item.id))
    shutil.copytree(root / 'task__a', root2 / 'task__a')
    second = billing.refresh(child, force=True)
    assert len(store.usage_charges.all()) == 1
    assert second.billing.amount_cny is None  # no new recorded requests


def test_usage_callback_registration_is_idempotent_and_async_safe(tmp_path, monkeypatch):
    import asyncio
    import importlib.util
    import sys
    from types import ModuleType
    from backend.app.config import settings
    spec = importlib.util.spec_from_file_location('billing_rewardkit_callback', settings.resources_dir / 'rewardkit_runner.py')
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    module = ModuleType('litellm')
    module.callbacks = []
    integration = ModuleType('litellm.integrations.custom_logger')
    integration.CustomLogger = type('CustomLogger', (), {})
    monkeypatch.setitem(sys.modules, 'litellm', module)
    monkeypatch.setitem(sys.modules, 'litellm.integrations.custom_logger', integration)
    path = tmp_path / 'usage.jsonl'
    monkeypatch.setattr(runner, 'USAGE_PATH', path)
    runner.install_usage_recorder()
    runner.install_usage_recorder()
    assert len(module.callbacks) == 1
    callback = module.callbacks[0]
    response = dict(id='r1', usage=dict(prompt_tokens=100, completion_tokens=10))
    asyncio.run(callback.async_log_success_event({'model': 'test'}, response, None, None))
    assert json.loads(path.read_text())['metrics']['prompt_tokens'] == 100


def test_litellm_transport_prefixes_resolve_only_exact_server_ids():
    snap = snapshot()
    for model in ['gpt-6.1-sol', 'gpt-6.1-sol-codex', 'openai/gpt-6.1-sol-codex', 'openai/openai/gpt-6.1-sol-codex']:
        value, _ = billing.estimate(dict(input=1000, output=100), snap.agent, snap, model=model)
        assert value == pytest.approx((.001*2+.0001*10)*.15*6.7195)
    assert billing.estimate(dict(input=1000), snap.agent, snap, model='openai/gpt-6.1-sol-codex-pro')[0] is None


def test_delete_archives_spend_and_keeps_person_visibility(tmp_path):
    item, root = job(tmp_path)
    trajectory(root)
    first = billing.refresh(item, force=True)
    billing.archive(first)
    store.jobs.delete(item.id)
    dev = store.users.get(item.owner_id)
    admin = store.users.get(user_id_of('admin@prt.local'))
    assert billing.summary([], actor=dev)['amount_cny'] == first.billing.amount_cny
    assert billing.summary([], actor=admin)['amount_cny'] == first.billing.amount_cny
    stranger = dev.model_copy(update={'id': 'stranger'})
    assert billing.summary([], actor=stranger)['amount_cny'] is None


def test_one_invalid_trial_does_not_hide_other_spend(tmp_path):
    item, root = job(tmp_path)
    trajectory(root, prompt=100, cache=200)
    other = root / 'task__b' / 'agent'
    other.mkdir(parents=True)
    source = root / 'task__a' / 'agent' / 'trajectory.json'
    data = json.loads(source.read_text())
    data['session_id'] = 'valid-session'
    data['steps'][0]['metrics']['prompt_tokens'] = 1000
    (other / 'trajectory.json').write_text(json.dumps(data))
    updated = billing.refresh(item, force=True)
    assert updated.billing.amount_cny > 0
    assert updated.billing.status == 'partial'
    assert any('无法读取' in note for note in updated.billing.warnings)


def test_claude_subagent_session_is_not_billed_twice_after_atif(tmp_path):
    snap = snapshot(agent=component('atlascloud', 'anthropic/claude-opus-4.8-ccmax'))
    item, root = job(tmp_path, snap=snap)
    area = root / 'task__a' / 'agent'
    main = area / 'sessions' / 'parent.jsonl'
    child = area / 'sessions' / 'parent' / 'subagents' / 'child.jsonl'
    main.parent.mkdir(parents=True)
    child.parent.mkdir(parents=True)
    def event(session, msg, time):
        return dict(type='assistant', sessionId=session, timestamp=time,
                    message=dict(id=msg, model='claude-opus-4.8', usage=dict(input_tokens=100, output_tokens=10)))
    main.write_text(json.dumps(event('parent', 'm1', '2026-10-22T00:00:00Z')))
    child.write_text(json.dumps(event('child', 'm2', '2026-10-22T00:01:00Z')))
    first = billing.refresh(item, force=True)
    assert first.billing.requests == 2
    path = trajectory(root, session='parent', model='claude-opus-4.8', prompt=100, output=10, cache=0)
    data = json.loads(path.read_text())
    data['steps'].append(dict(data['steps'][0], step_id=2))
    path.write_text(json.dumps(data))
    second = billing.refresh(first, force=True)
    assert second.billing.amount_cny == first.billing.amount_cny
    assert len(store.usage_charges.all()) == 1


def claude_session(root, *messages, name='main.jsonl'):
    path = root / 'task__a' / 'agent' / 'sessions' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(json.dumps(dict(type='assistant', sessionId='s1', timestamp=time,
                    message=dict(id=msg, model=model, usage=dict(input_tokens=1000, output_tokens=100))))
                    for msg, model, time in messages))
    return path


def test_zero_usage_trajectory_falls_back_to_session_logs(tmp_path):
    item, root = job(tmp_path, snap=snapshot(agent=component('atlascloud', 'anthropic/claude-opus-4.8-ccmax')))
    claude_session(root, ('m1', 'claude-opus-4.8', '2026-10-22T00:00:00Z'), ('m2', '<synthetic>', '2026-10-22T00:01:00Z'))
    trajectory(root, session='s1', model='claude-opus-4.8', prompt=0, output=0, cache=0)
    updated = billing.refresh(item, force=True)
    assert updated.billing.amount_cny > 0
    assert updated.billing.requests == 1  # <synthetic> messages are local and never billed
    assert not any('价格待配置' in note or '用量缺失' in note for note in updated.billing.warnings)


def test_trajectory_with_usage_still_wins_over_sessions(tmp_path):
    item, root = job(tmp_path, snap=snapshot(agent=component('atlascloud', 'anthropic/claude-opus-4.8-ccmax')))
    claude_session(root, ('m1', 'claude-opus-4.8', '2026-10-22T00:00:00Z'), ('m2', 'claude-opus-4.8', '2026-10-22T00:01:00Z'))
    trajectory(root, session='s1', model='claude-opus-4.8', prompt=1000, output=100, cache=0)
    updated = billing.refresh(item, force=True)
    assert updated.billing.requests == 1


def test_synthetic_rows_already_in_ledger_are_removed(tmp_path):
    snap = snapshot(agent=component('atlascloud', 'anthropic/claude-opus-4.8-ccmax'))
    item, root = job(tmp_path, snap=snap)
    store.usage_charges.put(UsageCharge(id='old', job_id=item.id, phase='agent', model='<synthetic>', vendor='atlascloud',
                                        tokens=dict(input=0, output=0), requests=1, snapshot=snap))
    claude_session(root, ('m1', 'claude-opus-4.8', '2026-10-22T00:00:00Z'))
    updated = billing.refresh(item, force=True)
    assert [row.model for row in store.usage_charges.all()] == ['anthropic/claude-opus-4.8-ccmax']
    assert updated.billing.status == 'estimated'


def test_unreadable_session_keeps_other_usage_and_explains(tmp_path, monkeypatch):
    item, root = job(tmp_path, snap=snapshot(agent=component('atlascloud', 'anthropic/claude-opus-4.8-ccmax')))
    claude_session(root, ('m1', 'claude-opus-4.8', '2026-10-22T00:00:00Z'))
    locked = claude_session(root, ('m2', 'claude-opus-4.8', '2026-10-22T00:01:00Z'), name='locked.jsonl')
    original = Path.read_text

    def read_text(self, *args, **kwargs):
        if self == locked:
            raise PermissionError(13, 'Permission denied', str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read_text)
    updated = billing.refresh(item, force=True)
    assert updated.billing.amount_cny > 0
    assert updated.billing.status == 'partial'
    assert any('无读取权限' in note for note in updated.billing.warnings)


@pytest.mark.parametrize('timestamp,factor', [
    ('2026-10-22T02:00:00Z', 1),      # Thu 10:00 Beijing: peak
    ('2026-10-22T04:30:00Z', 0.5),    # Thu 12:30 Beijing: lunch break
    ('2026-10-22T10:00:00Z', 0.5),    # Thu 18:00 Beijing: evening
    ('2026-10-24T02:00:00Z', 0.5),    # Saturday
    ('2026-10-02T02:00:00Z', 0.5),    # National Day holiday
    ('2026-10-22T10:00:00+08:00', 1), # explicit offset
])
def test_deepseek_off_peak_by_beijing_time(timestamp, factor):
    snap = snapshot(agent=component('deepseek', 'deepseek-flash'))
    peak, _ = billing.estimate(dict(input=1_000_000, output=1_000_000), snap.agent, snap, timestamp='2026-10-22T02:00:00Z')
    assert peak == pytest.approx(10)
    value, notes = billing.estimate(dict(input=1_000_000, output=1_000_000), snap.agent, snap, timestamp=timestamp)
    assert value == pytest.approx(10 * factor)
    assert not notes


def test_deepseek_unknown_time_or_year_uses_peak_and_old_snapshots_upgrade():
    snap = snapshot(agent=component('deepseek', 'deepseek-v4-pro'))
    for kwargs in (dict(timestamp=''), dict(timestamp='2027-01-02T02:00:00Z'), dict(aggregate=True, timestamp='2026-10-24T02:00:00Z')):
        value, notes = billing.estimate(dict(input=1_000_000), snap.agent, snap, **kwargs)
        assert value == pytest.approx(9)
        assert any('高峰时段价格上限' in note for note in notes)
    legacy = snap.model_copy(deep=True)
    legacy.agent.price = legacy.agent.price.model_copy(update={'peak_schedule': '', 'off_peak_factor': None})
    assert billing.estimate(dict(input=1_000_000), legacy.agent, legacy, timestamp='2026-10-24T02:00:00Z')[0] == pytest.approx(4.5)
    custom = legacy.model_copy(deep=True)
    custom.agent.price.tiers[0].input = 8
    assert billing.estimate(dict(input=1_000_000), custom.agent, custom, timestamp='2026-10-24T02:00:00Z')[0] == pytest.approx(8)


def test_peak_schedule_requires_factor():
    with pytest.raises(ValidationError):
        ModelPrice(vendor='x', model='y', source='s', tiers=[RateTier(input=1, output=1)], peak_schedule='cn-workday')
