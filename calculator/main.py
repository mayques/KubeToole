import logging
import re
import threading
from contextlib import asynccontextmanager
from typing import Any, Literal
from uuid import UUID

from fastapi import Body, Depends, FastAPI, Header, Query
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

import jobs
import downloads
import policy
import storage
import uploads
from auth import verify_user
from database import connection, migrate
from errors import Rejected
from quotas import account, encoded, save_inline, transaction, usage
from tools import REGISTRY, execute, get


@asynccontextmanager
async def lifespan(app):
    if not policy.OWNER_SUBJECT or policy.OWNER_SUBJECT.startswith("REPLACE_"):
        raise RuntimeError("Configure OWNER_SUBJECT with the owner's immutable ZITADEL user ID")
    if not policy.DATA_ROOT.is_dir():
        raise RuntimeError("The persistent DATA_ROOT must be mounted before startup")
    if policy.COMPUTE_ENABLED and not re.search(r"@sha256:[a-f0-9]{64}$", policy.WORKER_IMAGE):
        raise RuntimeError("WORKER_IMAGE must be an immutable image digest matching the API release")
    migrate()
    with transaction() as conn:
        account(conn, policy.OWNER_SUBJECT)
        existing = conn.execute("SELECT count(*) AS n FROM accounts WHERE user_id<>%s", (policy.OWNER_SUBJECT,)).fetchone()["n"]
        if existing > policy.REGULAR_ACCOUNTS:
            raise RuntimeError("Existing history exceeds the account cap; retain the data and review admission before deployment")
    stop = threading.Event()
    def monitor():
        while not stop.is_set():
            try:
                jobs.reconcile()
                uploads.cleanup()
            except Exception:
                logging.exception("Job monitor unavailable; reservations remain held")
            stop.wait(3)
    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    yield
    stop.set()
    thread.join(timeout=15)


app = FastAPI(lifespan=lifespan)


@app.exception_handler(Rejected)
async def rejected(request, error):
    return JSONResponse({"detail": str(error)}, status_code=error.status)


class BodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in ("POST", "PUT", "PATCH"):
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk_write = re.fullmatch(r"/(?:internal/jobs/[a-fA-F0-9-]+/files|uploads)/[a-fA-F0-9-]+/write", scope["path"])
            chunk = message.get('body', b'')
            if len(body) + len(chunk) > (policy.UPLOAD_CHUNK_BYTES if chunk_write else policy.MAX_INPUT_BYTES):
                return await JSONResponse({"detail": "Request too large"}, status_code=413)(scope, receive, send)
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        delivered = False
        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()
        await self.app(scope, replay, send)


app.add_middleware(BodyLimit)
app.add_middleware(CORSMiddleware, allow_origins=["https://mayques.com", "https://www.mayques.com"],
                   allow_methods=["GET", "POST"], allow_headers=["Content-Type", "Authorization"])


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inputs: dict = Field(default_factory=dict)


class Calculation(BaseModel):
    expression: str = Field(min_length=1, max_length=1024)


def inline(user, tool_id, inputs):
    get(tool_id, inputs)
    with transaction() as conn:
        account(conn, user)
    try:
        result = execute(tool_id, inputs, None)
        if len(encoded(result)) > policy.MAX_RESULT_BYTES:
            raise Rejected("Result too large", 413)
    except (SyntaxError, ValueError, ZeroDivisionError, OverflowError, RecursionError):
        raise Rejected("Invalid calculation" if tool_id == "calculator" else "Invalid tool operation", 400) from None
    with transaction() as conn:
        save_inline(conn, user, tool_id, inputs, result)
    return result


@app.get("/health")
def health():
    with connection() as conn:
        conn.execute("SELECT 1")
    if not policy.DATA_ROOT.is_dir():
        raise Rejected("Persistent storage is unavailable", 503)
    return {"ready": True}


@app.post("/calculate")
def evaluate(data: Calculation, user: str = Depends(verify_user)):
    return inline(user, "calculator", {"expression": data.expression})


@app.get("/history")
def history(user: str = Depends(verify_user)):
    with transaction() as conn:
        account(conn, user)
        rows = conn.execute("""SELECT id,expression,result,created_at FROM calculations
            WHERE user_id=%s ORDER BY created_at DESC,id DESC LIMIT 100""", (user,)).fetchall()
    return {"calculations": rows}


@app.get("/account/usage")
def account_usage(user: str = Depends(verify_user)):
    with transaction() as conn:
        return usage(conn, user)


@app.get("/tools")
def tools(user: str = Depends(verify_user)):
    with transaction() as conn:
        account(conn, user)
    return {"tools": [spec for spec in REGISTRY.values() if not spec.get('internal')]}


