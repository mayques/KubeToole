import io
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlsplit

import pytest

import database
import jobs
import policy
import quotas
import uploads
import worker
from csv_format import InvalidCSV, validate
from errors import Rejected
from test_platform import launch, claim
from test_auth_worker import sdk_context

CSV = b'name,value\r\n"a,b",42\r\nformula,=1+2\r\nhtml,<script>alert(1)</script>\r\n'


def start(api, data=CSV, name='data.csv'):
    response = api[0].post('/uploads', json={'name': name, 'size': len(data)})
    assert response.status_code == 201, response.text
    return response.json()['id']


def send(api, file_id, data=CSV, offset=0):
    return api[0].post(f'/uploads/{file_id}/write?offset={offset}', content=data,
                       headers={'Content-Type': 'application/octet-stream'})


def seal(api, file_id, data=CSV):
    assert send(api, file_id, data).status_code == 200
    assert api[0].post(f'/uploads/{file_id}/complete').status_code == 200


def validator(api, file_id, monkeypatch):
    response = api[0].post(f'/uploads/{file_id}/validate')
    assert response.status_code == 202, response.text
    job_id = response.json()['id']
    token = api[2].tokens[job_id]
    claim(api, job_id, {'Authorization': 'Bearer ' + token})
    monkeypatch.setenv('JOB_ID', job_id)
    monkeypatch.setenv('JOB_TOKEN', token)
    monkeypatch.setenv('TOOL_ID', 'csv_validation')
    monkeypatch.setenv('TOOL_INPUTS', json.dumps({'upload_id': file_id}))
    def urlopen(request, timeout):
        parsed = urlsplit(request.full_url)
        response = api[0].request(request.get_method(), parsed.path + ('?' + parsed.query if parsed.query else ''),
                                  content=request.data, headers=dict(request.header_items()))
        if response.status_code >= 400:
            raise HTTPError(request.full_url, response.status_code, response.text, {}, None)
        return io.BytesIO(response.content)
    monkeypatch.setattr(worker.urllib.request, 'urlopen', urlopen)
    return job_id, worker.Context()


@pytest.mark.parametrize('data,expected', [
    (CSV, {'rows': 3, 'columns': 2}),
    (b'\xef\xbb\xbfhead,other\n"two\nlines","a""b"\n', {'rows': 1, 'columns': 2}),
    ('label,val\rZürich,-42'.encode(), {'rows': 1, 'columns': 2}),
    (b'head\n=HYPERLINK("evil")', None),  # Bare quotes are not valid in an unquoted field.
])
def test_csv_format(data, expected):
    if expected is None:
        with pytest.raises(InvalidCSV):
            validate(io.BytesIO(data))
    else:
        assert validate(io.BytesIO(data)) == expected


@pytest.mark.parametrize('data', [
    b'', b'header\n', b'a,a\nx,y\n', b'a, \nx,y', b'a,b\n1\n', b'a\n1,2',
    b'a\n\x00', b'a\n\xff', b'a\n"unclosed', b'a\n"ok"junk', b'a\nno"quote',
    b'a\n\n', b'a\n' + b'x' * 65537, b'a' * 129 + b'\n1',
    (','.join(str(n) for n in range(257)) + '\n' + ',' * 256).encode(),
    b'a\n"' + b'\n' * 1_048_577 + b'"',
])
def test_rejects_malformed_or_unbounded_csv(data):
    with pytest.raises(InvalidCSV):
        validate(io.BytesIO(data))


