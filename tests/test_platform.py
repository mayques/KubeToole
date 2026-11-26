from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
import database
import jobs
import policy
import quotas
import storage
from errors import Rejected
from cluster import manifest


def launch(api):
    client, identity, cluster = api
    response = client.post('/tools/probe/run', json={'inputs': {}})
    assert response.status_code == 202, response.text
    job_id = response.json()['job']['id']
    headers = {'Authorization': 'Bearer ' + cluster.tokens[job_id]}
    return job_id, headers


def claim(api, job_id, headers):
    response = api[0].post(f'/internal/jobs/{job_id}/claim', headers=headers)
    assert response.status_code == 200, response.text


def test_migration_preserves_history(blank):
    with database.connection() as conn:
        conn.execute('CREATE TABLE calculations(id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,user_id TEXT,expression TEXT,result DOUBLE PRECISION,created_at TIMESTAMPTZ DEFAULT NOW())')
        conn.execute("INSERT INTO calculations(user_id,expression,result) VALUES ('existing','2+2',4)")
    database.migrate()
    database.migrate()
    with database.connection() as conn:
        assert conn.execute('SELECT result FROM calculations').fetchone()['result'] == 4
        assert conn.execute('SELECT used_bytes FROM accounts').fetchone()['used_bytes'] == 11
        assert conn.execute('SELECT count(*) AS n FROM schema_migrations').fetchone()['n'] == 1


def test_calculator_contract_isolation_and_usage(api):
    client, identity, _ = api
    assert client.post('/calculate', json={'expression': '12+4*3'}).json() == {'result': 24}
    assert client.get('/history').json()['calculations'][0]['expression'] == '12+4*3'
    assert client.get('/account/usage').json()['tools']['calculator']['savedBytes'] == 14
    identity.user = 'bob'
    assert client.get('/history').json() == {'calculations': []}
    assert client.get('/account/usage').json()['storage']['usedBytes'] == 0
    assert client.get('/tools').json()['tools'][0]['id'] == 'calculator'
    assert client.post('/tools/calculator/run', json={'inputs': {'expression': '-(2+3)/2'}}).json()['result'] == {'result': -2.5}


@pytest.mark.parametrize('expression', ['1/0', '__import__("os")', 'True', '1e999', '2**99', '1+' * 400 + '1'])
def test_invalid_calculation_does_not_save(api, expression):
    client, _, _ = api
    assert client.post('/calculate', json={'expression': expression}).status_code == 400
    assert client.get('/history').json()['calculations'] == []


def test_payload_is_bounded(api):
    assert api[0].post('/tools/probe/run', content='x' * 65537).status_code == 413
    assert api[0].post('/tools/probe/run', json={'inputs': {}, 'cpu': 99}).status_code == 422
    assert api[0].post('/tools/unknown/run', json={'inputs': {}}).status_code == 404


def test_admission_is_atomic(db):
    def admit(index):
        try:
            with quotas.transaction() as conn:
                quotas.account(conn, f'user-{index}')
            return True
        except Rejected:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(admit, range(8))) == 6
    with quotas.transaction() as conn:
        assert quotas.account(conn, 'owner')['user_id'] == 'owner'
        assert quotas.limit('owner') == 50_000_000_000
        assert quotas.limit('user-0') == 25_000_000_000


def test_global_slots_are_atomic(compute):
    def start(index):
        try:
            return jobs.launch(f'user-{index}', 'probe', {})['id']
        except Rejected as error:
            assert error.status == 429
    with ThreadPoolExecutor(max_workers=6) as pool:
        assert len([x for x in pool.map(start, range(6)) if x]) == 2
    assert compute.creates == 2


def test_one_job_per_user_and_no_duplicate_execution(api):
    job_id, headers = launch(api)
    assert api[0].post('/tools/probe/run', json={'inputs': {}}).status_code == 429
    claim(api, job_id, headers)
    assert api[0].post(f'/internal/jobs/{job_id}/claim', headers=headers).status_code == 409