@app.post("/tools/{tool_id}/run")
def run_tool(tool_id: str, data: Input, user: str = Depends(verify_user)):
    spec = get(tool_id, data.inputs)
    if spec.get('internal'):
        raise Rejected('Tool not found', 404)
    if spec["execution"] == "inline":
        return {"kind": "inline", "result": inline(user, tool_id, data.inputs)}
    return JSONResponse(content=jsonable_encoder(
        {"kind": "job", "job": jobs.launch(user, tool_id, data.inputs)}), status_code=202)


@app.get("/jobs")
def list_jobs(user: str = Depends(verify_user)):
    with transaction() as conn:
        account(conn, user)
        ids = conn.execute("SELECT id FROM jobs WHERE user_id=%s ORDER BY created_at DESC LIMIT 20", (user,)).fetchall()
        return {"jobs": [jobs.public(conn, row["id"], user) for row in ids]}


@app.get("/jobs/{job_id}")
def job_status(job_id: UUID, user: str = Depends(verify_user)):
    with transaction() as conn:
        return jobs.public(conn, job_id, user)


@app.post("/jobs/{job_id}/cancel", status_code=202)
def cancel_job(job_id: UUID, user: str = Depends(verify_user)):
    return jobs.cancel(user, job_id)


def download(conn, artifact_id, user):
    row = conn.execute("""SELECT a.* FROM artifacts a JOIN jobs j ON j.id=a.job_id
        WHERE a.id=%s AND j.user_id=%s AND j.state='succeeded' AND a.kind='output' AND a.complete""",
                       (artifact_id, user)).fetchone()
    if not row:
        raise Rejected("File not found", 404)
    path = storage.artifact_path(row)
    if not path.is_file():
        raise Rejected("File is unavailable", 404)
    return FileResponse(path, filename=row["name"], media_type="application/octet-stream")


@app.get("/artifacts/{artifact_id}")
def artifact_download(artifact_id: UUID, user: str = Depends(verify_user)):
    with transaction() as conn:
        return downloads.response(*downloads.resolve(conn, 'artifact', artifact_id, user))


class Upload(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=5, max_length=160)
    size: int = Field(ge=1, le=policy.MAX_UPLOAD_BYTES, strict=True)


@app.post('/uploads', status_code=201)
def create_upload(data: Upload, user: str = Depends(verify_user)):
    return uploads.create(user, data.name, data.size)


@app.get('/uploads')
def list_uploads(user: str = Depends(verify_user)):
    with transaction() as conn:
        account(conn, user)
        rows = conn.execute(f'SELECT {uploads.PUBLIC} FROM uploads WHERE user_id=%s ORDER BY created_at DESC', (user,)).fetchall()
        return {'uploads': rows, 'maxBytes': policy.MAX_UPLOAD_BYTES, 'chunkBytes': policy.UPLOAD_CHUNK_BYTES}


@app.get('/uploads/{file_id}/download')
def download_upload(file_id: UUID, user: str = Depends(verify_user)):
    with transaction() as conn:
        return downloads.response(*downloads.resolve(conn, 'upload', file_id, user))


@app.post('/uploads/{file_id}/write')
def upload_chunk(file_id: UUID, offset: int = Query(ge=0), data: bytes = Body(media_type='application/octet-stream'),
                 user: str = Depends(verify_user)):
    with transaction() as conn:
        uploads.write(conn, file_id, user, offset, data)
    return {'written': len(data)}


@app.post('/uploads/{file_id}/complete')
def seal_upload(file_id: UUID, user: str = Depends(verify_user)):
    with transaction() as conn:
        return uploads.complete(conn, file_id, user)


@app.post('/uploads/{file_id}/validate', status_code=202)
def validate_upload(file_id: UUID, user: str = Depends(verify_user)):
    # Check ownership before admission, and bind again atomically with job creation.
    with transaction() as conn:
        uploads.owned(conn, file_id, user)
    return jobs.launch(user, 'csv_validation', {'upload_id': str(file_id)}, internal=True)


@app.post('/uploads/{file_id}/remove')
def remove_upload(file_id: UUID, user: str = Depends(verify_user)):
    with transaction() as conn:
        uploads.remove(conn, file_id, user)
    return {'removed': True}


