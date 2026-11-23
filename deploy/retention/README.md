# Seven-day trash collection

Checking out these files does not enable or execute a purge. The restore window ends 168 hours after deletion. Physical removal is on the first successful pass at/after expiry, not necessarily at the exact deadline. The timer runs daily at 03:15 UTC with up to five minutes of jitter. Persistent=true catches a missed event after boot. Monitor failed units and the time of the last successful run.

## Which deployments are safe?

| Deployment | Required storage |
| --- | --- |
| Local, including cloud-model mode | One Linux host, shared local bind mount, SQLite knowledge/traces and images, SQLite image vectors, file objects. Cloud document embeddings, when used, live in the local cloud-vectors.sqlite3, not in Milvus. No model calls are needed. |
| Stock remote storage Compose | One Linux application host with a single shared lock/cache directory, dedicated PostgreSQL database (knowledge schema adaptive_rag and the image role's normal search path), dedicated S3 bucket/prefix and dedicated Milvus collection. Any cloud document embedding cache is **still on that local mount**. PostgreSQL and Milvus stay up. |

Use a dedicated **never-versioned** bucket. Verify the S3 provider implements GetBucketVersioning and DeleteObject; grant these permissions in addition to existing application access. Enabled/suspended versioning is refused: a delete marker does not destroy the bytes. Do not change bucket versioning, mount identity, catalogs, project, image, credentials or topology during a pass. Do not use age-based bucket expiration on content-addressed objects.

**Do not enable** for multiple hosts, separate caches, mixed selectors, shared objects/collections, additional reference catalogs, custom plugins, network filesystems, outside/unwrapped writers or concurrent restore/migration. A host-local advisory lock cannot fence other machines or arbitrary remote clients. Restrict credentials and access accordingly. Every writer (including queries, imports, prewarm, API, workers, CLI, one-off scripts and backups/restores) must use the same directory and cooperating writer entrypoint over its **whole** operation, including writes before SQL commit. Never run a bare CLI through docker exec against this installation. The runner checks expected container entrypoints, not unrelated clients. If exclusivity is unprovable, leave GC disabled.

Both metadata catalogs and the image vector store must have been initialized by this release even if empty. On a new docs-only installation exercise the authorized image listing to initialize lazy image storage. Never replace a missing or mis-mounted catalog with an empty one. The remote deployment refuses leftover local catalogs in the same directory. Do not point a different release/installation at a pending retry queue.

## Install on an isolated staging deployment first

From this directory on Linux with LF line endings:

~~~sh
install -d /etc/rag-retention /usr/local/libexec
install -m 0755 runner.sh /usr/local/libexec/rag-retention
install -m 0644 compose.*.yaml local.conf remote.conf /etc/rag-retention/
install -m 0644 rag-retention@.service rag-retention@.timer /etc/systemd/system/
systemctl daemon-reload
~~~

Review the chosen /etc/rag-retention/INSTANCE.conf. Use the **existing** project name, installed base Compose file and pinned deployment.env. No application secrets belong in the instance file. Deploy an immutable release containing both new retention modules. Apply the writer overlay before enabling collection. Example with the default local instance:

~~~sh
docker compose -p an-rag --env-file /opt/adaptive-rag/deployment.env -f /opt/adaptive-rag/compose.yaml -f /etc/rag-retention/compose.local.yaml up -d --wait
systemctl start rag-retention@local.service
journalctl -u rag-retention@local.service
systemctl enable --now rag-retention@local.timer
systemctl list-timers 'rag-retention@*'
~~~

For remote use project an-rag-storage, the paths from remote.conf, compose.remote.yaml and instance remote. The remote overlay fences **both API and asset-worker**. Include the overlay on every later deployment. For a foreground trusted CLI use those same Compose arguments followed by `run --rm --no-deps api python -m adaptive_rag ...`; keep the shared-lock entrypoint. Do not run the purge command online.

## What the run deletes

- Explicit trash only: mtime, revocation, unpublished status or legacy deleted flags do not start the clock. Admin restore restrictions do not extend the seven-day window.
- Expired document revisions, parsed chunks, jobs and event metadata, their historical manifest entries and affected pending candidates. The content-free document ID reservation survives. Blob candidates come only from persisted raw/parsed keys, original source keys, parser figure keys and their revision-specific bindings. Legacy PDF markers may preserve a potential reference but never authorize deletion.
- Images are selected by exact trash ID, incarnation and revision, not just visible asset ID. Originals, thumbnails, old revision/job rows and identifiable image search traces are removed. For legacy images with no incarnation ID only exact proven revision matches qualify; ambiguous historical revisions remain.
- Each catalog durably records blob candidates in the same transaction that deletes owner rows. After commit the pass checks **all tenants in both catalogs**, including staged/historical documents, image revisions, current images and other trash. Retries recheck owners acquired since the previous attempt. No orphan or prefix sweeps. Corrupt/inaccessible metadata prevents blob deletion.
- Unshared local document vector cache keys across model namespaces, native SQLite/Milvus image vectors for exact (tenant, asset, revision), and traces containing structured references to a purged item. Arbitrary human text is not matched. Model providers, exports, reports, original files outside application storage, previously copied responses and external parser services need independent retention policies.
- Primary-store deletion is not a forensic overwrite guarantee. SQLite uses secure_delete and truncates WAL; media snapshots, SSDs, database backups, PostgreSQL MVCC/WAL, S3 replicas and Milvus compaction have their own lifetimes. Budget for full-catalog scans and a service outage; measure with representative staging data.

## Failure and recovery

The runner saves immutable IDs of previously running writers under /var/lib/rag-retention-INSTANCE/restart-ids **before** stopping them. The collector takes a nonblocking exclusive flock on their data directory inode. A still-running wrapped CLI makes collection fail. Exit cleanup restarts only the recorded IDs, including after purge errors; systemd also invokes recovery in ExecStopPost. After abrupt host loss, the next service invocation recovers outstanding IDs first. Verify API health and worker state after a failure. An ID manually removed by an operator needs investigation; do not blindly start a different deployment.

A success prints counts only. Error output omits provider details, SQL parameters and secrets. Check mounts, schemas, supported selectors, permissions, storage capacity, active writers, backend health and authoritative inventory. Never clear retry tables or bypass the fence to silence a failure. Re-run the service once safe, or let the next daily pass retry. Disable the timer to stop future schedules; allow active maintenance to finish restarting writers.