def test_cancel_ownership_idempotency_and_confirmed_stop(api):
    client, identity, cluster = api
    job_id, headers = launch(api)
    claim(api, job_id, headers)
    identity.user = 'bob'
    assert client.post(f'/jobs/{job_id}/cancel').status_code == 404
    assert client.get(f'/jobs/{job_id}').status_code == 404
    identity.user = 'alice'
    for _ in range(2):
        assert client.post(f'/jobs/{job_id}/cancel').json()['state'] == 'cancelling'
    jobs.reconcile()
    assert client.get('/account/usage').json()['cpu']['usedSlots'] == 1
    assert client.post('/tools/probe/run', json={'inputs': {}}).status_code == 429
    assert client.post(f'/internal/jobs/{job_id}/files', json={'name': 'x', 'kind': 'output'}, headers=headers).status_code == 409
    cluster.stop(job_id)
    jobs.reconcile()
    assert client.get(f'/jobs/{job_id}').json()['state'] == 'cancelled'
    assert client.get('/account/usage').json()['cpu']['usedSlots'] == 0
    assert client.post(f'/jobs/{job_id}/cancel').json()['state'] == 'cancelled'
    assert cluster.creates == 1


def test_cluster_outage_does_not_release_slot(api):
    job_id, _ = launch(api)
    api[2].offline = True
    jobs.reconcile()
    assert api[0].get('/account/usage').json()['cpu']['usedSlots'] == 1


def test_uncertain_create_never_retries_or_releases_running_worker(api):
    api[2].uncertain = True
    job_id, _ = launch(api)
    jobs.reconcile()
    jobs.reconcile()
    assert api[2].creates == 1
    assert api[0].get(f'/jobs/{job_id}').json()['state'] == 'cancelling'


def test_timeout_keeps_slot_until_stop(api):
    job_id, _ = launch(api)
    with quotas.transaction() as conn:
        conn.execute("UPDATE jobs SET created_at=NOW()-INTERVAL '46 minutes' WHERE id=%s", (job_id,))
    jobs.reconcile()
    assert api[0].get(f'/jobs/{job_id}').json()['state'] == 'cancelling'
    api[2].stop(job_id)
    jobs.reconcile()
    row = api[0].get(f'/jobs/{job_id}').json()
    assert row['state'] == 'failed' and row['error'] == 'Job deadline exceeded'


def create_file(api, job_id, headers, kind='output'):
    response = api[0].post(f'/internal/jobs/{job_id}/files', headers=headers, json={'name': 'result.bin', 'kind': kind})
    assert response.status_code == 200, response.text
    return response.json()['id']


def test_parallel_reservations_cannot_overrun_account(api, monkeypatch):
    monkeypatch.setattr(policy, 'USER_BYTES', 100)
    job_id, headers = launch(api)
    claim(api, job_id, headers)
    files = [create_file(api, job_id, headers) for _ in range(2)]
    def reserve(file_id):
        return api[0].post(f'/internal/jobs/{job_id}/files/{file_id}/reserve', headers=headers, json={'size': 80}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, files)) == [200, 413]
    assert api[0].get('/account/usage').json()['storage']['reservedBytes'] == 80


def test_scratch_and_disk_guards(api, monkeypatch):
    job_id, headers = launch(api)
    claim(api, job_id, headers)
    file_id = create_file(api, job_id, headers, 'temp')
    assert api[0].post(f'/internal/jobs/{job_id}/files/{file_id}/reserve', headers=headers, json={'size': policy.SCRATCH_BYTES + 1}).status_code == 413
    monkeypatch.setattr(quotas.shutil, 'disk_usage', lambda path: SimpleNamespace(free=policy.DATA_FREE_BYTES))
    assert api[0].post(f'/internal/jobs/{job_id}/files/{file_id}/reserve', headers=headers, json={'size': 1}).status_code == 507


def test_unreserved_and_cross_job_writes_are_rejected(api):
    job_id, headers = launch(api)
    claim(api, job_id, headers)
    file_id = create_file(api, job_id, headers)
    assert api[0].post(f'/internal/jobs/{job_id}/files/{file_id}/write?offset=0', headers={**headers, 'Content-Type': 'application/octet-stream'}, content=b'abc').status_code == 413
    api[1].user = 'bob'
    other, other_headers = launch(api)
    claim(api, other, other_headers)
    assert api[0].post(f'/internal/jobs/{other}/files/{file_id}/reserve', headers=other_headers, json={'size': 3}).status_code == 404
    assert api[0].post(f'/internal/jobs/{other}/claim', headers=headers).status_code == 401