def test_success_is_private_until_validation_stops_and_download_is_original(api, monkeypatch):
    client, identity, cluster = api
    file_id = start(api)
    assert client.get('/account/usage').json()['storage']['reservedBytes'] == len(CSV)
    assert client.get(f'/uploads/{file_id}/download').status_code == 404
    seal(api, file_id)
    job_id, _ = validator(api, file_id, monkeypatch)
    assert worker.execute_child() == 0
    jobs.reconcile()
    assert client.get('/uploads').json()['uploads'][0]['state'] == 'validating'
    assert client.get(f'/uploads/{file_id}/download').status_code == 404
    cluster.stop(job_id, True)
    jobs.reconcile()
    jobs.reconcile()  # Must never charge twice.
    row = client.get('/uploads').json()['uploads'][0]
    assert (row['state'], row['rows'], row['columns']) == ('ready', 3, 2)
    snapshot = client.get('/account/usage').json()['storage']
    assert snapshot['reservedBytes'] == 0
    assert snapshot['usedBytes'] == len(CSV) + len(quotas.encoded({'upload_id': file_id})) + len(quotas.encoded({'rows': 3, 'columns': 2}))
    download = client.get(f'/uploads/{file_id}/download')
    assert download.content == CSV  # Formulas and HTML remain bytes, not evaluated/rewritten.
    assert download.headers['content-disposition'].startswith('attachment;')
    assert download.headers['x-content-type-options'] == 'nosniff'
    identity.user = 'bob'
    assert client.get('/uploads').json()['uploads'] == []
    assert client.get(f'/uploads/{file_id}/download').status_code == 404
    assert client.post('/downloads', json={'kind': 'upload', 'id': file_id}).status_code == 404
    other_id, other_headers = launch(api)
    claim(api, other_id, other_headers)
    assert client.get(f'/internal/jobs/{other_id}/inputs/{file_id}', headers=other_headers).status_code == 404
    identity.user = 'alice'
    own_id, own_headers = launch(api)
    claim(api, own_id, own_headers)
    assert client.get(f'/internal/jobs/{own_id}/inputs/{file_id}', headers=own_headers).content == CSV
    assert client.post(f'/uploads/{file_id}/remove').status_code == 409  # A worker could be reading it.
    cluster.stop(own_id)
    jobs.reconcile()
    assert client.post(f'/uploads/{file_id}/remove').status_code == 200
    assert not uploads.path(file_id).exists()
    assert client.get('/account/usage').json()['storage']['usedBytes'] == snapshot['usedBytes'] - len(CSV) + 2


def test_all_upload_mutations_check_owner_and_bounds(api):
    client, identity, _ = api
    file_id = start(api)
    identity.user = 'bob'
    assert send(api, file_id).status_code == 404
    for action in ('complete', 'validate', 'remove'):
        assert client.post(f'/uploads/{file_id}/{action}').status_code == 404
    identity.user = 'alice'
    assert send(api, file_id, b'x', 1).status_code == 409
    assert send(api, file_id, CSV + b'x').status_code == 413
    assert send(api, file_id, b'x' * (policy.UPLOAD_CHUNK_BYTES + 1)).status_code == 413
    assert client.post(f'/uploads/{file_id}/complete').status_code == 409
    assert send(api, file_id, CSV[:5]).status_code == 200
    assert send(api, file_id, CSV[:5]).status_code == 409  # Never append a retry twice.
    assert send(api, file_id, CSV[5:], 5).status_code == 200
    assert client.post(f'/uploads/{file_id}/complete').status_code == 200
    assert send(api, file_id, b'x', len(CSV)).status_code == 409


def test_size_and_paths_are_not_client_controlled(api):
    client = api[0]
    for name in ('../file.csv', '/etc/passwd.csv', 'a\\b.csv', 'a\n.csv', 'data.exe'):
        assert client.post('/uploads', json={'name': name, 'size': 10}).status_code == 422
    for size in (0, -1, 500_000_001, True, '10'):
        assert client.post('/uploads', json={'name': 'data.csv', 'size': size}).status_code == 422
    assert client.post('/uploads', json={'name': 'data.csv', 'size': 4, 'user_id': 'bob'}).status_code == 422
    response = client.post('/uploads', json={'name': 'maximum.csv', 'size': 500_000_000})
    assert response.status_code == 201  # No 500 MB allocation: only a reservation and empty file.
    path = uploads.path(response.json()['id'])
    assert path.stat().st_size == 0
    assert path.relative_to(policy.DATA_ROOT).parts == ('uploads', response.json()['id'], 'data.csv')


