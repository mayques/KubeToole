"""All admission and accounting mutations run inside transaction()."""
from contextlib import contextmanager
import json
import shutil

from psycopg.types.json import Jsonb
import policy
from database import connection, lock
from errors import Rejected


@contextmanager
def transaction():
    with connection() as conn:
        lock(conn)
        yield conn


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


def limit(user):
    return policy.OWNER_BYTES if user == policy.OWNER_SUBJECT else policy.USER_BYTES


def account(conn, user):
    row = conn.execute("SELECT * FROM accounts WHERE user_id=%s", (user,)).fetchone()
    if row:
        return row
    if not policy.OWNER_SUBJECT:
        raise Rejected("Account admission is not configured", 503)
    count = conn.execute("SELECT count(*) AS n FROM accounts WHERE user_id<>%s", (policy.OWNER_SUBJECT,)).fetchone()["n"]
    if user != policy.OWNER_SUBJECT and count >= policy.REGULAR_ACCOUNTS:
        raise Rejected("All account places are currently occupied", 403)
    return conn.execute("INSERT INTO accounts(user_id) VALUES (%s) RETURNING *", (user,)).fetchone()


def reserved(conn, user):
    return conn.execute("""SELECT COALESCE(SUM(a.reserved_bytes),0) AS n FROM artifacts a
        JOIN jobs j ON a.job_id=j.id WHERE j.user_id=%s AND j.state=ANY(%s) AND a.kind='output'""",
                        (user, list(policy.ACTIVE))).fetchone()["n"]


def check_allowance(conn, user, additional):
    row = account(conn, user)
    if additional < 0 or row["used_bytes"] + reserved(conn, user) + additional > limit(user):
        raise Rejected("Storage allowance exceeded", 413)


def check_disk(conn, additional=0):
    # Conservative: count the whole outstanding reservation even if partly written.
    # This intentionally avoids over-admission when two writers are progressing together.
    active = conn.execute("SELECT count(*) AS n FROM jobs WHERE state=ANY(%s)", (list(policy.ACTIVE),)).fetchone()["n"]
    output = conn.execute("""SELECT COALESCE(SUM(a.reserved_bytes),0) AS n FROM artifacts a
        JOIN jobs j ON j.id=a.job_id WHERE j.state=ANY(%s) AND a.kind='output'""",
                          (list(policy.ACTIVE),)).fetchone()["n"]
    if shutil.disk_usage(policy.DATA_ROOT).free - active * policy.SCRATCH_BYTES - output - additional < policy.DATA_FREE_BYTES:
        raise Rejected("Storage is temporarily at capacity", 507)
    if shutil.disk_usage("/").free < policy.SYSTEM_FREE_BYTES:
        raise Rejected("Server storage is temporarily at capacity", 507)


def save_inline(conn, user, tool_id, inputs, result):
    if tool_id == "calculator":
        size = len(inputs["expression"].encode()) + 8
    else:
        size = len(encoded(result))
    if size > policy.MAX_RESULT_BYTES:
        raise Rejected("Result is too large for an inline tool", 413)
    check_allowance(conn, user, size)
    check_disk(conn, size)
    if tool_id == "calculator":
        conn.execute("INSERT INTO calculations(user_id,expression,result) VALUES (%s,%s,%s)",
                     (user, inputs["expression"], result["result"]))
    else:
        conn.execute("INSERT INTO inline_results(user_id,tool_id,result,size_bytes) VALUES (%s,%s,%s,%s)",
                     (user, tool_id, Jsonb(result), size))
    conn.execute("UPDATE accounts SET used_bytes=used_bytes+%s WHERE user_id=%s", (size, user))


def usage(conn, user):
    row = account(conn, user)
    per_tool = {}
    queries = [
        ("SELECT 'calculator' AS tool_id, COALESCE(SUM(octet_length(expression)+8),0) AS n FROM calculations WHERE user_id=%s", (user,)),
        ("SELECT tool_id,SUM(size_bytes) AS n FROM inline_results WHERE user_id=%s GROUP BY tool_id", (user,)),
        ("SELECT tool_id,SUM(input_bytes+result_bytes) AS n FROM jobs WHERE user_id=%s GROUP BY tool_id", (user,)),
        ("""SELECT j.tool_id,SUM(a.size_bytes) AS n FROM artifacts a JOIN jobs j ON j.id=a.job_id
            WHERE j.user_id=%s AND j.state='succeeded' AND a.kind='output' GROUP BY j.tool_id""", (user,)),
    ]
    for query, args in queries:
        for item in conn.execute(query, args):
            per_tool[item["tool_id"]] = {"savedBytes": per_tool.get(item["tool_id"], {}).get("savedBytes", 0) + int(item["n"] or 0)}
    used_slots = conn.execute("SELECT count(*) AS n FROM jobs WHERE state=ANY(%s)", (list(policy.ACTIVE),)).fetchone()["n"]
    return {"storage": {"usedBytes": row["used_bytes"], "reservedBytes": int(reserved(conn, user)), "limitBytes": limit(user)},
            "cpu": {"usedSlots": used_slots, "totalSlots": policy.MAX_JOBS, "enabled": policy.COMPUTE_ENABLED},
            "tools": per_tool}
