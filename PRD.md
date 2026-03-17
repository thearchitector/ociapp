<!-- pragma: no ai -->
# OCIApp Architecture

This document outlines the initial architecture for a locally hosted, OCI-image-based application execution system. It is intended as a durable reference for future work.

## Goal

Provide a system that:

- packages customer applications as OCI images,
- runs those images locally or on a host-managed runtime,
- communicates with running applications over a Unix domain socket (UDS),
- treats application payloads as opaque `bytes`,
- keeps the contract between service and application narrow: a single `execute` function.

The system is divided into three packages:

1. `ociapp`
2. `ociapp-build`
3. `ociapp-runtime`

---

## System model

At a high level:

- application authors write code against `ociapp`, defining a single `execute` handler,
- application projects use `ociapp-build` to produce OCI image artifacts,
- a host service uses `ociapp-runtime` to start containers, talk to them over UDS, and reclaim them when idle.

Conceptually:

```text
User project
  ├─ depends on ociapp
  ├─ uses ociapp-build as build backend
  └─ builds to OCI image

Host service
  └─ depends on ociapp-runtime
       ├─ starts OCI containers
       ├─ connects to container UDS
       ├─ serializes/deserializes msgpack payloads
       └─ manages warm/idle lifecycle
```

---

## Package 1: `ociapp`

### Purpose

`ociapp` is the sandbox-side library. It is used inside OCI images by application code. Its responsibility is to expose a single execution interface over a Unix domain socket using an asyncio server.

### Primary responsibilities

- define the application contract,
- provide a server that listens on a UDS path,
- receive framed requests as raw `bytes`,
- decode request payloads via msgpack,
- invoke a user-defined `execute` function,
- encode the result via msgpack,
- return the response over the same socket.

### Contract model / public API

The application-facing contract is intentionally minimal:

- one function: `execute`
- input: user-defined Pydantic model validated using decoded msgpack bytes
- output: user-defined Pydantic model which will be dumped and encoded to msgpack

User applications should be able to define request and response models however they want, as long as they can be converted to and from msgpack-compatible structures.

A likely shape:

```python
class CustomApplication[RequestT, ResponseT](Application):
    @override
    async def execute(self, request: RequestT) -> ResponseT: ...
```

### Wire protocol

The wire protocol should be small and explicit.

Request/response bodies should be msgpack maps.

Recommended request shape:

```python
{"request_id": UUID, "payload": bytes}
```

Recommended response shape:

```python
{"request_id": UUID, "payload": bytes | None, "error": bytes | None}
```

This keeps transport stable while allowing internal payload schemas to evolve.

### Container expectations

Containers using `ociapp` must define `ociapp serve --app myapp.main:app` as their startup command, where `myapp.main:app` is the import path to the application instance.

This convention allows `ociapp-runtime` to treat containers uniformly.

---

## Package 2: `ociapp-build`

### Purpose

`ociapp-build` is the build tool. It packages a Python project into an OCI image file artifact.

This package exists so application projects can declare how they should be built without manually re-implementing container packaging logic.

### Primary responsibilities

- is a standlaone tool and does not interfere with the declared build backend,
- read project configuration from `pyproject.toml`,
- generate or coordinate an OCI image build,
- package the application code together with `ociapp`,
- produce a physical OCI image artifact suitable for storage and later execution,
- expose building via CLI: `ociapp-build /path/to/project/root`

### Configuration model

A likely `pyproject.toml` shape:

```toml
[tool.ociapp-build]
mode = "managed"  # or "custom"
system-packages = ["foo", "bar"]
entrypoint = "myapp.main:app"
```

### Expected behavior

Two build modes should exist.

#### 1. Managed build mode

The backend generates the OCI build structure itself.

Inputs:

- the current Python project (assume canonical pyproject.toml and `uv`)
- optional system packages
- entrypoint metadata

Behavior:

- uses a built-in Containerfile,
- injects requested system packages into Containerfile as build args,
- does not include source code in the image, only a pre-built wheel to install,
- uses `tini` as the container ENTRYPOINT
- uses `ociapp serve` as the CMD, with the specified entrypoint argument.

This should be the default option.

#### 2. Custom Containerfile mode

If `containerfile` is specified, the backend delegates image construction to that Containerfile.

Behavior:

- when `mode = "custom"` is specified, only `containerfile` config is permitted.
- backend performs no validate of any kind that the built image is "ociapp-compliant".

### Output

`ociapp-build` should produce a physical OCI image achive tar artifact, and not require a registry.

Filename convention:
```
{project_name}-{version}.ociapp
```

---

## Package 3: `ociapp-runtime`

### Purpose

`ociapp-runtime` is the host-side runtime library. It is responsible for starting OCI containers, communicating with them over UDS, and tearing them down when idle.

This is the execution engine used by services.

### Primary responsibilities

- load OCI image artifacts,
- start container instances,
- mount a host-visible runtime directory into each container,
- connect to the container's UDS socket,
- send msgpack-serialized requests,
- receive and decode responses,
- maintain a warm pool of running containers,
- stop containers after an idle timeout.

### Runtime model

A container instance is treated as a long-lived worker.

Container lifecycle:

1. image is assumed to be available locally as a `.ociapp` file,
2. container is started,
3. runtime waits for `/run/ociapp/app.sock` to become available,
4. runtime keeps a client connection to that socket,
5. requests are dispatched to warm instances,
6. idle instances are stopped after timeout.

This avoids per-request container startup overhead.

### Public-facing API

`ociapp-runtime` owns the client side of the same custom UDS protocol used by `ociapp`.

It should expose a transport-agnostic execution API such as:

```python
with Runtime() as runtime:
    result = await runtime.execute("/path/to/app.ociapp", payload)
```

Internally it is responsible for:

- unpacking and loading a `.ociapp` file,
- framing requests,
- msgpack encoding/decoding,
- request correlation,
- timeout enforcement,
- response parsing.

### Warm pool behavior

The runtime should manage instances per image.

For each image:

- keep zero or more warm instances,
- dispatch to an available instance,
- stop instances after configured idle timeout, or at runtime shutdown.

### Internal architecture

A reasonable initial implementation:

- runtime runs in its own thread,
- main service submits execution requests into a queue,
- assumes `podman` to start containers,

This keeps host service logic separate from runtime state management.

### Idle shutdown

Idle detection should be explicit and simple.

Each instance tracks:

- state (`starting`, `ready`, `busy`, `stopping`)
- last-used timestamp
- active request count

An instance is eligible for shutdown when:

- it is `ready`,
- it is not currently handling a request,
- idle time exceeds configured threshold.

### Future evolution

Later, `ociapp-runtime` may be split from the host service into its own process or node-local agent.

That split should be possible without changing the contract between service and application.

This suggests designing `ociapp-runtime` around a clean internal interface from the start.
