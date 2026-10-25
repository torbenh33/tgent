# Remote agent runtime model

## Goal

Deploy one complete Python environment to `<remote_base_dir>/app` and use it
for both the persistent remote-agent service and sandboxed workers. Avoid
maintaining a second worker-runtime copy under `runtime/versions` or a separate
set of Python modules under `/opt/agent-runtime`.

## The deployed app is the runtime

The deployment artifact at `<remote_base_dir>/app` should contain the Python
environment and the installed tgent application code. The persistent service
runs from this environment, for example with its interpreter at
`<remote_base_dir>/app/bin/python` and `-m remote_agent_service`.

The worker uses the same interpreter and installed application code. Bubblewrap
mounts `<remote_base_dir>/app` read-only at that same absolute path inside the
sandbox. This keeps the interpreter and package paths stable while allowing the
project snapshot to remain separately mounted read-write. Runtime dependencies
must be in the deployed environment; worker imports must not depend on a
second copy of selected source files or on the operator's checkout path.

## Development and deployment

The normal operator workflow is:

```sh
.venv/bin/python deploy_remote_agent.py --skip-rootfs --install-project
```

The local `.venv` is the environment used to prepare the deployment, and the
current checkout is the application source to install. Development changes
should be installed into the deployment environment as part of deployment;
the deployed package must not be an editable install pointing back to a local
checkout that does not exist on Beep. The service and worker must therefore see
the same installed code and dependencies from `<remote_base_dir>/app`.

Virtual environments are not generally relocatable: copied scripts may retain
absolute interpreter paths, and environment metadata can refer to the original
Python installation. Deployment must validate the interpreter and entry points
at their final remote path rather than assume that copying a local venv makes
it usable. The concrete deployment procedure should preserve or deliberately
recreate the environment at the final path and fail before switching the service
if validation fails.

## Updates, versions, and rootfs

A deployment stops the service before updating it. Keep one current app/runtime
by default; do not publish every app update as another permanent worker runtime
version. If rollback is required, retain at most one previous complete app as
an explicit rollback artifact, with a clear cleanup policy. Existing agent
metadata may record which deployment was active when an agent started, but that
does not require retaining every old runtime indefinitely.

The sandbox RFS/rootfs is separate from this Python application runtime. The
normal deployment command uses `--skip-rootfs`; this model does not require
building or synchronizing multiple rootfs copies. Any rootfs update/versioning
policy should be designed separately and only if operationally needed.

## Implementation acceptance criteria

- `<remote_base_dir>/app` is the single deployed Python environment and contains
  the installed tgent code and dependencies.
- The persistent service starts using that environment.
- Sandboxed workers use the same interpreter and installed code, with the app
  tree mounted read-only at its normal absolute path.
- Worker imports do not rely on `/opt/agent-runtime` copies or a separate
  `runtime/versions` mirror.
- Deployment validates the final remote interpreter/package before enabling
  or restarting the service, and avoids leaving a partially updated live app
  on failure.
- `--skip-rootfs` continues to avoid rootfs build/synchronization.
