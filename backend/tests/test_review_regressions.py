"""Regression coverage for shared-job privacy and lifecycle concurrency."""

import asyncio
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi import HTTPException

from backend.app.builders.base import BuildResult
from backend.app.models import DeploymentCreate, Job, JobStatus, User, VerifyConfig
from backend.app.services import dataset_service, deployment_service, job_service, runner
from backend.app.services.scheduler import scheduler
from backend.app.store import store

from .conftest import project_id


@pytest.fixture
def no_dispatch(monkeypatch):
    monkeypatch.setattr(scheduler, "tick", lambda: None)
    monkeypatch.setattr(scheduler, "dispatch", lambda: None)


def test_job_responses_mask_secrets_but_keep_stored_config(client, admin_headers, member_headers, no_dispatch, tmp_path):
    # 成员提交才会进入待审核，下方 /review 的响应也要覆盖到
    response = client.post('/api/jobs', headers=member_headers, json={
        'name': 'private configuration', 'type': 'verify', 'project_id': project_id(),
        'resources': {'n_gpus': 0, 'env': {'ACCESS_TOKEN': 'resource-secret-value', 'THREADS': '4'}},
        'verify': {'agent': 'nop', 'tasks_path': str(tmp_path),
                   'agent_env': {'OPENAI_API_KEY': 'agent-secret-value'},
                   'verifier_env': {'EVAL_API_KEY': 'judge-secret-value'},
                   'extra_args': {'nested': {'authorization': 'Bearer private-value'}}},
    })
    assert response.status_code == 201, response.text
    job_id = response.json()['id']
    try:
        responses = [response,
                     client.get(f'/api/jobs/{job_id}', headers=member_headers),
                     client.get('/api/jobs', headers=member_headers, params={'q': job_id}),
                     client.post(f'/api/jobs/{job_id}/resubmit', headers=admin_headers),
                     client.post(f'/api/jobs/{job_id}/review', headers=admin_headers, json={'approve': True}),
                     client.post(f'/api/jobs/{job_id}/cancel', headers=admin_headers)]
        for result in responses:
            assert result.status_code in (200, 201), result.text
            for secret in ('resource-secret-value', 'agent-secret-value', 'judge-secret-value', 'Bearer private-value'):
                assert secret not in result.text
        saved = store.jobs.get(job_id)
        assert saved.resources.env['ACCESS_TOKEN'] == 'resource-secret-value'
        assert saved.verify.agent_env['OPENAI_API_KEY'] == 'agent-secret-value'
        assert saved.resources.env['THREADS'] == '4'
    finally:
        store.jobs.delete(job_id)
        for cloned in store.jobs.find(lambda j: j.parent_job_id == job_id):
            store.jobs.delete(cloned.id)


@pytest.mark.parametrize('prepare_fails', [False, True])
def test_cancel_during_prepare_never_spawns_or_becomes_failed(tmp_path, monkeypatch, no_dispatch, prepare_fails):
    from backend.app.services.scheduler import Scheduler
    owner = User(email='cancel-probe@example.com', password_hash='unused', display_name='Probe')
    job = Job(name='cancel preparing', type='verify', framework='harbor', owner_id=owner.id,
              status=JobStatus.QUEUED, resources={'n_gpus': 0}, verify=VerifyConfig(agent='nop'))
    store.jobs.put(job)
    entered, release = threading.Event(), threading.Event()
    spawned = []
    def prepare(_job):
        entered.set()
        assert release.wait(3)
        if prepare_fails:
            raise ValueError('prepare failed after cancellation')
        return BuildResult(command=['unused'], cwd=str(tmp_path)), tmp_path, tmp_path
    def popen(*args, **kwargs):
        spawned.append(True)
        raise OSError('unexpected process launch')
    monkeypatch.setattr(runner, 'prepare', prepare)
    monkeypatch.setattr(runner.subprocess, 'Popen', popen)
    local = Scheduler()
    try:
        with ThreadPoolExecutor() as pool:
            future = pool.submit(local.dispatch)
            assert entered.wait(3)
            job_service.cancel_job(job.id, actor=owner)
            release.set()
            future.result(timeout=3)
        assert spawned == []
        assert store.jobs.get(job.id).status == JobStatus.CANCELLED
    finally:
        release.set()
        store.jobs.delete(job.id)


