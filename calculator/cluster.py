"""Narrow Kubernetes adapter; no user-controlled pod specification."""
from functools import lru_cache
import policy
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException


class Cluster:
    def __init__(self):
        config.load_incluster_config()  # Never read a developer's kubeconfig implicitly.
        settings = client.Configuration.get_default_copy()
        settings.retries = 0
        api = client.ApiClient(settings)
        self.batch = client.BatchV1Api(api)
        self.core = client.CoreV1Api(api)

    def create(self, job_id, token):
        self.batch.create_namespaced_job(policy.WORKER_NAMESPACE, manifest(job_id, token), _request_timeout=(3, 10))

    def delete(self, job_id):
        try:
            self.batch.delete_namespaced_job(name=f"tool-{job_id}", namespace=policy.WORKER_NAMESPACE,
                body=client.V1DeleteOptions(propagation_policy="Foreground", grace_period_seconds=10),
                _request_timeout=(3, 10))
        except ApiException as error:
            if error.status != 404:
                raise

    def observe(self, job_id):
        pods = self.core.list_namespaced_pod(policy.WORKER_NAMESPACE,
                    label_selector=f"kubetoole-job={job_id}", _request_timeout=(3, 10)).items
        active = any(p.status.phase not in ("Succeeded", "Failed") or p.status.reason == "NodeLost" for p in pods)
        try:
            job = self.batch.read_namespaced_job(f"tool-{job_id}", policy.WORKER_NAMESPACE, _request_timeout=(3, 10))
        except ApiException as error:
            if error.status == 404:
                return {"exists": False, "active": active, "terminal": None}
            raise
        terminal = next((c.type for c in job.status.conditions or []
                         if c.status == "True" and c.type in ("Complete", "Failed")), None)
        return {"exists": True, "active": active, "terminal": terminal}

    def job_ids(self):
        rows = self.batch.list_namespaced_job(policy.WORKER_NAMESPACE,
                    label_selector="app=kubetoole-worker", _request_timeout=(3, 10)).items
        return [item.metadata.labels["kubetoole-job"] for item in rows]


@lru_cache
def cluster():
    return Cluster()


def manifest(job_id, token):
    labels = {"app": "kubetoole-worker", "kubetoole-job": str(job_id)}
    resources = {"cpu": policy.CPU, "memory": policy.RAM, "ephemeral-storage": "64Mi"}
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": f"tool-{job_id}", "labels": labels},
        "spec": {"parallelism": 1, "completions": 1, "backoffLimit": 0,
            "podReplacementPolicy": "Failed", "activeDeadlineSeconds": policy.DEADLINE_SECONDS,
            # Cleanup is API-controlled AFTER durable outcome/accounting; no TTL races.
            "template": {"metadata": {"labels": labels}, "spec": {
                "restartPolicy": "Never", "terminationGracePeriodSeconds": 10,
                "automountServiceAccountToken": False, "serviceAccountName": "worker",
                "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                                    "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [{"name": "worker", "image": policy.WORKER_IMAGE,
                    "command": ["python", "worker.py"], "resources": {"requests": resources, "limits": resources.copy()},
                    "securityContext": {"readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False,
                                        "capabilities": {"drop": ["ALL"]}},
                    "env": [{"name": name, "value": str(value)} for name, value in {
                        "JOB_ID": job_id, "JOB_TOKEN": token, "WORKER_API": policy.WORKER_API,
                        "TMPDIR": "/tmp",
                    }.items()],
                }],
            }}}
    }