def test_upload_admission_and_quota_are_atomic(api, monkeypatch):
    def reserve(_):
        try:
            return uploads.create('alice', 'data.csv', 80)
        except Rejected as error:
            return error.status
    monkeypatch.setattr(policy, 'USER_BYTES', 100)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, range(2)))
    assert sum(isinstance(row, dict) for row in results) == 1
    assert 429 in results
    assert api[0].get('/account/usage').json()['storage']['reservedBytes'] == 80
    assert api[0].post('/calculate', json={'expression': '1+' * 6 + '1'}).status_code == 413
    monkeypatch.setattr(quotas.shutil, 'disk_usage', lambda path: SimpleNamespace(free=policy.DATA_FREE_BYTES))
    assert api[0].post('/tools/probe/run', json={'inputs': {}}).status_code == 507


def test_invalid_csv_and_cancel_release_only_after_worker_stops(api, monkeypatch):
    client, _, cluster = api
    bad = b'a,b\n1\n'
    file_id = start(api, bad)
    seal(api, file_id, bad)
    job_id, _ = validator(api, file_id, monkeypatch)
    assert worker.execute_child() == 1
    jobs.reconcile()
    assert client.get('/account/usage').json()['storage']['reservedBytes'] == len(bad)
    cluster.stop(job_id)
    jobs.reconcile()
    row = client.get('/uploads').json()['uploads'][0]
    assert row['state'] == 'rejected' and 'Column count' in row['error']
    assert client.get('/account/usage').json()['storage']['reservedBytes'] == 0
    assert not uploads.path(file_id).exists()
    file_id = start(api)
    seal(api, file_id)
    job_id, _ = validator(api, file_id, monkeypatch)
    assert client.post(f'/jobs/{job_id}/cancel').status_code == 202
    assert client.post(f'/uploads/{file_id}/remove').status_code == 409
    jobs.reconcile()
    assert uploads.path(file_id).exists()
    cluster.stop(job_id)
    jobs.reconcile()
    assert not uploads.path(file_id).exists()


def test_expiry_and_failed_disk_cleanup_keep_accounting_safe(api, monkeypatch):
    file_id = start(api)
    send(api, file_id, b'name')
    with database.connection() as conn:
        conn.execute("UPDATE uploads SET created_at=NOW()-INTERVAL '61 minutes' WHERE id=%s", (file_id,))
    assert send(api, file_id, b'x', 4).status_code == 410
    original = uploads.erase
    def fail(file_id):
        original(file_id)
        raise OSError('Simulated crash after unlink')
    monkeypatch.setattr(uploads, 'erase', fail)
    with pytest.raises(OSError):
        uploads.cleanup()
    assert api[0].get('/account/usage').json()['storage']['reservedBytes'] == len(CSV)
    monkeypatch.setattr(uploads, 'erase', original)
    uploads.cleanup()
    uploads.cleanup()
    assert api[0].get('/account/usage').json()['storage']['reservedBytes'] == 0


def test_busy_validation_does_not_queue_or_duplicate(api, monkeypatch):
    file_id = start(api)
    seal(api, file_id)
    job_id, _ = launch(api)
    assert api[0].post(f'/uploads/{file_id}/validate').status_code == 429
    assert api[0].get('/uploads').json()['uploads'][0]['state'] == 'uploaded'
    api[2].stop(job_id)
    jobs.reconcile()
    validation_id, _ = validator(api, file_id, monkeypatch)
    assert api[0].post(f'/uploads/{file_id}/validate').status_code == 429
    assert api[2].creates == 2
    assert all(spec['id'] != 'csv_validation' for spec in api[0].get('/tools').json()['tools'])
    assert api[0].post('/tools/csv_validation/run', json={'inputs': {'upload_id': file_id}}).status_code == 404
    assert api[0].get(f'/jobs/{validation_id}').json()['state'] == 'running'