@pytest.mark.parametrize("resource", ["gpu", "port"])
def test_concurrent_deployments_reserve_resources_atomically(tmp_path, monkeypatch, no_dispatch, resource):
    monkeypatch.setattr(scheduler, 'total_gpus', 1)
    monkeypatch.setattr(deployment_service, '_port_in_use', lambda port: False)
    monkeypatch.setattr(deployment_service, '_launch', lambda item: item)
    first, second = threading.Event(), threading.Event()
    allocate = deployment_service._allocate_gpus
    count = 0
    def slow_allocate(n):
        nonlocal count
        count += 1
        result = allocate(n)
        if count == 1:
            first.set()
            second.wait(.3)
        else:
            second.set()
        return result
    monkeypatch.setattr(deployment_service, '_allocate_gpus', slow_allocate)
    owner = User(email='gpu-probe@example.com', password_hash='unused', display_name='Probe')
    def create(port):
        try:
            return deployment_service.create_deployment(DeploymentCreate(
                name='GPU reservation', source='path', model_path=str(tmp_path), port=port, n_gpus=1 if resource == 'gpu' else 0), owner=owner)
        except HTTPException as exc:
            return exc.status_code
    try:
        with ThreadPoolExecutor() as pool:
            one = pool.submit(create, 18701)
            assert first.wait(3)
            two = pool.submit(create, 18702 if resource == 'gpu' else 18701)
            results = [one.result(timeout=3), two.result(timeout=3)]
        assert sum(result == 409 for result in results) == 1
        assert [result.gpu_ids for result in results if result != 409] == ([[0]] if resource == 'gpu' else [[]])
    finally:
        for item in store.deployments.find(lambda d: d.owner_id == owner.id):
            store.deployments.delete(item.id)


def test_duplicate_dataset_names_keep_every_file(tmp_path):
    paths = ['a/train.json', 'b/train.json', 'a/train.jsonl', 'a_b/train.json', 'a/b_train.json']
    for relative in paths:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {'instruction': relative, 'output': 'answer'}
        path.write_text(json.dumps(record if path.suffix == '.jsonl' else [record]))
    from backend.app.models import DatasetKind
    stats, files, entries = dataset_service._validate_json_dataset(tmp_path, DatasetKind.SFT_ALPACA)
    assert len(entries) == stats.n_files == len(files) == len(paths)
    assert {entry['file_name'] for entry in entries.values()} == {str((tmp_path / p).resolve()) for p in paths}


