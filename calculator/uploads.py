"""Private CSV storage on the API's data volume. Filenames are metadata only."""
import os
from uuid import UUID, uuid4

import policy
from errors import Rejected
from quotas import account, check_allowance, check_disk, transaction

PUBLIC = 'id,name,size_bytes,received_bytes,state,job_id,rows,columns,error,created_at'


def path(file_id):
    target = policy.DATA_ROOT / 'uploads' / str(UUID(str(file_id))) / 'data.csv'
    if any(p.is_symlink() for p in (target, target.parent, target.parent.parent, policy.DATA_ROOT)):
        raise Rejected('Invalid file path', 400)
    return target


def owned(conn, file_id, user):
    row = conn.execute('SELECT * FROM uploads WHERE id=%s AND user_id=%s', (file_id, user)).fetchone()
    if not row:
        raise Rejected('File not found', 404)
    return row


def public(conn, file_id, user):
    row = owned(conn, file_id, user)
    return {key: row[key] for key in PUBLIC.split(',')}


def create(user, name, size):
    if (not name.lower().endswith('.csv') or len(name) > 160 or '/' in name or '\\' in name
            or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in name)):
        raise Rejected('Choose a CSV filename without paths or control characters', 422)
    if not policy.COMPUTE_ENABLED:
        raise Rejected('CSV validation requires compute to be enabled', 503)
    file_id = uuid4()
    with transaction() as conn:
        account(conn, user)
        if conn.execute('SELECT 1 FROM uploads WHERE user_id=%s AND state=ANY(%s)', (user, list(policy.UPLOAD_ACTIVE))).fetchone():
            raise Rejected('Finish or remove your current upload first', 429)
        count = conn.execute('SELECT count(*) AS n FROM uploads WHERE user_id=%s', (user,)).fetchone()['n']
        if count >= policy.MAX_FILES:
            raise Rejected('Remove a CSV file before uploading another (128-file limit)', 413)
        active = conn.execute('SELECT count(*) AS n FROM uploads WHERE state=ANY(%s)', (list(policy.UPLOAD_ACTIVE),)).fetchone()['n']
        if active >= 2:
            raise Rejected('Two uploads are already in progress. Please try again later.', 429)
        check_allowance(conn, user, size)
        check_disk(conn, size)
        conn.execute("INSERT INTO uploads(id,user_id,name,size_bytes,state) VALUES (%s,%s,%s,%s,'receiving')",
                     (file_id, user, name, size))
    # Commit the reservation first. A crash at any later point is cleaned up by expiry.
    with transaction() as conn:
        row = owned(conn, file_id, user)
        writable(row)
        if row['state'] != 'receiving':
            raise Rejected('Upload is no longer accepting data')
        target = path(file_id)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=False)
        target.touch(mode=0o600, exist_ok=False)
        return public(conn, file_id, user)


def writable(row):
    from datetime import datetime, timezone
    if (datetime.now(timezone.utc) - row['created_at']).total_seconds() >= policy.UPLOAD_SECONDS:
        raise Rejected('Upload expired; remove it and start again', 410)


def write(conn, file_id, user, offset, data):
    row = owned(conn, file_id, user)
    writable(row)
    if row['state'] != 'receiving':
        raise Rejected('Upload is no longer accepting data')
    if not data or len(data) > policy.UPLOAD_CHUNK_BYTES or offset + len(data) > row['size_bytes']:
        raise Rejected('Upload exceeds its reserved size', 413)
    target = path(file_id)
    if offset != row['received_bytes'] or not target.is_file() or target.stat().st_size != offset:
        raise Rejected('Upload offset does not match; remove it and start again')
    check_disk(conn)
    with target.open('ab') as stream:
        stream.write(data)
    conn.execute('UPDATE uploads SET received_bytes=received_bytes+%s WHERE id=%s', (len(data), file_id))


