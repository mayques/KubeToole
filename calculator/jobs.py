import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timezone
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
import policy
import storage
import uploads
from cluster import cluster
from errors import Rejected
from quotas import account, check_allowance, check_disk, encoded, transaction
from tools import get

log = logging.getLogger(__name__)
PUBLIC = "id,tool_id,state,result,error,created_at,finished_at,cancel_requested"


def public(conn, job_id, user):
    row = conn.execute(f"SELECT {PUBLIC} FROM jobs WHERE id=%s AND user_id=%s", (job_id, user)).fetchone()
    if not row:
        raise Rejected("Job not found", 404)
    if row["state"] != "succeeded":
        row["result"] = None
    row["artifacts"] = conn.execute("""SELECT id,name,size_bytes,csv_checked FROM artifacts
        WHERE job_id=%s AND kind='output' AND complete AND EXISTS
        (SELECT 1 FROM jobs WHERE id=%s AND state='succeeded')""", (job_id, job_id)).fetchall()
    return row


def authorize_worker(conn, job_id, token):
    row = conn.execute("SELECT * FROM jobs WHERE id=%s", (job_id,)).fetchone()
    digest = hashlib.sha256(token.encode()).hexdigest()
    if not row or not hmac.compare_digest(row["token_hash"], digest):
        raise Rejected("Invalid job credential", 401)
    if row["state"] not in ("pending", "running") or row["cancel_requested"]:
        raise Rejected("Job is no longer accepting work")
    if (datetime.now(timezone.utc) - row["created_at"]).total_seconds() >= policy.DEADLINE_SECONDS:
        raise Rejected("Job deadline exceeded")
    return row


def launch(user, tool_id, inputs, *, internal=False):
    if not policy.COMPUTE_ENABLED:
        raise Rejected("Heavy computation is not enabled", 503)
    spec = get(tool_id, inputs)
    if spec.get('internal') and not internal:
        raise Rejected('Tool not found', 404)
    if spec["execution"] != "job":
        raise Rejected("This tool does not require a heavy job", 422)
    token, job_id = secrets.token_urlsafe(32), uuid4()
    size = len(encoded(inputs))
    with transaction() as conn:
        account(conn, user)
        for name, schema in spec['inputs'].get('properties', {}).items():
            if schema.get('format') == 'csv-file' and name in inputs:
                try:
                    file_id = UUID(inputs[name])
                except (ValueError, TypeError, AttributeError):
                    raise Rejected('Invalid CSV file ID', 422) from None
                uploads.readable(conn, file_id, user)
        count = conn.execute("SELECT count(*) AS n FROM jobs WHERE state=ANY(%s)", (list(policy.ACTIVE),)).fetchone()["n"]
        if count >= policy.MAX_JOBS:
            raise Rejected("All compute resources are currently in use. Please try again later.", 429)
        if conn.execute("SELECT 1 FROM jobs WHERE user_id=%s AND state=ANY(%s)", (user, list(policy.ACTIVE))).fetchone():
            raise Rejected("You already have an active job", 429)
        check_allowance(conn, user, size)
        check_disk(conn, policy.SCRATCH_BYTES + size)
        conn.execute("""INSERT INTO jobs(id,user_id,tool_id,inputs,input_bytes,token_hash,state)
            VALUES (%s,%s,%s,%s,%s,%s,'launching')""",
                     (job_id, user, tool_id, Jsonb(inputs), size, hashlib.sha256(token.encode()).hexdigest()))
        conn.execute("UPDATE accounts SET used_bytes=used_bytes+%s WHERE user_id=%s", (size, user))
        if tool_id == 'csv_validation':
            uploads.bind(conn, UUID(inputs['upload_id']), user, job_id)
    # Commit the durable reservation BEFORE creating anything in Kubernetes.
    with transaction() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id=%s", (job_id,)).fetchone()
        if job["state"] == "launching" and not job["cancel_requested"]:
            try:
                storage.prepare(job_id)
                cluster().create(job_id, token)
                conn.execute("UPDATE jobs SET state='pending' WHERE id=%s", (job_id,))
            except Exception:
                # A timed-out create may have succeeded. Never retry creation or free its slot blindly.
                conn.execute("UPDATE jobs SET state='cancelling',error='Job could not be started' WHERE id=%s", (job_id,))
                log.warning("Worker launch failed for job %s; reconciliation will confirm termination", job_id)
        return public(conn, job_id, user)