@pytest.mark.parametrize('replace', [False, True])
def test_upload_processing_runs_outside_event_loop(client, member_headers, monkeypatch, no_dispatch, replace):
    from backend.app.main import app
    from backend.app.models import Dataset
    owner = store.users.first(lambda user: user.email == 'dev@prt.local')
    dataset = Dataset(name='thread probe', kind='sft_alpaca', owner_id=owner.id)
    observed = []
    def ingest(*args, **kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            observed.append('worker')
        else:
            observed.append('event_loop')
        assert kwargs['stream'].read() == b'probe'
        return dataset
    monkeypatch.setattr(dataset_service, 'replace_file' if replace else 'create_from_upload', ingest)
    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as ac:
            response = await ac.request('PUT' if replace else 'POST',
                f'/api/datasets/{dataset.id}/file' if replace else '/api/datasets',
                headers=member_headers, files={'file': ('probe.zip', io.BytesIO(b'probe'), 'application/zip')},
                data={'name': dataset.name, 'kind': dataset.kind.value})
            assert response.status_code in (200, 201), response.text
    asyncio.run(request())
    assert observed == ['worker']


def test_cancel_during_spawn_sees_registered_process(tmp_path, monkeypatch, no_dispatch):
    from types import SimpleNamespace
    owner = User(email='spawn-probe@example.com', password_hash='unused', display_name='Probe')
    job = Job(name='cancel spawn', type='verify', framework='harbor', owner_id=owner.id,
              status=JobStatus.PREPARING, resources={'n_gpus': 0}, verify=VerifyConfig(agent='nop'))
    store.jobs.put(job)
    entered, release, cancelling = threading.Event(), threading.Event(), threading.Event()
    terminated = []
    def spawn(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return SimpleNamespace(pid=987654)
    def supervise(job_id, process, handle):
        handle.close()
    def terminate(current):
        terminated.append((current.runtime.pid, current.runtime.pgid, current.runtime.cancel_requested))
        store.jobs.update(current.id, lambda item: setattr(item, 'status', JobStatus.CANCELLED))
        return True
    def cancel():
        cancelling.set()
        return job_service.cancel_job(job.id, actor=owner)
    monkeypatch.setattr(runner, 'prepare', lambda _: (BuildResult(command=['unused'], cwd=str(tmp_path)), tmp_path, tmp_path))
    monkeypatch.setattr(runner.subprocess, 'Popen', spawn)
    monkeypatch.setattr(runner, '_supervise', supervise)
    monkeypatch.setattr(runner, 'terminate', terminate)
    try:
        with ThreadPoolExecutor() as pool:
            launched = pool.submit(runner.launch, job)
            assert entered.wait(3)
            cancelled = pool.submit(cancel)
            assert cancelling.wait(3)
            release.set()
            launched.result(timeout=3)
            assert cancelled.result(timeout=3).status == JobStatus.CANCELLED
        assert terminated == [(987654, 987654, True)]
    finally:
        release.set()
        runner._PROCESSES.pop(job.id, None)
        store.jobs.delete(job.id)


def test_training_reservation_blocks_deployment_without_holding_lock_during_prepare(tmp_path, monkeypatch, no_dispatch):
    from backend.app.services.scheduler import Scheduler
    owner = User(email='training-gpu@example.com', password_hash='unused', display_name='Probe')
    job = Job(name='training reserves gpu', type='verify', framework='harbor', owner_id=owner.id,
              status=JobStatus.QUEUED, resources={'n_gpus': 1}, verify=VerifyConfig(agent='nop'))
    store.jobs.put(job)
    entered, release = threading.Event(), threading.Event()
    def prepare(_job):
        entered.set()
        assert release.wait(3)
        raise ValueError('test stops preparation')
    monkeypatch.setattr(runner, 'prepare', prepare)
    monkeypatch.setattr(scheduler, 'total_gpus', 1)
    monkeypatch.setattr(deployment_service, '_port_in_use', lambda port: False)
    monkeypatch.setattr(deployment_service, '_launch', lambda item: item)
    local = Scheduler()
    local.total_gpus = 1
    try:
        with ThreadPoolExecutor() as pool:
            launched = pool.submit(local.dispatch)
            assert entered.wait(3)
            try:
                deployment = pool.submit(deployment_service.create_deployment,
                    DeploymentCreate(name='contender', source='path', model_path=str(tmp_path), port=18703, n_gpus=1),
                    owner=owner)
                with pytest.raises(HTTPException) as error:
                    deployment.result(timeout=1)
                assert error.value.status_code == 409
            finally:
                job_service.cancel_job(job.id, actor=owner)
                release.set()
            launched.result(timeout=3)
        assert store.jobs.get(job.id).status == JobStatus.CANCELLED
        assert scheduler.free_gpu_ids() == [0]
    finally:
        release.set()
        store.jobs.delete(job.id)
        for item in store.deployments.find(lambda d: d.owner_id == owner.id):
            store.deployments.delete(item.id)


def test_stale_queued_snapshot_cannot_revive_cancelled_job(tmp_path, monkeypatch, no_dispatch):
    from backend.app.services.scheduler import Scheduler
    owner = User(email='stale-queue@example.com', password_hash='unused', display_name='Probe')
    job = Job(name='cancel queued snapshot', type='verify', framework='harbor', owner_id=owner.id,
              status=JobStatus.QUEUED, resources={'n_gpus': 0}, verify=VerifyConfig(agent='nop'))
    store.jobs.put(job)
    local = Scheduler()
    def free_gpus():
        job_service.cancel_job(job.id, actor=owner)
        return []
    monkeypatch.setattr(local, 'free_gpu_ids', free_gpus)
    monkeypatch.setattr(runner, 'launch', lambda job: pytest.fail('cancelled task was launched'))
    try:
        local.dispatch()
        assert store.jobs.get(job.id).status == JobStatus.CANCELLED
    finally:
        store.jobs.delete(job.id)


def test_failed_deployment_releases_reservations(tmp_path, monkeypatch, no_dispatch):
    monkeypatch.setattr(scheduler, 'total_gpus', 1)
    monkeypatch.setattr(deployment_service, '_port_in_use', lambda port: False)
    owner = User(email='failed-gpu@example.com', password_hash='unused', display_name='Probe')
    payload = DeploymentCreate(name='retry deployment', source='path', model_path=str(tmp_path), port=18704, n_gpus=1)
    def fail(_item):
        raise OSError('test startup failure')
    monkeypatch.setattr(deployment_service, '_launch', fail)
    try:
        with pytest.raises(HTTPException):
            deployment_service.create_deployment(payload, owner=owner)
        assert scheduler.free_gpu_ids() == [0]
        monkeypatch.setattr(deployment_service, '_launch', lambda item: item)
        assert deployment_service.create_deployment(payload, owner=owner).gpu_ids == [0]
    finally:
        for item in store.deployments.find(lambda d: d.owner_id == owner.id):
            store.deployments.delete(item.id)


def test_edit_rerun_preserves_credentials_only_for_source_controller(client, admin_headers, member_headers, no_dispatch, tmp_path):
    payload = {'name': 'edit secret', 'type': 'verify', 'project_id': project_id(),
               'resources': {'n_gpus': 0}, 'verify': {'agent': 'nop', 'tasks_path': str(tmp_path),
               'agent_env': {'OPENAI_API_KEY': 'original-key-for-rerun'}}}
    source = client.post('/api/jobs', headers=admin_headers, json=payload).json()
    try:
        payload['verify'] = source['verify']
        payload['parent_job_id'] = source['id']
        assert payload['verify']['agent_env']['OPENAI_API_KEY'] == '***'
        assert client.post('/api/jobs', headers=member_headers, json=payload).status_code == 403
        assert client.post(f"/api/jobs/{source['id']}/resubmit", headers=member_headers).status_code == 403
        cloned = client.post('/api/jobs', headers=admin_headers, json=payload)
        assert cloned.status_code == 201, cloned.text
        assert 'original-key-for-rerun' not in cloned.text
        assert store.jobs.get(cloned.json()['id']).verify.agent_env['OPENAI_API_KEY'] == 'original-key-for-rerun'
        payload['parent_job_id'] = None
        assert client.post('/api/jobs', headers=admin_headers, json=payload).status_code == 400
    finally:
        for job in store.jobs.find(lambda j: j.id == source['id'] or j.parent_job_id == source['id']):
            store.jobs.delete(job.id)
