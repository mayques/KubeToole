# Deployment — manual, not performed by these edits

These files extend the existing `default/calculator` Deployment, `calculator-service:8000`, `postgres-service` and `postgres-secret`. They do not replace PostgreSQL, ingress, TLS, or ZITADEL. The owner confirmed that the API container is named `kubetoole-calculator` (the Deployment is named `calculator`). The strategic merge patch must match that container name.

## Current deployed account configuration

- `OWNER_SUBJECT=unassigned-owner`; this identifier is not assigned to a login.
- **Six normal accounts total, including Léo**, each with **25 GB** shared across tools.
- The special **50 GB** owner allowance remains in code but stays **unused**. The internal `unassigned-owner` database record is not a seventh usable account.
- Total allowance for usable accounts is **150 GB**. Unused logical allowance does not physically reserve disk space.

Léo explicitly requested this configuration without code changes. Preserve it in future deployment patches unless instructed otherwise. The owner confirmed a successful rollout using API/worker image `ghcr.io/mayques/kubetoole-calculator@sha256:bfd50b7fe8b0be20ea76f9094e0da6e98b24638ab1f2c90f79ef7cfafa2bb1c0`.

## Prepare

1. The owner already completed a PostgreSQL backup and restore test for this rollout, then explicitly requested skipping further database-protection checks or backup copies. Do not repeat those steps for this walkthrough. Do not copy Secrets or kubeconfig into this repository.
2. Confirm the node is `ov-1f810b`, `/mnt/data` is actually the mounted SDB filesystem, and the existing database is `/mnt/data/postgresql`. Verify this mount is present after reboot before starting workloads. Check actual allocatable CPU/RAM and baseline load; the recorded server is 4 vCPUs / 12 GB RAM. The proposed API cap is 500m / 512 MiB, separate from worker limits.
3. Confirm at least 20 GB free on SDB, plus room for both 5 GB scratch reservations and outputs, and at least 5 GB free on SDA. Container images, logs, PostgreSQL metadata and WAL need their own headroom. Root filesystem free space inside the API is used as the SDA guard; verify the container runtime actually stores it on SDA.
4. Create `/mnt/data/kubetoole` **on SDB**, owned by UID/GID 10001 and mode 0700. Do not change the ownership or contents of `/mnt/data/postgresql`. This new path is mounted only by the API. PV `capacity: 200G` declares capacity; it does not impose a disk quota.
5. Preserve `OWNER_SUBJECT=unassigned-owner` for the current six-normal-account configuration. Do not replace it with Léo's ZITADEL user ID: that would grant his account the special 50 GB allowance he declined. Startup rejects excess historical accounts without deleting data.
6. Publish the tested image through the existing GitHub workflow, then obtain its immutable `ghcr.io/mayques/kubetoole-calculator@sha256:...` digest. Ensure the node can pull it. Public GHCR packages need no credentials; if the package is private, configure pull access in **both** namespaces without committing credentials.

## Review and apply

Use the local Python environment with PyYAML to render a concrete patch:

```sh
python k8s/render.py --owner-id unassigned-owner --image ghcr.io/mayques/kubetoole-calculator@sha256:YOUR_DIGEST > /tmp/kubetoole-api-patch.yaml
```

The renderer rejects placeholders and mutable image tags. Review the generated file. Both API and worker must use that same image digest. If your namespace, node, service or container names differ, adapt the maintained YAML and `WORKER_API` / `WORKER_NAMESPACE` settings first.

On the server, after the checks above:

```sh
kubectl apply -f k8s/compute.yaml -f k8s/storage.yaml
kubectl -n default wait --for=jsonpath='{.status.phase}'=Bound pvc/kubetoole-data --timeout=60s
kubectl patch deployment calculator -n default --type strategic --patch-file /tmp/kubetoole-api-patch.yaml --dry-run=server
kubectl patch deployment calculator -n default --type strategic --patch-file /tmp/kubetoole-api-patch.yaml
kubectl rollout status deployment/calculator -n default
```

`compute.yaml` creates a dedicated restricted namespace, the two-worker resource quota, and permissions for the API to create/get/list/delete Jobs and get/list pods there. Workers receive no service-account credentials. It does not add network isolation; the current Flannel setup has no verified NetworkPolicy enforcement.