def cancel(user, job_id):
    with transaction() as conn:
        public(conn, job_id, user)  # Ownership check before any cluster action.
        conn.execute("""UPDATE jobs SET cancel_requested=TRUE,state='cancelling'
            WHERE id=%s AND state=ANY(%s)""", (job_id, list(policy.ACTIVE)))
        result = public(conn, job_id, user)
    # The reconciler performs deletion; the endpoint remains idempotent during cluster outages.
    return result


def claim(conn, job):
    if job["claimed"]:
        raise Rejected("Job was already started")
    conn.execute("UPDATE jobs SET claimed=TRUE,state='running' WHERE id=%s", (job["id"],))
    remaining = policy.DEADLINE_SECONDS - (datetime.now(timezone.utc) - job["created_at"]).total_seconds()
    return {"tool_id": job["tool_id"], "inputs": job["inputs"], "remaining_seconds": max(0, int(remaining))}


def finish(conn, job, outcome, result, error=None):
    if not job["claimed"] or job["outcome"]:
        raise Rejected("Job is not accepting a result")
    try:
        size = len(encoded(result)) if outcome == "succeeded" else 0
    except (ValueError, TypeError, RecursionError):
        raise Rejected("Result must contain valid JSON values", 422) from None
    if size > policy.MAX_RESULT_BYTES:
        raise Rejected("Result is too large; save it as an output file", 413)
    check_allowance(conn, job["user_id"], size)
    check_disk(conn, size)
    conn.execute("UPDATE jobs SET outcome=%s,result=%s,result_bytes=%s,error=%s WHERE id=%s",
                 (outcome, Jsonb(result) if outcome == "succeeded" else None, size,
                  error if outcome == 'failed' else None, job["id"]))
    conn.execute("UPDATE accounts SET used_bytes=used_bytes+%s WHERE user_id=%s", (size, job["user_id"]))


def reconcile():
    """Safe to run in more than one API process. Fail closed if the cluster cannot be observed."""
    if not policy.COMPUTE_ENABLED:
        return
    from database import connection
    with connection() as conn:
        rows = conn.execute("SELECT id FROM jobs WHERE state=ANY(%s)", (list(policy.ACTIVE),)).fetchall()
    for item in rows:
        try:
            with transaction() as conn:
                job = conn.execute("SELECT * FROM jobs WHERE id=%s", (item["id"],)).fetchone()
                if job["state"] not in policy.ACTIVE:
                    continue
                age = (datetime.now(timezone.utc) - job["created_at"]).total_seconds()
                if age >= policy.DEADLINE_SECONDS:
                    conn.execute("UPDATE jobs SET state='cancelling',error='Job deadline exceeded' WHERE id=%s", (job["id"],))
                    job["state"], job["error"] = "cancelling", "Job deadline exceeded"
                if job["state"] == "cancelling":
                    cluster().delete(job["id"])
                observed = cluster().observe(job["id"])
                if observed["active"]:
                    continue
                if observed["exists"] and not observed["terminal"]:
                    continue
                if not observed["exists"] and age < 60 and (job["state"] == "launching" or job["error"] == "Job could not be started"):
                    continue  # Permit in-flight creation to settle; never launch it again.
                success = observed["terminal"] == "Complete" and job["outcome"] == "succeeded" and job["state"] != "cancelling"
                try:
                    if success:
                        uploads.check_result(conn, job)
                    storage.finalize(conn, job, success)
                except Rejected:
                    success = False
                    storage.finalize(conn, job, False)
                state = "cancelled" if job["cancel_requested"] else ("succeeded" if success else "failed")
                error = None if state in ("succeeded", "cancelled") else (job["error"] or "Job failed or exceeded its resource limits")
                uploads.finalize(conn, job, success, error)
                conn.execute("UPDATE jobs SET state=%s,error=%s,finished_at=NOW() WHERE id=%s", (state, error, job["id"]))
        except Exception:
            log.exception("Could not reconcile job %s; its reservation remains held", item["id"])
    # Also remove late-created/orphaned Jobs. No auto-retries or reruns of tools.
    try:
        for job_id in cluster().job_ids():
            with transaction() as conn:
                row = conn.execute("SELECT state FROM jobs WHERE id=%s", (job_id,)).fetchone()
                if not row or row["state"] not in policy.ACTIVE:
                    cluster().delete(job_id)
    except Exception:
        log.exception("Worker cleanup postponed; Kubernetes is unavailable")