class Download(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['upload', 'artifact']
    id: UUID


@app.post('/downloads')
def request_download(data: Download, user: str = Depends(verify_user)):
    with transaction() as conn:
        return downloads.issue(conn, data.kind, data.id, user)


@app.post('/downloads/consume')
def consume_download(data: bytes = Body(media_type='application/x-www-form-urlencoded')):
    # A single-use capability issued only after JWT/ownership checks; sent in a POST
    # body, never a query string, so the browser can stream to its download manager.
    with transaction() as conn:
        return downloads.consume(conn, data)


def worker(conn, job_id, credential, claimed=True):
    if not credential.startswith("Bearer "):
        raise Rejected("Job credential required", 401)
    row = jobs.authorize_worker(conn, job_id, credential[7:])
    if claimed and not row["claimed"]:
        raise Rejected("Job has not started")
    return row


@app.post("/internal/jobs/{job_id}/claim")
def claim_job(job_id: UUID, authorization: str = Header(default="")):
    with transaction() as conn:
        return jobs.claim(conn, worker(conn, job_id, authorization, False))


class Finished(BaseModel):
    outcome: Literal["succeeded", "failed"]
    result: Any = None
    error: str | None = Field(default=None, max_length=300)


@app.post("/internal/jobs/{job_id}/finish")
def finish_job(job_id: UUID, data: Finished, authorization: str = Header(default="")):
    with transaction() as conn:
        jobs.finish(conn, worker(conn, job_id, authorization), data.outcome, data.result, data.error)
    return {"recorded": True}


class NewFile(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    kind: Literal["output", "temp"]


class Reservation(BaseModel):
    size: int = Field(ge=0, le=policy.OWNER_BYTES, strict=True)


@app.post("/internal/jobs/{job_id}/files")
def new_file(job_id: UUID, data: NewFile, authorization: str = Header(default="")):
    with transaction() as conn:
        row = storage.reserve_file(conn, worker(conn, job_id, authorization), data.name, data.kind)
        return {"id": row["id"]}


@app.post("/internal/jobs/{job_id}/files/{file_id}/reserve")
def reserve(job_id: UUID, file_id: UUID, data: Reservation, authorization: str = Header(default="")):
    with transaction() as conn:
        storage.extend(conn, worker(conn, job_id, authorization), file_id, data.size)
    return {"reserved": data.size}


@app.post("/internal/jobs/{job_id}/files/{file_id}/complete")
def complete(job_id: UUID, file_id: UUID, authorization: str = Header(default="")):
    with transaction() as conn:
        storage.complete(conn, worker(conn, job_id, authorization), file_id)
    return {"complete": True}


@app.post('/internal/jobs/{job_id}/files/{file_id}/csv-checked')
def csv_checked(job_id: UUID, file_id: UUID, authorization: str = Header(default='')):
    with transaction() as conn:
        worker(conn, job_id, authorization)
        row = conn.execute("SELECT * FROM artifacts WHERE id=%s AND job_id=%s AND complete AND kind='output'",
                           (file_id, job_id)).fetchone()
        if not row or not row['name'].lower().endswith('.csv'):
            raise Rejected('CSV output not found', 404)
        conn.execute('UPDATE artifacts SET csv_checked=TRUE WHERE id=%s', (file_id,))
    return {'checked': True}


@app.post("/internal/jobs/{job_id}/files/{file_id}/write")
def write_file(job_id: UUID, file_id: UUID, offset: int = Query(ge=0),
               data: bytes = Body(media_type="application/octet-stream"), authorization: str = Header(default="")):
    with transaction() as conn:
        storage.write_chunk(conn, worker(conn, job_id, authorization), file_id, offset, data)
    return {"written": len(data)}


@app.get("/internal/jobs/{job_id}/files/{file_id}")
def read_own_file(job_id: UUID, file_id: UUID, authorization: str = Header(default="")):
    with transaction() as conn:
        worker(conn, job_id, authorization)
        row = conn.execute("SELECT * FROM artifacts WHERE id=%s AND job_id=%s AND complete", (file_id, job_id)).fetchone()
        if not row:
            raise Rejected("File not found", 404)
        return FileResponse(storage.artifact_path(row), media_type="application/octet-stream")


@app.post("/internal/jobs/{job_id}/files/{file_id}/discard")
def discard_temporary(job_id: UUID, file_id: UUID, authorization: str = Header(default="")):
    with transaction() as conn:
        worker(conn, job_id, authorization)
        row = conn.execute("SELECT * FROM artifacts WHERE id=%s AND job_id=%s AND kind='temp' AND complete", (file_id, job_id)).fetchone()
        if not row:
            raise Rejected("Temporary file not found", 404)
        storage.artifact_path(row).unlink(missing_ok=True)
        conn.execute("DELETE FROM artifacts WHERE id=%s", (file_id,))
    return {"discarded": True}


@app.get("/internal/jobs/{job_id}/inputs/{file_id}")
def read_input(job_id: UUID, file_id: UUID, authorization: str = Header(default="")):
    with transaction() as conn:
        row = worker(conn, job_id, authorization)
        if conn.execute('SELECT 1 FROM uploads WHERE id=%s', (file_id,)).fetchone():
            validation_job = row['id'] if row['tool_id'] == 'csv_validation' else None
            upload, path = uploads.readable(conn, file_id, row['user_id'], validation_job)
            return FileResponse(path, media_type='application/octet-stream')
        return download(conn, file_id, row["user_id"])
