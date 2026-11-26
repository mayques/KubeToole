import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo, conninfo_to_dict
import pytest

import database
import jobs
import policy
import quotas
from tools import REGISTRY


@pytest.fixture(scope="session")
def postgres():
    uri = os.getenv("TEST_DATABASE_URL")
    if uri:
        host = conninfo_to_dict(uri).get("host", "")
        if host not in ("localhost", "127.0.0.1", "::1") and not host.startswith(("/tmp", "/private/tmp")):
            pytest.fail("Tests require an explicitly local PostgreSQL server")
        yield uri
    else:
        pgserver = pytest.importorskip("pgserver", reason="Provide TEST_DATABASE_URL or install pgserver for local integration tests")
        with tempfile.TemporaryDirectory(prefix="kubetoole-pg-") as directory:
            server = pgserver.get_server(Path(directory) / "db", cleanup_mode="delete")
            yield server.get_uri()
            server.cleanup()


@pytest.fixture
def blank(postgres, monkeypatch, tmp_path):
    schema = "test_" + uuid4().hex
    with psycopg.connect(postgres) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    monkeypatch.setenv("DATABASE_URL", make_conninfo(postgres, options=f"-csearch_path={schema}"))
    monkeypatch.setattr(policy, "OWNER_SUBJECT", "owner")
    monkeypatch.setattr(policy, "DATA_ROOT", tmp_path / "data")
    policy.DATA_ROOT.mkdir()
    monkeypatch.setattr(quotas.shutil, "disk_usage", lambda path: SimpleNamespace(free=1_000_000_000_000))
    yield
    with psycopg.connect(postgres) as conn:
        conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def db(blank):
    database.migrate()


class FakeCluster:
    def __init__(self):
        self.items = {}
        self.tokens = {}
        self.creates = 0
        self.uncertain = False
        self.offline = False

    def create(self, job_id, token):
        self.creates += 1
        self.tokens[str(job_id)] = token
        self.items[str(job_id)] = {"exists": True, "active": True, "terminal": None}
        if self.uncertain:
            raise TimeoutError()

    def observe(self, job_id):
        if self.offline:
            raise TimeoutError()
        return self.items.get(str(job_id), {"exists": False, "active": False, "terminal": None})

    def delete(self, job_id):
        if self.offline:
            raise TimeoutError()
        item = self.items.get(str(job_id))
        if item and not item['active']:
            del self.items[str(job_id)]

    def job_ids(self):
        return list(self.items)

    def stop(self, job_id, success=False):
        self.items[str(job_id)] = {"exists": True, "active": False, "terminal": "Complete" if success else "Failed"}


@pytest.fixture
def compute(db, monkeypatch):
    cluster = FakeCluster()
    monkeypatch.setattr(policy, "COMPUTE_ENABLED", True)
    monkeypatch.setattr(policy, "WORKER_IMAGE", "example/image@sha256:" + "a" * 64)
    monkeypatch.setattr(jobs, "cluster", lambda: cluster)
    monkeypatch.setitem(REGISTRY, "probe", {"id": "probe", "name": "Probe", "description": "Test only", "execution": "job",
                                          "inputs": {"type": "object", "additionalProperties": False}})
    return cluster


@pytest.fixture
def api(compute):
    from fastapi.testclient import TestClient
    from auth import verify_user
    from main import app
    identity = SimpleNamespace(user="alice")
    app.dependency_overrides[verify_user] = lambda: identity.user
    client = TestClient(app)  # Explicit migrations above; no background reconciler in these tests.
    yield client, identity, compute
    client.close()
    app.dependency_overrides.clear()
