# KubeToole

A shared API and PostgreSQL database, with temporary Kubernetes workers for heavy tools. The calculator stays lightweight. All changes here are local source/configuration; publishing an image does **not** deploy it.

## Fixed policy

| Resource | Limit |
|---|---|
| Heavy workers | 2 globally; 1 per account; no queue |
| CPU / RAM per worker | 1.25 vCPU (`1250m`) / 3 GiB |
| Duration | 45 minutes including startup; up to 10 seconds to terminate |
| Execution retries | None |
| Saved data | 25 GB per usable account, including Léo, across all tools |
| Accounts | 6 normal accounts total, including Léo; 150 GB total allowance |
| Special owner allowance | 50 GB remains in code but is unused in the deployed configuration |
| Temporary files | 5 GB per job, on SDB, removed when the job ends |
| Minimum disk free space | 20 GB on SDB; 5 GB on SDA |
| CSV uploads | 500 MB per file; 128 saved/pending files per account |
| Concurrent uploads | 2 globally; 1 per account; incomplete uploads expire after 1 hour |

These constants live in [`calculator/policy.py`](calculator/policy.py). GB is decimal; GiB is binary. Two heavy workers use at most 2.5 vCPUs / 6 GiB, leaving a nominal 1.5 vCPUs for the rest of the recorded 4-vCPU server. Actual baseline capacity still needs measurement before deploying heavy tools.

The deployed setting is `OWNER_SUBJECT=unassigned-owner`, an identifier not assigned to a login. Startup creates its internal account record, but it is not a seventh usable account. Léo chose the normal 25 GB allowance and counts within the six normal accounts. The special 50 GB allowance stays unused; no code change was made for this choice. Unused allowance does not physically reserve disk space.

Account admission happens on first authenticated API use; it does not prevent someone registering with ZITADEL. `OWNER_SUBJECT` is a trusted server setting, never a client-supplied role. Existing calculator history is preserved and counted by the migration. Startup refuses excess existing accounts rather than deleting data.

## How enforcement works

PostgreSQL transactions and a shared advisory lock serialize admission, quota reservations, and job state changes. The API commits a job reservation before asking Kubernetes to create a worker. The compute namespace also has a two-pod resource quota. Cancellation records the request immediately; the slot stays occupied until the worker is confirmed stopped. During an API/cluster outage, uncertain jobs keep their reservations. The monitor resumes cleanup after restart; it never re-executes a tool. No second queue, scheduler service, or permanent worker deployment is needed.

Workers use the same immutable image as the API, with a different command. Each has a one-job credential, no database credentials, no Kubernetes service-account token, a read-only filesystem and no SDB mount. Their only persistent/temporary writes go through the shared storage API: reserve bytes, write bounded chunks, seal the file, then publish it after successful worker termination. A failed, cancelled, timed-out, or oversized job loses its incomplete outputs; previously completed jobs remain intact. Requests that fail quota checks are rejected; the worker reports failure and stops.

Saved usage counts UTF-8 calculator expressions plus eight bytes per result, inline result JSON, retained job input/result JSON, completed output file bytes, and validated CSV uploads. Pending outputs and uploads reserve account space. Temporary data has its separate 5 GB cap. Database indexes, row metadata, WAL and logs consume platform headroom, not the logical user allowance. Physical free-space checks conservatively subtract all outstanding reservations, even if partly written, so admission can stop earlier than the displayed account limit. Existing external services can also consume disk; these checks are not a filesystem quota for PostgreSQL or the whole host.

Only reviewed, server-bundled tools are supported. This is not a sandbox for user-submitted Python or arbitrary container images. Flannel alone is not an enforced network-isolation policy. Tools requiring writable local scratch paths need a separately designed, hard-limited storage adapter; do not add a writable mount that bypasses this interface.

## Code and APIs

- `calculator/main.py`, `auth.py`: HTTP routes and ZITADEL JWT verification.
- `database.py`, `migrations/`, `quotas.py`: additive schema, admission and usage accounting.
- `jobs.py`, `cluster.py`: launch, observation, timeout, cancellation and cleanup.
- `storage.py`, `worker.py`: quota-controlled binary streams and single-execution runner.
- `uploads.py`, `csv_format.py`, `downloads.py`: private CSV storage, bounded validation, and downloads.
- `tools/<id>/tool.json` + `tool.py`: trusted tool definitions and executable code. See [tool contract](calculator/tools/README.md).
- `k8s/`: maintained manifests and [deployment instructions](k8s/README.md). `SDA` remains a historical copy, not a deployment source.

