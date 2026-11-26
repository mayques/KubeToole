# Deployment — manual, not performed by these edits

These files extend the existing `default/calculator` Deployment, `calculator-service:8000`, `postgres-service` and `postgres-secret`. They do not replace PostgreSQL, ingress, TLS, or ZITADEL. Confirm those names and the existing API container name (`calculator`) before applying the patch.

## Prepare

1. Take a consistent PostgreSQL backup and verify restoration. Retain a copy of the current sanitized Deployment configuration and its image digest. Do not copy Secrets or kubeconfig into this repository.
2. Confirm the node is `ov-1f810b`, `/mnt/data` is actually the mounted SDB filesystem, and the existing database is `/mnt/data/postgresql`. Verify this mount is present after reboot before starting workloads. Check actual allocatable CPU/RAM and baseline load; the recorded server is 4 vCPUs / 12 GB RAM. The proposed API cap is 500m / 512 MiB, separate from worker limits.
3. Confirm at least 20 GB free on SDB, plus room for both 5 GB scratch reservations and outputs, and at least 5 GB free on SDA. Container images, logs, PostgreSQL metadata and WAL need their own headroom. Root filesystem free space inside the API is used as the SDA guard; verify the container runtime actually stores it on SDA.
4. Create `/mnt/data/kubetoole` **on SDB**, owned by UID/GID 10001 and mode 0700. Do not change the ownership or contents of `/mnt/data/postgresql`. This new path is mounted only by the API. PV `capacity: 200G` declares capacity; it does not impose a disk quota.
5. Identify the owner's immutable ZITADEL **user ID**, not email or a client/project ID. The initial cap is six regular application accounts plus this owner. Inspect account counts before rollout; excess historical accounts block startup without deleting data.
6. Publish the tested image through the existing GitHub workflow, then obtain its immutable `ghcr.io/mayques/kubetoole-calculator@sha256:...` digest. Ensure the node can pull it. Public GHCR packages need no credentials; if the package is private, configure pull access in **both** namespaces without committing credentials.

## Review and apply

Use the local Python environment with PyYAML to render a concrete patch:

```sh
python k8s/render.py --owner-id YOUR_ZITADEL_USER_ID --image ghcr.io/mayques/kubetoole-calculator@sha256:YOUR_DIGEST > /tmp/kubetoole-api-patch.yaml
```

The renderer rejects placeholders and mutable image tags. Review the generated file. Both API and worker must use that same image digest. If your namespace, node, service or container names differ, adapt the maintained YAML and `WORKER_API` / `WORKER_NAMESPACE` settings first.

On the server, after the checks above:

```sh
kubectl apply --dry-run=server -f k8s/compute.yaml -f k8s/storage.yaml
kubectl apply -f k8s/compute.yaml -f k8s/storage.yaml
kubectl patch deployment calculator -n default --type strategic --patch-file /tmp/kubetoole-api-patch.yaml --dry-run=server
kubectl patch deployment calculator -n default --type strategic --patch-file /tmp/kubetoole-api-patch.yaml
kubectl rollout status deployment/calculator -n default
```

`compute.yaml` creates a dedicated restricted namespace, the two-worker resource quota, and permissions for the API to create/get/list/delete Jobs and get/list pods there. Workers receive no service-account credentials. It does not add network isolation; the current Flannel setup has no verified NetworkPolicy enforcement.

The Deployment uses **Recreate**, so expect a short interruption. On startup the API applies additive, versioned SQL migrations, counts existing calculator history, and validates owner/data/image configuration. The current PostgreSQL Secret is reused. No old history is dropped. Do not run old and new API versions concurrently: the old version does not maintain the new quota accounting.

Publish these website assets together **after** the API is ready:

- `mayques.com/calculator/calculator.html`
- `mayques.com/calculator/account-dashboard.js`
- `mayques.com/calculator/auth.js`

Keep the existing ZITADEL redirect URL. The login request now explicitly includes the API project's audience scope. Existing sessions with a different audience may need a fresh login.

## Verify before enabling a public heavy tool

- `/health` succeeds; existing calculator and history still work for two distinct accounts; each sees only its own history and files.
- Usage reflects saved and reserved data; the seventh regular account is denied while the configured owner retains its place.
- In a test cluster/image containing a test tool, start two workers and reject a third; reject a second job for one account. Inspect actual container CPU/RAM caps and namespace quota.
- Test cancellation during startup and computation, RAM exhaustion, quota exhaustion and incomplete output. A slot must remain occupied while the old worker is still terminating. Check that no failed job reruns.
- Verify the 45-minute startup-inclusive deadline and process termination; confirm temporary/partial data is removed and previously saved outputs survive. Test an API restart during execution and cleanup, and an unavailable Kubernetes API.
- Monitor node load, disk free space and API/database responsiveness under two workers before relying on the nominal remaining 1.5 vCPUs.

The repository tests simulate Kubernetes and cannot establish these host-level results. The shipped catalog contains only the lightweight Calculator; heavy tools become available when their folders are bundled in a new release. Setting `COMPUTE_ENABLED=false` rejects new heavy launches; **do not** use it while jobs are active because it also stops monitoring. Cancel/drain active jobs first. Keep the API running for cleanup.

## Recovery

If the API restarts, durable reservations survive and the monitor continues observing/cleaning jobs without rerunning them. If Kubernetes cannot be reached, slots stay held. Investigate stuck/unknown pods on the single node before manually releasing any reservation; a force-deleted pod object is not proof that an unreachable process stopped.

If rollout fails, keep the existing data and fix configuration or deploy the previous **quota-aware** release. The pre-platform API can still read calculator history, but running it bypasses new accounting; do not resume writes with it and then blindly redeploy. Restore a consistent backup or reconcile usage before returning to the new platform. Never delete the database or the retained PV as a rollback shortcut.