def complete(conn, file_id, user):
    row = owned(conn, file_id, user)
    writable(row)
    if row['state'] != 'receiving':
        raise Rejected('Upload is already sealed')
    target = path(file_id)
    if row['received_bytes'] != row['size_bytes'] or not target.is_file() or target.stat().st_size != row['size_bytes']:
        raise Rejected('Upload is incomplete')
    with target.open('rb') as stream:
        os.fsync(stream.fileno())
    conn.execute("UPDATE uploads SET state='uploaded' WHERE id=%s", (file_id,))
    return public(conn, file_id, user)


def bind(conn, file_id, user, job_id):
    row = owned(conn, file_id, user)
    writable(row)
    if row['state'] != 'uploaded':
        raise Rejected('Upload is not ready for validation')
    conn.execute("UPDATE uploads SET state='validating',job_id=%s WHERE id=%s", (job_id, file_id))


def readable(conn, file_id, user, validation_job=None):
    row = owned(conn, file_id, user)
    if row['state'] != 'ready' and not (row['state'] == 'validating' and row['job_id'] == validation_job):
        raise Rejected('File not found', 404)
    target = path(file_id)
    if not target.is_file() or target.stat().st_size != row['size_bytes']:
        raise Rejected('File is unavailable', 404)
    return row, target


def erase(file_id):
    target = path(file_id)
    target.unlink(missing_ok=True)
    if target.parent.exists():
        target.parent.rmdir()


def check_result(conn, job):
    if job['tool_id'] != 'csv_validation':
        return
    row, _ = readable(conn, UUID(job['inputs']['upload_id']), job['user_id'], job['id'])
    result = job['result']
    if (not isinstance(result, dict) or type(result.get('rows')) is not int or result['rows'] < 1
            or type(result.get('columns')) is not int or not 1 <= result['columns'] <= 256):
        raise Rejected('CSV validation did not produce a valid result')


def finalize(conn, job, success, error):
    row = conn.execute("SELECT * FROM uploads WHERE job_id=%s AND state='validating'", (job['id'],)).fetchone()
    if not row:
        return
    if success:
        conn.execute('UPDATE accounts SET used_bytes=used_bytes+%s WHERE user_id=%s', (row['size_bytes'], row['user_id']))
        conn.execute("UPDATE uploads SET state='ready',rows=%s,columns=%s WHERE id=%s",
                     (job['result']['rows'], job['result']['columns'], row['id']))
    else:
        erase(row['id'])  # Never release a reservation before disk cleanup succeeds.
        conn.execute("UPDATE uploads SET state='rejected',error=%s WHERE id=%s",
                     (error or 'CSV validation cancelled', row['id']))


def remove(conn, file_id, user):
    row = owned(conn, file_id, user)
    if row['state'] == 'validating':
        raise Rejected('Cancel the validation job and wait for it to stop before removing this file')
    if row['state'] == 'ready' and conn.execute('SELECT 1 FROM jobs WHERE user_id=%s AND state=ANY(%s)',
                                              (user, list(policy.ACTIVE))).fetchone():
        raise Rejected('Wait for your active job to stop before removing an input file')
    erase(file_id)
    if row['state'] == 'ready':
        conn.execute('UPDATE accounts SET used_bytes=used_bytes-%s WHERE user_id=%s', (row['size_bytes'], user))
    conn.execute("DELETE FROM download_tickets WHERE kind='upload' AND file_id=%s", (file_id,))
    conn.execute('DELETE FROM uploads WHERE id=%s', (file_id,))


def cleanup():
    with transaction() as conn:
        rows = conn.execute("""SELECT id FROM uploads WHERE state IN ('receiving','uploaded')
            AND created_at < NOW() - %s * INTERVAL '1 second'""", (policy.UPLOAD_SECONDS,)).fetchall()
        for row in rows:
            erase(row['id'])
            conn.execute("UPDATE uploads SET state='rejected',error='Upload expired after one hour' WHERE id=%s", (row['id'],))
        conn.execute("DELETE FROM uploads WHERE state='rejected' AND created_at < NOW() - INTERVAL '1 day'")
        conn.execute('DELETE FROM download_tickets WHERE expires_at < NOW()')