Existing `POST /calculate` and `GET /history` contracts remain available. The dashboard uses `GET /tools`, `/account/usage`, `/jobs`; `POST /tools/{id}/run`; and `POST /jobs/{id}/cancel`. Job status and completed output downloads are owner-filtered. `/internal/jobs/...` requires the scoped worker credential; it does not accept an ordinary user JWT. Request JSON is limited to 64 KiB; result JSON to 48 KiB. Files use chunks of at most 1 MiB, with at most 128 live files per job.

The separate website's `calculator/calculator.html`, `account-dashboard.js`, `csv-files.js` and `auth.js` must be published together after the new API. It polls status every five seconds while visible, discovers tools automatically, shows storage and compute use, and provides Cancel job / Stopping / Cancelled states. The workspace stays empty until a tool is selected. Login, registration and logout retain their ZITADEL redirects. Calculator is the only public tool; the internal CSV validator uses the existing limited Kubernetes jobs.

## Private CSV storage

The deployment fixes the API volume at **`/mnt/data/kubetoole` on SDB**, mounted as `/data` in the API. Only the API mounts it. PostgreSQL remains separately at `/mnt/data/postgresql`.

| Data | SDB location |
|---|---|
| Uploaded CSV | `/mnt/data/kubetoole/uploads/<generated-uuid>/data.csv` |
| Tool output | `/mnt/data/kubetoole/jobs/<job-uuid>/outputs/<generated-uuid>` |
| Temporary tool data | `/mnt/data/kubetoole/jobs/<job-uuid>/scratch/<generated-uuid>` |

These are server-controlled paths, never public URLs or client-supplied destinations. Original filenames are display/download metadata. PostgreSQL records each file's owner; every listing, write, validation, removal, worker input and download checks that owner. Knowing another user's file ID grants no access. Workers stream their own account's files through `context.open_input(id)`.

The accepted format is UTF-8 (an optional UTF-8 BOM is accepted), comma-separated fields, standard double-quote escaping, a unique nonempty header, and at least one data row. All rows must have the same column count. Bounds: 256 columns, 128 characters per header, 65,536 characters per field, and 1,048,576 characters per logical record. Quoted newlines are supported; invalid encoding, malformed quotes, NUL and other unsupported control characters are rejected. No delimiter/type detection, compressed uploads, automatic resume, or execution retries.

The upload size limit is exactly **500,000,000 bytes**. The API reserves the full declared size before accepting sequential chunks of at most 1 MiB. `/complete` seals and fsyncs the file; `/validate` starts the internal CSV validation job, using the same two global slots, per-account slot, CPU/RAM cap and 45-minute deadline. If compute is busy, the sealed file waits for an explicit **Validate CSV** click; there is no queue. It expires one hour after upload creation if validation has not started. Validation starts from the beginning and streams bounded records. Only a successful, stopped worker makes the file usable/downloadable. Failures and cancellation remove its bytes before releasing the reservation. The monitor resumes cleanup after API restarts.

Files retain their original bytes. Cells are never evaluated, passed to a shell, interpolated into SQL, or rendered as HTML. Structural CSV validity does **not** establish tool-specific semantics: each tool must check its required columns, numeric types, ranges and algorithm costs. Spreadsheet software on the user's computer can interpret formulas in an original CSV; the download UI explains this without silently altering data.

Upload endpoints: `GET/POST /uploads`, then `POST /uploads/{id}/write?offset=...`, `/complete`, `/validate`, or `/remove`. Remove releases saved CSV bytes; validation-job history remains counted. Remove is blocked while that account has a worker that could be reading the file. Pending upload cancellation/expiry clears partial files; active validation is cancelled through the ordinary job API. Rejected-file metadata expires after a day.

Downloads: authenticated `GET /uploads/{id}/download` and `GET /artifacts/{id}` return only owned, validated CSVs as attachments. The website uses authenticated `POST /downloads` with `{kind: "upload" | "artifact", id}` to obtain a single-use, five-minute ticket, then submits it in a native HTML form to `POST /downloads/consume`. Ownership/readiness is rechecked when consuming it. Tickets are hashed in PostgreSQL, limited to two outstanding per account, and never placed in URLs. This lets the browser stream a large download without a JavaScript blob. Responses disable caching and MIME sniffing. Native download completion/errors appear in the browser download manager.

CSV tool outputs are structurally validated by the worker SDK when their output stream closes. Other output formats remain usable internally but have no public download. The 500 MB cap applies to uploads; outputs use the account quota and job limits.

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

There is no billing/plan system, automatic deployment, job rerun facility, or general import interface for formats beyond CSV.

Local CSV verification (26 November 2026): 70 backend tests and 11 website DOM tests passed. A synthetic 500,000,000-byte stream validated in 61.24 seconds with 17.4 MB peak parser-process RSS on the development Mac. This checks bounded parser memory; browser/ingress transfer behaviour and actual Kubernetes resource enforcement remain deployment checks.