The Deployment uses **Recreate**, so expect a short interruption. On startup the API applies additive, versioned SQL migrations, counts existing calculator history, and validates owner/data/image configuration. The current PostgreSQL Secret is reused. No old history is dropped. Do not run old and new API versions concurrently: the old version does not maintain the new quota accounting.

Publish these website assets together **after** the API is ready:

- `mayques.com/calculator/calculator.html`
- `mayques.com/calculator/account-dashboard.js`
- `mayques.com/calculator/csv-files.js`
- `mayques.com/calculator/auth.js`

Keep the existing ZITADEL redirect URL. The login request now explicitly includes the API project's audience scope. Existing sessions with a different audience may need a fresh login.

### CSV release

The CSV changes are local until the next build and rollout. They reuse the existing PV/PVC and compute namespace; **no new mount, permanent pod, dependency or database secret is needed**. Keep the volume rooted at `/mnt/data/kubetoole` on SDB and mounted at `/data`. Uploads go into its `uploads/<uuid>/data.csv` region; outputs remain under `jobs/<uuid>/outputs/`. The API creates subdirectories as UID 10001. Never expose these directories through ingress or static hosting.

Deploy the new image for **both** API and workers; startup applies `002_csv.sql`. Keep `COMPUTE_ENABLED=true` so validation and job cleanup work. The four website assets above must match the API release. Existing old output files are not retroactively marked as validated CSVs.

Each public upload request is at most **1 MiB**, even for a 500 MB file. If custom ingress body-size/time limits exist, allow 1 MiB raw requests and up to 60 seconds per upload chunk; no 500 MB buffered request or multipart proxy configuration is needed. Native downloads stream through `POST /downloads/consume`; keep streaming responses enabled. Application request-body and Uvicorn concurrency caps remain in place.

After rollout, check one valid upload/download, one malformed CSV rejection, and cross-account denial. Confirm original bytes survive download, validation uses a limited worker, cancellation waits for termination, and quota usage changes correctly. Check the native download in the browsers you use and monitor API RAM during a large transfer. Local tests cannot prove browser/ingress behaviour or the live host's resource enforcement.

## Verify before enabling a public heavy tool

- `/health` succeeds; existing calculator and history still work for two distinct accounts; each sees only its own history and files.
- Usage reflects saved and reserved data; all six usable accounts, including Léo, receive 25 GB, and a seventh normal account is denied.
- In a test cluster/image containing a test tool, start two workers and reject a third; reject a second job for one account. Inspect actual container CPU/RAM caps and namespace quota.
- Test cancellation during startup and computation, RAM exhaustion, quota exhaustion and incomplete output. A slot must remain occupied while the old worker is still terminating. Check that no failed job reruns.
- Verify the 45-minute startup-inclusive deadline and process termination; confirm temporary/partial data is removed and previously saved outputs survive. Test an API restart during execution and cleanup, and an unavailable Kubernetes API.
- Monitor node load, disk free space and API/database responsiveness under two workers before relying on the nominal remaining 1.5 vCPUs.

The repository tests simulate Kubernetes and cannot establish these host-level results. The public catalog contains only the lightweight Calculator; CSV uploads also invoke an internal validation worker. Further heavy tools become available when their folders are bundled in a new release. Setting `COMPUTE_ENABLED=false` rejects new heavy launches and CSV uploads; **do not** use it while jobs are active because it also stops job monitoring. Cancel/drain active jobs first. Keep the API running for cleanup.

## Recovery

If the API restarts, durable reservations survive and the monitor continues observing/cleaning jobs without rerunning them. If Kubernetes cannot be reached, slots stay held. Investigate stuck/unknown pods on the single node before manually releasing any reservation; a force-deleted pod object is not proof that an unreachable process stopped.

If rollout fails, keep the existing data and fix configuration. After CSV uploads exist, a rollback must retain CSV reservation accounting and cleanup: the earlier quota-aware image does not understand pending uploads. Do not resume writes using it without reconciling those reservations. The pre-platform API also bypasses job/output accounting. Never delete the database or the retained PV as a rollback shortcut.