def save_output(api, job_id, headers):
    file_id = create_file(api, job_id, headers)
    assert api[0].post(f'/internal/jobs/{job_id}/files/{file_id}/reserve', headers=headers, json={'size': 3}).status_code == 200
    assert api[0].post(f'/internal/jobs/{job_id}/files/{file_id}/write?offset=0', headers={**headers, 'Content-Type': 'application/octet-stream'}, content=b'abc').status_code == 200
    assert api[0].post(f'/internal/jobs/{job_id}/files/{file_id}/complete', headers=headers).status_code == 200
    return file_id


def test_success_publishes_only_after_stop_and_preserves_on_cancel(api):
    job_id, headers = launch(api)
    claim(api, job_id, headers)
    file_id = save_output(api, job_id, headers)
    assert api[0].get(f'/artifacts/{file_id}').status_code == 404
    assert api[0].post(f'/internal/jobs/{job_id}/finish', headers=headers, json={'outcome': 'succeeded', 'result': {'value': 24}}).status_code == 200
    jobs.reconcile()
    assert api[0].get(f'/jobs/{job_id}').json()['state'] == 'running'
    assert api[0].get(f'/jobs/{job_id}').json()['result'] is None
    api[2].stop(job_id, True)
    jobs.reconcile()
    jobs.reconcile()
    assert api[0].get(f'/artifacts/{file_id}').content == b'abc'
    total = api[0].get('/account/usage').json()['storage']
    assert total['usedBytes'] == 2 + 3 + len(quotas.encoded({'value': 24}))
    assert total['reservedBytes'] == 0
    assert api[0].post(f'/jobs/{job_id}/cancel').json()['state'] == 'succeeded'
    assert api[0].get(f'/artifacts/{file_id}').content == b'abc'
    api[1].user = 'bob'
    assert api[0].get(f'/artifacts/{file_id}').status_code == 404


def test_failed_job_cleans_partial_files(api):
    job_id, headers = launch(api)
    claim(api, job_id, headers)
    save_output(api, job_id, headers)
    api[2].stop(job_id, False)
    jobs.reconcile()
    assert not storage.job_dir(job_id).exists()
    assert api[0].get('/account/usage').json()['storage']['reservedBytes'] == 0
    assert api[0].get(f'/jobs/{job_id}').json()['state'] == 'failed'


def test_cleanup_is_repeatable_after_transaction_failure(api, monkeypatch):
    job_id, headers = launch(api)
    claim(api, job_id, headers)
    save_output(api, job_id, headers)
    api[0].post(f'/internal/jobs/{job_id}/finish', headers=headers, json={'outcome': 'succeeded', 'result': 1})
    api[2].stop(job_id, True)
    original = storage.finalize
    def crash(conn, job, success):
        original(conn, job, success)
        raise OSError('simulated crash after filesystem cleanup')
    monkeypatch.setattr(storage, 'finalize', crash)
    jobs.reconcile()
    assert api[0].get(f'/jobs/{job_id}').json()['state'] == 'running'
    monkeypatch.setattr(storage, 'finalize', original)
    jobs.reconcile()
    assert api[0].get(f'/jobs/{job_id}').json()['state'] == 'succeeded'
    assert api[0].get('/account/usage').json()['storage']['usedBytes'] == 6


def test_manifest_enforces_policy_and_worker_isolation():
    body = manifest(UUID('00000000-0000-0000-0000-000000000001'), 'test-token')
    spec = body['spec']
    assert spec['backoffLimit'] == 0 and spec['activeDeadlineSeconds'] == 2700
    assert spec['podReplacementPolicy'] == 'Failed'
    pod = spec['template']['spec']
    assert pod['restartPolicy'] == 'Never'
    assert pod['automountServiceAccountToken'] is False
    container = pod['containers'][0]
    assert container['resources']['limits']['cpu'] == '1250m'
    assert container['resources']['limits']['memory'] == '3Gi'
    assert container['resources']['requests'] == container['resources']['limits']
    assert container['securityContext']['readOnlyRootFilesystem'] is True
    assert 'volumes' not in pod and 'volumeMounts' not in container
    assert all('POSTGRES' not in env['name'] for env in container['env'])
