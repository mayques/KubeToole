import io
import json
import time
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

import auth
import jobs
import policy
import quotas
import worker
from test_platform import launch, claim


@pytest.fixture
def signing_key(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(auth.jwks, 'get_signing_key_from_jwt', lambda token: SimpleNamespace(key=key.public_key()))
    return key


def test_valid_identity_and_missing_auth(signing_key):
    payload = {'sub': 'alice', 'iss': auth.ISSUER, 'aud': auth.PROJECT_ID, 'exp': int(time.time()) + 60}
    token = jwt.encode(payload, signing_key, algorithm='RS256')
    assert auth.verify_user(HTTPAuthorizationCredentials(scheme='Bearer', credentials=token)) == 'alice'
    with pytest.raises(HTTPException) as failure:
        auth.verify_user(None)
    assert failure.value.status_code == 401


@pytest.mark.parametrize('changed', [
    {'exp': 1}, {'iss': 'https://wrong.example'}, {'aud': 'another-project'}, {'sub': ''}, {'sub': 42},
])
def test_invalid_identity_is_rejected(signing_key, changed):
    payload = {'sub': 'alice', 'iss': auth.ISSUER, 'aud': auth.PROJECT_ID, 'exp': int(time.time()) + 60, **changed}
    token = jwt.encode(payload, signing_key, algorithm='RS256')
    with pytest.raises(HTTPException) as failure:
        auth.verify_user(HTTPAuthorizationCredentials(scheme='Bearer', credentials=token))
    assert failure.value.status_code == 401


def test_invalid_signature_is_rejected(signing_key):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode({'sub': 'alice', 'iss': auth.ISSUER, 'aud': auth.PROJECT_ID, 'exp': int(time.time()) + 60}, other, algorithm='RS256')
    with pytest.raises(HTTPException) as failure:
        auth.verify_user(HTTPAuthorizationCredentials(scheme='Bearer', credentials=token))
    assert failure.value.status_code == 401


def sdk_context(api, monkeypatch):
    job_id, headers = launch(api)
    claim(api, job_id, headers)
    monkeypatch.setenv('JOB_ID', job_id)
    monkeypatch.setenv('JOB_TOKEN', headers['Authorization'][7:])
    def urlopen(request, timeout):
        parsed = urlsplit(request.full_url)
        path = parsed.path + ('?' + parsed.query if parsed.query else '')
        response = api[0].request(request.get_method(), path, content=request.data, headers=dict(request.header_items()))
        if response.status_code >= 400:
            raise HTTPError(request.full_url, response.status_code, response.text, {}, None)
        return io.BytesIO(response.content)
    monkeypatch.setattr(worker.urllib.request, 'urlopen', urlopen)
    return job_id, worker.Context()


def test_worker_streams_chunks_and_seals_files(api, monkeypatch):
    job_id, context = sdk_context(api, monkeypatch)
    data = b'x' * 1_048_577  # Spans two HTTP writes; each reservation must precede its write.
    with context.output('result.bin') as output:
        assert output.write(data) == len(data)
    assert output.tell() == len(data)
    with output.open_reader() as saved:
        assert saved.read() == data
    with context.temporary('scratch.bin') as scratch:
        scratch.write(b'working data')
    scratch.discard()
    context.request('/finish', {'outcome': 'succeeded', 'result': {'file': output.id}})
    api[2].stop(job_id, True)
    jobs.reconcile()
    assert api[0].get(f'/artifacts/{output.id}').content == data
    assert api[0].get('/account/usage').json()['storage']['reservedBytes'] == 0


def test_worker_stops_on_quota_rejection(api, monkeypatch):
    monkeypatch.setattr(policy, 'USER_BYTES', 10)
    job_id, context = sdk_context(api, monkeypatch)
    with pytest.raises(HTTPError) as failure:
        with context.output('too-big.bin') as output:
            output.write(b'x' * 9)  # Two bytes are already charged for the job's {} inputs.
    assert failure.value.code == 413
    context.request('/finish', {'outcome': 'failed'})
    api[2].stop(job_id)
    jobs.reconcile()
    assert api[0].get(f'/jobs/{job_id}').json()['state'] == 'failed'
    assert api[0].get('/account/usage').json()['storage']['usedBytes'] == 2


def test_system_disk_guard_and_nonfinite_inputs(api, monkeypatch):
    monkeypatch.setattr(quotas.shutil, 'disk_usage', lambda path: SimpleNamespace(free=policy.SYSTEM_FREE_BYTES - 1 if str(path) == '/' else 10**12))
    assert api[0].post('/calculate', json={'expression': '1+1'}).status_code == 507
    assert api[0].post('/tools/probe/run', content='{"inputs":{"n":NaN}}', headers={'Content-Type': 'application/json'}).status_code == 422


@pytest.mark.parametrize('stop_reason', ['cancel', 'deadline'])
def test_worker_supervisor_stops_child_processes(tmp_path, stop_reason):
    """Exercise real process-group termination, including a child ignoring SIGTERM."""
    import os
    from pathlib import Path
    import signal
    import subprocess
    import sys

    pid_path = tmp_path / 'child.pid'
    executable = tmp_path / 'controlled_tool.py'
    executable.write_text(
        'import os, signal, time\n'
        'from pathlib import Path\n'
        'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
        f'Path({str(pid_path)!r}).write_text(str(os.getpid()))\n'
        'time.sleep(60)\n'
    )
    seconds = 60 if stop_reason == 'cancel' else 1
    supervisor = (
        'import worker\n'
        f'worker.__file__ = {str(executable)!r}\n'
        'worker.Context.request = lambda *args, **kwargs: '
        f'{{"tool_id": "probe", "inputs": {{}}, "remaining_seconds": {seconds}}}\n'
        'raise SystemExit(worker.run())\n'
    )
    env = {**os.environ, 'JOB_ID': 'test-only', 'JOB_TOKEN': 'test-only',
           'PYTHONPATH': str(Path(worker.__file__).parent)}
    process = subprocess.Popen([sys.executable, '-c', supervisor], env=env)
    child_pid = None
    try:
        for _ in range(200):
            if pid_path.exists():
                child_pid = int(pid_path.read_text())
                break
            time.sleep(.01)
        assert child_pid is not None
        if stop_reason == 'cancel':
            process.terminate()
        assert process.wait(timeout=10) == 1
        # The direct child was reaped by the supervisor after TERM/KILL.
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if child_pid is not None:
            try:
                os.killpg(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
