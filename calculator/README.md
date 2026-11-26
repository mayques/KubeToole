# Application image

This directory contains the API, the common worker runner and bundled tools. The calculator remains a lightweight authenticated tool; heavy tools run in separate, limited Kubernetes Jobs.

See the [project README](../README.md) for limits, local tests and endpoint contracts, the [tool contract](tools/README.md) for adding executable code, and the [deployment guide](../k8s/README.md) for required owner, image and storage configuration. A bare `uvicorn main:app` without PostgreSQL, `OWNER_SUBJECT` and an existing `DATA_ROOT` is intentionally not a configured server.
