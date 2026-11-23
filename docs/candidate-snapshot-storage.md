# Candidate snapshot storage

A pending generation is an immutable manifest in `candidate_snapshots` and
`candidate_revisions`, with its staged job IDs in `candidate_jobs`. It has no
row in `snapshots`, `snapshot_revisions` or `current_snapshots`. Activation
checks the recorded base against the tenant's live generation, materializes
the published manifest with the same generation and timestamp, marks all jobs
published, and moves the pointer in one transaction. A stale candidate cannot
be activated by supplying a newer expected generation. Rebuild against the new
base, or abandon the pending generation. Published history remains readable.

## Reads and prewarm

Normal reads (including explicit generation and evidence lookup) see only
published snapshots. Trusted preview requires authorization by the caller and
`allow_candidate=True` on both the manifest and content reads. Preview does not
bypass normal document permissions. The separate trusted operator method
`list_prewarm_chunks(snapshot, *, allow_candidate=False)` returns all and only
non-revoked children of the verified manifest, including restricted subjects,
data domains, and all SKUs. Its output contains document text. Never expose it
to normal queries or use its count as proof that embeddings have been written.

## Rollback and mixed versions

Candidate-unaware **readers** cannot open pending generations in this schema:
the tables they read contain only published manifests. Candidate-unaware
**writers** do not honor job reservations: legacy single-job publication can
expose part of a pending batch. Do not create candidates until all writers are
upgraded; do not roll back to an old writer with pending candidates. Offline
SQLite-to-PostgreSQL migration copies pending manifests, base generations and
job reservations (and activated history); it is not a cleanup mechanism.

Before downgrading, quiesce all writers, back up the database and inventory
pending generations in the configured knowledge database/schema:

```sql
SELECT tenant_id, generation, base_generation FROM candidate_snapshots
WHERE activated = 0 ORDER BY tenant_id, generation;
```

For each unwanted result, run the candidate-aware adapter's
`store.abandon_candidate_snapshot(tenant_id, generation)` while ordinary
writers remain stopped. This removes that manifest and its reservations only;
jobs remain staged unless already published by another activated candidate.
A job still reserved by another pending candidate is not released for direct
publication. Abandon returns true once, false for a missing generation, and
rejects an activated candidate. Recreating an abandoned batch gets a new
generation, so delayed cancellation cannot delete its replacement. Re-run the
inventory and verify it is empty before enabling old writers. Do not just drop
the candidate tables or alter the current pointer. Abandoning a manifest does
not erase separately managed vector caches or object-store contents.
