"""One trusted tool per pod. The parent terminates the entire process group on timeout/cancel."""
import json
import os
import signal
import subprocess
import sys
import urllib.request

import policy
from tools import execute


class Context:
    def __init__(self):
        self.base = policy.WORKER_API.rstrip('/') + '/internal/jobs/' + os.environ['JOB_ID']
        self.token = os.environ['JOB_TOKEN']

    def request(self, route, value=None, raw=None):
        req = urllib.request.Request(self.base + route,
            data=raw if raw is not None else json.dumps(value if value is not None else {}, ensure_ascii=False, allow_nan=False).encode(),
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/octet-stream" if raw is not None else "application/json"})
        # No retry: uncertain outcomes are reconciled by the API, never rerun by this process.
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.load(response)

    def output(self, name):
        return Writer(self, name, "output")

    def temporary(self, name):
        return Writer(self, name, "temp")

    def open_input(self, artifact_id):
        from uuid import UUID
        req = urllib.request.Request(self.base + '/inputs/' + str(UUID(str(artifact_id))),
                                     headers={"Authorization": "Bearer " + self.token})
        return urllib.request.urlopen(req, timeout=15)


class Writer:
    """Append-only binary storage stream. Workers have no writable filesystem mount."""
    def __init__(self, context, name, kind):
        self.context = context
        self.id = context.request('/files', {"name": name, "kind": kind})['id']
        self.size = 0
        self.closed = False

    def write(self, data):
        if self.closed:
            raise ValueError("Output is closed")
        view = memoryview(data)
        for offset in range(0, len(view), 1_048_576):
            chunk = bytes(view[offset:offset + 1_048_576])
            self.context.request(f"/files/{self.id}/reserve", {"size": self.size + len(chunk)})
            self.context.request(f"/files/{self.id}/write?offset={self.size}", raw=chunk)
            self.size += len(chunk)
        return len(view)

    def tell(self):
        return self.size

    def flush(self):
        pass  # Each chunk is sent immediately; fsync happens on complete.

    def __enter__(self):
        return self

    def __exit__(self, error_type, error, traceback):
        self.closed = True
        if error_type is None:
            self.context.request(f"/files/{self.id}/complete")

    def open_reader(self):
        if not self.closed:
            raise ValueError("Complete the file before reading it")
        req = urllib.request.Request(self.context.base + f"/files/{self.id}",
                                     headers={"Authorization": "Bearer " + self.context.token})
        return urllib.request.urlopen(req, timeout=15)

    def discard(self):
        if not self.closed:
            raise ValueError("Complete the temporary file before discarding it")
        self.context.request(f"/files/{self.id}/discard")


def execute_child():
    context = Context()
    # The parent already claimed the job once. Inputs are not included in argv or logs.
    inputs = json.loads(os.environ['TOOL_INPUTS'])
    try:
        result = execute(os.environ['TOOL_ID'], inputs, context)
        context.request('/finish', {"outcome": "succeeded", "result": result})
    except Exception:
        try:
            context.request('/finish', {"outcome": "failed"})
        except Exception:
            pass
        return 1
    return 0


def run():
    context = Context()
    job = context.request('/claim')
    env = {**os.environ, 'TOOL_ID': job['tool_id'], 'TOOL_INPUTS': json.dumps(job['inputs'], ensure_ascii=False)}
    child = subprocess.Popen([sys.executable, __file__, '--execute'], env=env, start_new_session=True)
    def terminate(signum, frame):
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        raise SystemExit(1)
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    try:
        return child.wait(timeout=max(1, job['remaining_seconds']))
    except subprocess.TimeoutExpired:
        terminate(None, None)


if __name__ == '__main__':
    try:
        sys.exit(execute_child() if '--execute' in sys.argv else run())
    except Exception:
        print('Worker failed; consult job status.', file=sys.stderr)
        sys.exit(1)
