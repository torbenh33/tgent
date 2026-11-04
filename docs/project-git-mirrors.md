# Remote project synchronization

**Status:** implemented for remote agents.

## Goal and scope

For remote agents, make the project available on the remote host while keeping Git history separate from working-tree file transfer. This first version supports a single Git repository and does not support submodules.

Assume SSH access is already configured for Git and rsync. SSH setup, credential provisioning, and submodule handling are out of scope.

## Project directory

Derive the remote project directory name from the local project directory path: normalize it, then replace path separators with underscores. For example, `/home/alice/work/widget` becomes `_home_alice_work_widget`. Store it beneath a configured remote project root. Use the same name for repeated syncs of that local directory.

Different local paths can map to the same name. Collision detection is best-effort: warn if a collision is detected, but do not add a disambiguator or reject the sync. If different project histories are pushed to the same bare repository, multiple root commits are acceptable. The name is a convenient mapping, not an authorization mechanism. The remote root and SSH destination are configured by the service; do not let project contents supply a remote command or destination.

## Sync workflow

For each remote-agent start or project sync:

1. Ensure a bare Git repository exists in the remote project directory.
2. Resolve the local `HEAD` to its commit SHA and push that commit to the remote repository. Capture the SHA as the agent's base commit; do not rely on the remote repository's mutable `HEAD` or a shared moving ref after the push. Concurrent pushes may update remote refs, but each agent must use the exact SHA it pushed.
3. Make the remote project repository available inside the agent sandbox as a read-only mount. Create the agent's private work directory by cloning that repository with `git clone --reference` pointing at the mounted repository, then check out the captured base-commit SHA. The clone intentionally depends on the referenced repository for its Git objects; do not use `--dissociate`.
4. Rsync the local working tree into the agent work directory, excluding only `.git`, so tracked edits, untracked files, and the rest of the current project contents (including `.venv/`, as today) are present without replacing the agent clone's Git metadata. Do not add other path exclusions.

For continuations, retain and use the agent's recorded base-commit SHA rather than resolving `HEAD` again. A new start may capture a new SHA.

Use argument-vector subprocess calls with configured SSH destinations and fixed remote roots. Avoid shell interpolation of project paths. Preserve the existing behavior that an unsuccessful transfer does not result in a successful agent start.

Repeated syncs may push the latest `HEAD` and rsync the working tree again. This design does not require atomic promotion, transaction manifests, or crash reconciliation; an interrupted sync can be retried.

## Boundaries and limitations

- The remote bare repository is shared Git object storage and must be mounted read-only inside each agent sandbox. Each agent has its own writable clone/work directory, which intentionally depends on the shared repository because it was created with `git clone --reference`; do not dissociate the clone. The mirror must remain available for as long as those clones need its objects.
- Mirror and agent-work-directory lifecycle management, including cleanup and garbage collection, is out of scope. The mirror may be removed manually when space is needed; no automatic cleanup is required in this version.
- Rsync updates working-tree files only. It does not copy the source index or `.git` metadata into the agent work directory.
- The captured commit SHA is the agent's Git base. Local uncommitted changes are transferred as working-tree content, not committed by the service.
- No submodule discovery, transfer, or publication is supported in this first implementation. Repositories containing submodules should be rejected with a clear error until support is added.
- This workflow is for remote-agent project synchronization; it does not define project registration, cross-agent commit publication/review, mirror backup/retention, or local-agent snapshots.

## Initial tests

1. A remote start captures and pushes the source commit SHA, clones with a reference, checks out that exact SHA, then rsyncs files while excluding `.git`.
2. Tracked edits, untracked files, and deletions in the local working tree are reflected according to the rsync options; `.git` is the only excluded path and the agent clone's Git metadata remains intact.
3. Concurrent pushes may move remote refs, but each start/continuation checks out its recorded base-commit SHA; a new sync transfers the current working tree again.
4. An interrupted push or rsync is reported as a failed sync; retrying performs the workflow again.
5. Project paths map consistently to remote directory names, and repositories with submodules fail clearly.
