"""Only generated storage keys are paths; user filenames are display metadata."""
import os
from pathlib import Path
import shutil
from uuid import UUID, uuid4

import policy
from errors import Rejected
from quotas import check_allowance, check_disk


def job_dir(job_id):
    return policy.DATA_ROOT / "jobs" / str(UUID(str(job_id)))


def artifact_path(row):
    path = job_dir(row["job_id"]) / ("outputs" if row["kind"] == "output" else "scratch") / str(row["id"])
    # No user-supplied path components and no following symlinks on validation/download.
    if any(p.is_symlink() for p in (path, path.parent, path.parent.parent)):
        raise Rejected("Invalid artifact path", 400)
    return path


def prepare(job_id):
    root = job_dir(job_id)
    root.mkdir(parents=True, exist_ok=False)
    for name in ("outputs", "scratch"):
        (root / name).mkdir(mode=0o700)


def reserve_file(conn, job, name, kind):
    if kind not in ("output", "temp") or not name or len(name) > 160 or any(ord(c) < 32 for c in name):
        raise Rejected("Invalid artifact metadata", 422)
    count = conn.execute("SELECT count(*) AS n FROM artifacts WHERE job_id=%s", (job["id"],)).fetchone()["n"]
    if count >= policy.MAX_FILES:
        raise Rejected("Too many files in this job", 413)
    row = conn.execute("""INSERT INTO artifacts(id,job_id,name,kind) VALUES (%s,%s,%s,%s) RETURNING *""",
                       (uuid4(), job["id"], name, kind)).fetchone()
    artifact_path(row).touch(exist_ok=False)
    return row


def write_chunk(conn, job, artifact_id, offset, data):
    row = conn.execute("SELECT * FROM artifacts WHERE id=%s AND job_id=%s", (artifact_id, job["id"])).fetchone()
    if not row:
        raise Rejected("Artifact not found", 404)
    if row["complete"] or offset < 0 or offset + len(data) > row["reserved_bytes"]:
        raise Rejected("Write exceeds its reservation", 413)
    path = artifact_path(row)
    if path.stat().st_size != offset:
        raise Rejected("Output offset does not match")
    # Reservation was committed by the separate reserve request BEFORE any bytes are written.
    with path.open('ab') as stream:
        stream.write(data)


def extend(conn, job, artifact_id, size):
    row = conn.execute("SELECT * FROM artifacts WHERE id=%s AND job_id=%s", (artifact_id, job["id"])).fetchone()
    if not row:
        raise Rejected("Artifact not found", 404)
    if row["complete"] or size < row["reserved_bytes"]:
        raise Rejected("Invalid reservation")
    additional = size - row["reserved_bytes"]
    if row["kind"] == "output":
        check_allowance(conn, job["user_id"], additional)
        check_disk(conn, additional)
    else:
        total = conn.execute("SELECT COALESCE(SUM(reserved_bytes),0) AS n FROM artifacts WHERE job_id=%s AND kind='temp'",
                             (job["id"],)).fetchone()["n"]
        if total + additional > policy.SCRATCH_BYTES:
            raise Rejected("Temporary-file allowance exceeded", 413)
        check_disk(conn)
    conn.execute("UPDATE artifacts SET reserved_bytes=%s WHERE id=%s", (size, row["id"]))
    return row


def complete(conn, job, artifact_id):
    row = conn.execute("SELECT * FROM artifacts WHERE id=%s AND job_id=%s", (artifact_id, job["id"])).fetchone()
    if not row:
        raise Rejected("Artifact not found", 404)
    path = artifact_path(row)
    if not path.is_file() or path.stat().st_size > row["reserved_bytes"]:
        raise Rejected("Output exceeded its reservation", 413)
    with path.open('rb') as stream:
        os.fsync(stream.fileno())
    conn.execute("UPDATE artifacts SET complete=TRUE,size_bytes=%s WHERE id=%s", (path.stat().st_size, row["id"]))


def finalize(conn, job, success):
    """Called only after Kubernetes confirms every worker has stopped."""
    rows = conn.execute("SELECT * FROM artifacts WHERE job_id=%s", (job["id"],)).fetchall()
    if success:
        expected = {artifact_path(row) for row in rows if row["kind"] == "output"}
        for row in rows:
            if row["kind"] == "output":
                path = artifact_path(row)
                if not row["complete"] or not path.is_file() or path.stat().st_size != row["size_bytes"]:
                    raise Rejected("Incomplete output")
        actual = set((job_dir(job["id"]) / "outputs").iterdir())
        if actual != expected:
            raise Rejected("Tool wrote outside the storage interface")
        size = sum(row["size_bytes"] for row in rows if row["kind"] == "output")
        conn.execute("UPDATE accounts SET used_bytes=used_bytes+%s WHERE user_id=%s", (size, job["user_id"]))
        scratch = job_dir(job["id"]) / "scratch"
        if scratch.exists():
            shutil.rmtree(scratch)
        conn.execute("DELETE FROM artifacts WHERE job_id=%s AND kind='temp'", (job["id"],))
    else:
        root = job_dir(job["id"])
        if root.exists():
            shutil.rmtree(root)
        conn.execute("DELETE FROM artifacts WHERE job_id=%s", (job["id"],))
        conn.execute("UPDATE accounts SET used_bytes=used_bytes-%s WHERE user_id=%s", (job["result_bytes"], job["user_id"]))
        conn.execute("UPDATE jobs SET result=NULL,result_bytes=0 WHERE id=%s", (job["id"],))
