import importlib.util
from pathlib import Path

import pytest
import yaml
import policy

ROOT = Path(__file__).resolve().parents[1]


def test_deployment_limits_match_admission_policy():
    documents = list(yaml.safe_load_all((ROOT / 'k8s/compute.yaml').read_text()))
    capacity = next(item for item in documents if item['kind'] == 'ResourceQuota')['spec']['hard']
    assert int(capacity['pods']) == policy.MAX_JOBS
    assert capacity['limits.cpu'] == '2500m'
    assert capacity['limits.memory'] == '6Gi'
    permissions = next(item for item in documents if item['kind'] == 'Role')['rules']
    assert all('secrets' not in rule['resources'] and '*' not in rule['verbs'] for rule in permissions)
    volume = next(item for item in yaml.safe_load_all((ROOT / 'k8s/storage.yaml').read_text()) if item['kind'] == 'PersistentVolume')
    assert volume['spec']['local']['path'] == '/mnt/data/kubetoole'
    assert volume['spec']['persistentVolumeReclaimPolicy'] == 'Retain'


def renderer():
    spec = importlib.util.spec_from_file_location('render_patch', ROOT / 'k8s/render.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.render


def test_render_requires_owner_and_immutable_matching_images():
    render = renderer()
    image = 'ghcr.io/mayques/kubetoole-calculator@sha256:' + 'a' * 64
    patch = yaml.safe_load(render('test-owner', image))
    container = patch['spec']['template']['spec']['containers'][0]
    assert container['name'] == 'kubetoole-calculator'
    env = {item['name']: item['value'] for item in container['env']}
    assert container['image'] == env['WORKER_IMAGE'] == image
    assert env['OWNER_SUBJECT'] == 'test-owner'
    assert patch['spec']['strategy']['type'] == 'Recreate'
    for owner, reference in [('', image), ('REPLACE_OWNER_SUBJECT', image), ('owner', 'image:latest')]:
        with pytest.raises(ValueError):
            render(owner, reference)


def test_startup_rejects_unconfigured_owner(db, monkeypatch):
    from fastapi.testclient import TestClient
    from main import app
    monkeypatch.setattr(policy, 'OWNER_SUBJECT', '')
    with pytest.raises(RuntimeError, match='OWNER_SUBJECT'):
        with TestClient(app):
            pass
