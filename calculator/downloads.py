"""Authenticated, owner-scoped CSV downloads; no public storage URLs."""
import hashlib
import secrets
from urllib.parse import parse_qs

from fastapi.responses import FileResponse

import storage
import uploads
from errors import Rejected


def resolve(conn, kind, file_id, user):
    if kind == 'upload':
        return uploads.readable(conn, file_id, user)
    row = conn.execute("""SELECT a.* FROM artifacts a JOIN jobs j ON j.id=a.job_id
        WHERE a.id=%s AND j.user_id=%s AND j.state='succeeded'
        AND a.kind='output' AND a.complete AND a.csv_checked""", (file_id, user)).fetchone()
    if not row:
        raise Rejected('File not found', 404)
    path = storage.artifact_path(row)
    if not path.is_file() or path.stat().st_size != row['size_bytes']:
        raise Rejected('File is unavailable', 404)
    return row, path


def response(row, path):
    return FileResponse(path, filename=row['name'], media_type='text/csv; charset=utf-8', headers={
        'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
        'Content-Security-Policy': "default-src 'none'; sandbox", 'Referrer-Policy': 'no-referrer',
    })


def issue(conn, kind, file_id, user):
    resolve(conn, kind, file_id, user)  # Never issue a ticket based on an ID alone.
    conn.execute('DELETE FROM download_tickets WHERE expires_at < NOW()')
    count = conn.execute('SELECT count(*) AS n FROM download_tickets WHERE user_id=%s', (user,)).fetchone()['n']
    if count >= 2:
        raise Rejected('Two downloads are pending; wait five minutes before requesting another', 429)
    token = secrets.token_urlsafe(32)
    conn.execute('INSERT INTO download_tickets(token_hash,user_id,kind,file_id) VALUES (%s,%s,%s,%s)',
                 (hashlib.sha256(token.encode()).hexdigest(), user, kind, file_id))
    return {'token': token}


def consume(conn, body):
    try:
        fields = parse_qs(body.decode('ascii'), strict_parsing=True, max_num_fields=1)
        token = fields['token'][0]
        if len(fields) != 1 or len(token) != 43:
            raise ValueError()
    except (UnicodeError, KeyError, ValueError, IndexError):
        raise Rejected('Invalid download request', 400) from None
    ticket = conn.execute('DELETE FROM download_tickets WHERE token_hash=%s AND expires_at > NOW() RETURNING *',
                          (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
    if not ticket:
        raise Rejected('Download expired or already used', 404)
    row, path = resolve(conn, ticket['kind'], ticket['file_id'], ticket['user_id'])
    return response(row, path)
