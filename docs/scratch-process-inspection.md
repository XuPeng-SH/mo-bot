# Scratch cleanup process inspection

Scratch cleanup only detaches a terminal task's temporary files after checking
live turns, all live thread references and bind mounts. If `/proc` cannot be enumerated
or a process's references cannot be read, cleanup is deferred and the reason is
reported in `status.scratch_cleanup.errors`. A hidden-process proc mount is
also refused. This keeps an unreadable process from being mistaken for an idle
task; it can prevent automatic cleanup on normal unprivileged Linux services.

For a host installation that needs cleanup while other users or root processes
run, an administrator may install the optional read-only inspector from the
same source revision as the service:

```sh
sudo sh scripts/install-proc-inspection.sh "$(id -un)"
```

The installer places a root-owned inspector at
`/usr/local/libexec/mo-bot-proc-scan` and a validated, per-user sudo rule in
`/etc/sudoers.d/`. The rule allows that exact command with no arguments. The
inspector accepts only existing, non-symlinked directories owned by the invoking
user under a `jobs` or `scratch-trash` directory that other users cannot write.
Normal scans return only the requested directory IDs that are referenced.
Diagnostic scans additionally return process generation, namespace, cgroup
and root-mount identities; neither mode returns process environment, command
lines, file-descriptor contents or file contents. The inspector has a fixed
timeout and never deletes or modifies task files.

The service checks that the installed helper and its parent directories are
root-owned and not writable by other users, and that the helper exactly matches
the running source. Update the helper with the same command whenever
`mo_bot/proc_inspection.py` changes. An absent, outdated, unauthorized, timed
out or incomplete inspector leaves scratch intact and exposes an error. The
inspector must see the host's process namespace and `/proc`; do not use a
container-local process view to infer that host processes are absent.

If another mount namespace exposes the host filesystem as its root, the
inspector cannot reliably distinguish an ordinary cloned mount from a container
root bind. It defers new detachment and reports the ambiguous process. Some
systemd services create such namespaces, so this conservative rule can prevent
automatic reclamation even when the privileged inspector is installed. Existing
detached trash is checked independently and is only retried when its own
references can be verified. An open directory handle or current working
directory on the jobs root, trash root, or their direct state-directory parent
protects every child. More distant ancestors are not treated as ownership:
ordinary processes commonly hold directories such as `/home` or `/`. Each
trash tree is checked again immediately before deletion, because deleting an
earlier tree can take a long time. This requires an additional process scan per
tree.

An operator may explicitly trust specific service process identities. This is
a judgment about those services, not proof that a foreign root is not a bind:
the service could begin using an old task after the final process snapshot.
Use the same installed code as the service and an existing state directory:

```sh
python3 -m mo_bot.scratch_control --state-dir /path/to/state diagnose
```

The command requires the privileged inspector and at least one existing owned
job or detached trash directory. Review each `eligible` service's cgroup,
PID, process start time, namespace, mount and device. Container `.scope`
cgroups and unrecognized identities cannot be selected. To replace the
entire trust policy with individually reviewed entries, copy their `entry_id`
values from the diagnostic:

```sh
python3 -m mo_bot.scratch_control --state-dir /path/to/state set-trust \
  --entry ENTRY_ID_1 --entry ENTRY_ID_2
```

The command performs a fresh diagnostic and refuses identities that changed
since they were displayed. It writes a private, owner-controlled policy
atomically; no wildcard or automatic service selection is supported. A record
only ignores the selected process's exact host-root mount. All live threads,
current file handles, working directories and other mounts are still scanned.
Trust is bound to the boot and process generation and is not inherited across
service restarts; cleanup defers again until an operator reviews new identities.
To revoke all entries for subsequent scans, run:

```sh
python3 -m mo_bot.scratch_control --state-dir /path/to/state set-trust --clear
```

The coordinator's inherited cwd on the state directory is ignored only when
its exclusive `service.lock` is verified against that process and descriptor.
Its explicit directory handles and references from other processes remain
protection fences. Built-in local and remote backends started from the state
directory use a different initial cwd; individual turns still receive their
own workspace. Injected backend commands without an owner root retain their
original cwd and can still protect the trees. The optional inspector also runs
outside the state directory so it does not protect every tree merely by
inspecting it. A separate shell left in the state directory still protects the
trees, including the shell that launched a foreground service; leave that
directory to let cleanup proceed.

Deletion uses a bounded number of directory descriptors, checks directory
identity and kernel mount IDs, and avoids traversing nested mounts, including
bind mounts on the same filesystem. It normally moves between parent and child
descriptors instead of reopening the entire path. When the current thread's
mount namespace changes, it rechecks every visible ancestor before proceeding;
if the event monitor fails, cleanup is deferred. If a path exceeds the procfs
path-rendering limit, it falls back to rechecking its components from the trash
root and verifies that the trash directory has not moved through an ancestor
symlink. This fallback can take substantially longer on very deep paths. If
reference or mount identity cannot be verified, the tree is left for retry.

The process inspection is a snapshot, so the service's terminal-state and
live-turn fences must also prevent new task work from being launched while
scratch is detached. Rechecking each detached tree reduces the stale-snapshot
window but does not eliminate it. An unrelated process that starts using a tree
after its final check cannot be reliably detected without a shared ownership or
lease protocol. Mount events in another namespace or immediately after a check
also require such coordination for a strict guarantee.
