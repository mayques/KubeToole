"""Fixed service policy. GB is decimal; Kubernetes Gi is binary."""
import os
from pathlib import Path

CPU = "1250m"
RAM = "3Gi"
MAX_JOBS = 2
REGULAR_ACCOUNTS = 6
USER_BYTES = 25_000_000_000
OWNER_BYTES = 50_000_000_000
SCRATCH_BYTES = 5_000_000_000
DATA_FREE_BYTES = 20_000_000_000
SYSTEM_FREE_BYTES = 5_000_000_000
DEADLINE_SECONDS = 2700
MAX_INPUT_BYTES = 65_536
MAX_RESULT_BYTES = 49_152
MAX_FILES = 128
MAX_UPLOAD_BYTES = 500_000_000
UPLOAD_CHUNK_BYTES = 1_048_576
UPLOAD_SECONDS = 3600
UPLOAD_ACTIVE = ("receiving", "uploaded", "validating")
ACTIVE = ("launching", "pending", "running", "cancelling")
OWNER_SUBJECT = os.getenv("OWNER_SUBJECT", "")
DATA_ROOT = Path(os.getenv("DATA_ROOT", "/data"))
WORKER_NAMESPACE = os.getenv("WORKER_NAMESPACE", "kubetoole-compute")
WORKER_IMAGE = os.getenv("WORKER_IMAGE", "")
WORKER_API = os.getenv("WORKER_API", "http://calculator-service.default.svc.cluster.local:8000")
COMPUTE_ENABLED = os.getenv("COMPUTE_ENABLED", "false").lower() == "true"
