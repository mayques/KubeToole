# KubeToole

A shared API and PostgreSQL database, with temporary Kubernetes workers for heavy tools. The calculator stays lightweight. All changes here are local source/configuration; publishing an image does **not** deploy it.

## Fixed policy

| Resource | Limit |
|---|---|
| Heavy workers | 2 globally; 1 per account; no queue |
| CPU / RAM per worker | 1.25 vCPU (`1250m`) / 3 GiB |
| Duration | 45 minutes including startup; up to 10 seconds to terminate |
| Execution retries | None |
| Saved data | 25 GB per regular account; 50 GB for the owner, across all tools |
| Accounts | 6 regular accounts + owner; 200 GB total promised storage |
| Temporary files | 5 GB per job, on SDB, removed when the job ends |
| Minimum disk free space | 20 GB on SDB; 5 GB on SDA |

These constants live in [`calculator/policy.py`](calculator/policy.py). GB is decimal; GiB is binary. Two heavy workers use at most 2.5 vCPUs / 6 GiB, leaving a nominal 1.5 vCPUs for the rest of the recorded 4-vCPU server. Actual baseline capacity still needs measurement before deploying heavy tools.

Account admission happens on first authenticated API use; it does not prevent someone registering with ZITADEL. `OWNER_SUBJECT` is a trusted server setting, never a client-supplied role. Existing calculator history is preserved and counted by the migration. Startup refuses excess existing accounts rather than deleting data.

## How enforcement works

PostgreSQL transactions and a shared advisory lock serialize admission, quota reservations, and job state changes. The API commits a job reservation before asking Kubernetes to create a worker. The compute namespace also has a two-pod resource quota. Cancellation records the request immediately; the slot stays occupied until the worker is confirmed stopped. During an API/cluster outage, uncertain jobs keep their reservations. The monitor resumes cleanup after restart; it never re-executes a tool. No second queue, scheduler service, or permanent worker deployment is needed.

Workers use the same immutable image as the API, with a different command. Each has a one-job credential, no database credentials, no Kubernetes service-account token, a read-only filesystem and no SDB mount. Their only persistent/temporary writes go through the shared storage API: reserve bytes, write bounded chunks, seal the file, then publish it after successful worker termination. A failed, cancelled, timed-out, or oversized job loses its incomplete outputs; previously completed jobs remain intact. Requests that fail quota checks are rejected; the worker reports failure and stops.

Saved usage counts UTF-8 calculator expressions plus eight bytes per result, inline result JSON, retained job input/result JSON, and completed output file bytes. Pending outputs reserve account space. Temporary data has its separate 5 GB cap. Database indexes, row metadata, WAL and logs consume platform headroom, not the logical user allowance. Physical free-space checks conservatively subtract all outstanding reservations, even if partly written, so admission can stop earlier than the displayed account limit. Existing external services can also consume disk; these checks are not a filesystem quota for PostgreSQL or the whole host.

Only reviewed, server-bundled tools are supported. This is not a sandbox for user-submitted Python or arbitrary container images. Flannel alone is not an enforced network-isolation policy. Tools requiring writable local scratch paths need a separately designed, hard-limited storage adapter; do not add a writable mount that bypasses this interface.

## Code and APIs

- `calculator/main.py`, `auth.py`: HTTP routes and ZITADEL JWT verification.
- `database.py`, `migrations/`, `quotas.py`: additive schema, admission and usage accounting.
- `jobs.py`, `cluster.py`: launch, observation, timeout, cancellation and cleanup.
- `storage.py`, `worker.py`: quota-controlled binary streams and single-execution runner.
- `tools/<id>/tool.json` + `tool.py`: trusted tool definitions and executable code. See [tool contract](calculator/tools/README.md).
- `k8s/`: maintained manifests and [deployment instructions](k8s/README.md). `SDA` remains a historical copy, not a deployment source.

Existing `POST /calculate` and `GET /history` contracts remain available. The dashboard uses `GET /tools`, `/account/usage`, `/jobs`; `POST /tools/{id}/run`; and `POST /jobs/{id}/cancel`. Job status and completed output downloads are owner-filtered. `/internal/jobs/...` requires the scoped worker credential; it does not accept an ordinary user JWT. Request JSON is limited to 64 KiB; result JSON to 48 KiB. Files use chunks of at most 1 MiB, with at most 128 live files per job.

The separate website's `calculator/calculator.html`, `account-dashboard.js` and `auth.js` must be published together after the new API. It polls status every five seconds while visible, discovers tools automatically, shows storage and compute use, and provides Cancel job / Stopping / Cancelled states. The workspace stays empty until a tool is selected. Login, registration and logout retain their ZITADEL redirects. Only Calculator is bundled today; there is no heavy calculator job to cancel and no unrelated heavy demo tool in the production catalog.

## Local verification

Use Python 3.12 and a disposable local PostgreSQL database, never production:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
# Optional bundled temporary PostgreSQL for local runs without an existing test server:
pip install pgserver
pytest -q
```

Alternatively set `TEST_DATABASE_URL` to a local test PostgreSQL server. Tests create/drop their own schemas. CI runs PostgreSQL 17; the image publication job depends on passing tests. Tests cover real SQL transactions, migrations, account isolation, concurrent admission/reservations, JWT validation, worker streaming, cancellation, uncertain launches and interrupted cleanup. Kubernetes observations are simulated: these tests do not prove host-level enforcement.

With the sibling `mayques.com` folder present, run the website DOM checks without a live browser or network:

```sh
npm install --prefix /tmp/kubetoole-js-tests --no-audit --no-fund jsdom
NODE_PATH=/tmp/kubetoole-js-tests/node_modules node --test tests/dashboard.test.cjs
```

There is no new general upload/import interface, data-deletion UI, billing/plan system, automatic deployment, or job rerun facility in this foundation.