def test_single_use_download_tickets_are_expiring_and_owner_scoped(api, monkeypatch):
    file_id = start(api)
    seal(api, file_id)
    job_id, _ = validator(api, file_id, monkeypatch)
    assert worker.execute_child() == 0
    api[2].stop(job_id, True)
    jobs.reconcile()
    client = api[0]
    issue = lambda: client.post('/downloads', json={'kind': 'upload', 'id': file_id})
    token = issue().json()['token']
    assert client.post('/downloads/consume', data={'token': token}).content == CSV
    assert client.post('/downloads/consume', data={'token': token}).status_code == 404
    token = issue().json()['token']
    with database.connection() as conn:
        assert conn.execute('SELECT token_hash FROM download_tickets').fetchone()['token_hash'] != token
        conn.execute("UPDATE download_tickets SET expires_at=NOW()-INTERVAL '1 second'")
    assert client.post('/downloads/consume', data={'token': token}).status_code == 404
    token = issue().json()['token']
    assert client.post(f'/uploads/{file_id}/remove').status_code == 200
    assert client.post('/downloads/consume', data={'token': token}).status_code == 404


def test_worker_checks_csv_outputs_and_public_download_rejects_other_formats(api, monkeypatch):
    job_id, context = sdk_context(api, monkeypatch)
    with context.output('data.csv') as output:
        output.write(CSV)
    with context.output('data.bin') as binary:
        binary.write(b'arbitrary data')
    context.request('/finish', {'outcome': 'succeeded'})
    api[2].stop(job_id, True)
    jobs.reconcile()
    assert api[0].get(f'/artifacts/{output.id}').content == CSV
    assert api[0].get(f'/artifacts/{binary.id}').status_code == 404
    api[1].user = 'bob'
    assert api[0].get(f'/artifacts/{output.id}').status_code == 404
    assert api[0].post('/downloads', json={'kind': 'artifact', 'id': output.id}).status_code == 404


def test_csv_input_selector_checks_readiness_and_ownership_before_launch(api, monkeypatch):
    from tools import REGISTRY
    spec = {**REGISTRY['probe'], 'inputs': {'type': 'object', 'required': ['dataset'], 'additionalProperties': False,
                                          'properties': {'dataset': {'type': 'string', 'format': 'csv-file'}}}}
    monkeypatch.setitem(REGISTRY, 'probe', spec)
    file_id = start(api)
    submit = lambda value: api[0].post('/tools/probe/run', json={'inputs': {'dataset': value}})
    assert submit(file_id).status_code == 404
    assert submit('../other.csv').status_code == 422
    assert api[2].creates == 0
    seal(api, file_id)
    job_id, _ = validator(api, file_id, monkeypatch)
    assert worker.execute_child() == 0
    api[2].stop(job_id, True)
    jobs.reconcile()
    api[1].user = 'bob'
    assert submit(file_id).status_code == 404
    assert api[2].creates == 1
    api[1].user = 'alice'
    assert submit(file_id).status_code == 202


def test_global_upload_limit_and_invalid_output_validation(api, monkeypatch):
    start(api)
    api[1].user = 'bob'
    start(api)
    api[1].user = 'charlie'
    assert api[0].post('/uploads', json={'name': 'third.csv', 'size': 10}).status_code == 429
    job_id, context = sdk_context(api, monkeypatch)
    with pytest.raises(InvalidCSV):
        with context.output('malformed.csv') as output:
            output.write(b'a,b\n1\n')
    with database.connection() as conn:
        assert conn.execute('SELECT csv_checked FROM artifacts WHERE id=%s', (output.id,)).fetchone()['csv_checked'] is False
    context.request('/finish', {'outcome': 'failed'})
    api[2].stop(job_id)
    jobs.reconcile()
    assert api[0].get(f'/artifacts/{output.id}').status_code == 404


def test_upload_symlinks_and_unauthenticated_access_are_rejected(api, tmp_path):
    client = api[0]
    file_id = start(api)
    path = uploads.path(file_id)
    path.unlink()
    target = tmp_path / 'outside'
    target.write_bytes(b'private')
    path.symlink_to(target)
    assert send(api, file_id).status_code == 400
    assert target.read_bytes() == b'private'
    from main import app
    app.dependency_overrides.clear()
    assert client.get('/uploads').status_code == 401
    assert client.get(f'/uploads/{file_id}/download').status_code == 401
    assert client.post('/downloads', json={'kind': 'upload', 'id': file_id}).status_code == 401
