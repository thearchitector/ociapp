# OCIApp

OCIApp is a three-package workspace for packaging customer applications as OCI
artifacts and executing them locally through a narrow Unix domain socket
contract.

- `packages/ociapp`: sandbox-side SDK, direct `app = MyApplication()` contract,
  framing helpers, and UDS server
- `packages/ociapp-build`: standalone CLI that builds `.ociapp` OCI archives
- `packages/ociapp-runtime`: host-side runtime with Podman-backed warm workers

The repository also includes:

- `examples/echo-app`: a minimal application project configured for
  `ociapp-build`
- `examples/runtime_demo.py`: a small runtime consumer that sends repeated dict
  requests to a built artifact and receives decoded dict responses

Local validation:

```bash
uv run --no-sync pytest --cov
uv run --no-sync mypy .
uv run --no-sync prek run -a
```
