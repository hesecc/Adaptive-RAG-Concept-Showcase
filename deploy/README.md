# an-RAG server deployment artifacts

These artifacts target the approved `/opt/adaptive-rag` namespace. The existing host
Nginx terminates HTTPS; no new Caddy container is installed for this topology.
Check active and reserved ports, available memory/CPU/disk, host and cloud
firewalls, Compose version and certificate trust before activating them.

The optional PostgreSQL/Milvus/S3 topology is documented separately in
[STORAGE.md](STORAGE.md). It uses a new Compose project and is not an overlay
or an automatic migration of this currently deployed topology. The backup
procedure below does not cover remote databases, object storage or its worker.

## Files and private server state

| Repository artifact | Installed server path |
| --- | --- |
| `compose.server.yaml` | `/opt/adaptive-rag/compose.yaml` |
| `nginx.server.conf` | `/etc/nginx/conf.d/an-rag.conf` |
| `logrotate.server.conf` | `/etc/logrotate.d/an-rag` |
| `backup.sh`, `snapshot.py` | `/opt/adaptive-rag/infra/` |

Create `/opt/adaptive-rag/.managed` containing exactly `an-rag:standalone:v1`.
Create independent `config`, `data`, `secrets`, `tls`, `logs`, `backups`, `infra`,
`sources` and `releases` directories; never symlink these to another project.
Keep the management root, private config, secrets and backups owner-restricted.
The data bind is owned by application UID/GID `10001:10001` with writable access.
Host Docker mounts the bind directly, so the container needs data permissions,
not permission to traverse the host management root.

`deployment.env` contains the fixed release image, for example
`RAG_IMAGE=an-rag/app:release-<source-sha>`. `config/app.env` contains application
configuration and secrets, excluding SSH credentials; both are private files.
Compose `env_file.format: raw` requires Compose **2.30.0 or newer** and preserves
literal dollar signs/quotes in environment values. Use unquoted `KEY=value`
lines in this runtime file; do not `source` it in a shell.

Use an independently issued server certificate and private key at
`tls/server.crt` and `tls/server.key`. The current IP template requires an
`DNS:rag.example.invalid` SAN. Publish only the public CA/certificate and its fingerprint.
Never copy another application's private key. Nginx listens on `8443`; the API
binds only to `127.0.0.1:18000`. Both are candidate ports until host preflight
confirms them. The existing `18443`, `14173` and reserved `14174` remain untouched.

## Start and change a release

All commands must name this project's env and Compose file explicitly. Before
the initial start, validate the image and files, resource limits, data ownership
and health response. For subsequent releases, first take a successful backup
and keep the previous image and deployment parameters.

```bash
docker compose --project-name an-rag --env-file /opt/adaptive-rag/deployment.env -f /opt/adaptive-rag/compose.yaml config --quiet
docker compose --project-name an-rag --env-file /opt/adaptive-rag/deployment.env -f /opt/adaptive-rag/compose.yaml up -d --wait --wait-timeout 180
curl --fail http://127.0.0.1:18000/health
/www/server/nginx/sbin/nginx -t
/www/server/nginx/sbin/nginx -s reload
```

Only install the new vhost and reload after loopback verification and a
successful whole-host Nginx configuration check. Preserve its old configuration
and remove/revert only the new an-rag vhost if the new configuration fails.
Verify the public HTTPS response with strict CA/IP validation and test protected
routes both with and without a login session. Pass `Cookie` and `Set-Cookie`
unchanged so browser login works through the gateway. The application uses its
own `an_rag_session` cookie; do not rename it or clear another project's cookies.
Existing Bearer clients remain supported for maintenance.

The `/knowledge` and `/images` shells require `/static/page-owner.js` before
their page scripts. Include it in the release's static asset inventory (the
package-data rule is `static/*.js`). Preserve `X-RAG-Tenant` and `X-RAG-Principal`
in both directions, including on authenticated errors and binary responses.

All release changes and container CLI writes must acquire the same
`/opt/adaptive-rag/.maintenance.lock` used by the backup script. Do not run CLI writes
or extra application processes during the cold snapshot. The lock is advisory;
it does not magically stop a separately launched writer that ignores it.

## Gateway rate limits

`nginx.server.conf` limits requests per client address with three `limit_req`
zones. A request is counted by the first location that matches it.

| Zone | Rate and burst | Covers |
| --- | --- | --- |
| `an_rag_requests` | 30 r/min, burst 10 | Everything else: login, every write, `/health`, model and search endpoints, JSON reads such as `/documents` and `/assets` |
| `an_rag_media` | 300 r/min, burst 120 | Image bytes: `/assets/<id>/thumbnail`, `/assets/<id>/content` and the figures inside evidence, `/evidence/<generation>/<evidence>/images/<key>` |
| `an_rag_pages` | 300 r/min, burst 60 | Read-only page loads: `/`, `/images`, `/knowledge`, `/evaluation`, `/static/*`, `/console/session`, `/access/domains` |

Every response is `no-store`, so switching pages fetches the HTML, scripts,
stylesheet and both session reads again. Under the strict zone alone, two page
switches within a few seconds exhausted its burst and a random script or API call
got 429: the browser showed only the background, or fell back to the login form.
The pages location accepts GET only (`limit_except GET`), so a write can never
reach the generous zone. Keep new write, login and model routes out of it.

Never change the key of an existing `limit_req_zone`. `nginx -t` still passes,
but `nginx -s reload` fails with `[emerg] ... uses the "..." key while
previously it used the "$binary_remote_addr" key`, and the old configuration
keeps serving. Add a zone under a new name instead. Rejections are logged
(`limiting requests`) in `/opt/adaptive-rag/logs/nginx-error.log` with the zone name.

## Backup and isolated restore verification

Install `backup.sh` and `snapshot.py` together, owned by the deployment operator
and not writable by the app. Requirements are Bash, Python 3.11+, `flock`,
Docker Compose and standard GNU/Linux file tools. The scripts never execute
Compose `down`, delete volumes, run Docker prune or stop any other project.

```bash
sudo bash /opt/adaptive-rag/infra/backup.sh
sudo python3 /opt/adaptive-rag/infra/snapshot.py restore-check /opt/adaptive-rag/backups/<completed-backup-id>
```

The backup takes a project maintenance lock, validates paths and ownership
marker, stops only an existing `an-rag/api` writer, archives **all data plus
config, deployment parameters, TLS and private secrets**, and verifies every
member hash. An EXIT/INT/TERM trap starts an app that was previously running,
including after a partial stop or failed archive. An app previously stopped
remains stopped. A failed restart is a critical nonzero result requiring manual
attention. SIGKILL, host crash and power loss cannot run shell traps.

Completed backups contain `snapshot.tar.gz`, `manifest.json`, and `SHA256SUMS`.
Unfinished backups stay under `.partial-*`; they are never accepted for restore
checks. Archive checksums detect accidental corruption, not a malicious actor
who can rewrite the archive and checksums. Backups contain credentials and must
remain root/operator-only; do not commit or serve them over HTTP.

Restore checks extract only into a new private `.restore-check-*` directory
inside this project's backups directory. They reject links, special files,
unknown members and path traversal, verify archive hashes and the project
marker, run SQLite integrity/foreign-key checks, and verify every content-addressed
object and stored revision object reference. The source data remains unchanged.
Successful and failed isolated copies are retained as private evidence and take
disk space; remove only explicitly inspected copies during planned maintenance.
This verifies files, databases and object recovery, **not** a production restore,
application boot from restored data, model quality, or off-host backup coverage.

## Rollback

**Conversation-history upgrade:** SQLite and PostgreSQL knowledge schema v2 are
incompatible with the previous image. Fence all writers before upgrading, keep
a verified pre-upgrade backup, and disable image-only fallback. To roll back,
restore the complete v1 backup to a separate target and reconcile newer access
revocations and deletions before serving traffic. See the [upgrade and rollback
procedure](../docs/deployment.md#会话索引升级与回退sqlite--postgresql); do not run
the older data-domain downgrade script on v2.

Hold the project maintenance lock, preserve the failed release evidence and
set `RAG_IMAGE` back to the recorded previous image. Run the scoped Compose
configuration check and `up -d --wait`, then verify loopback/public API and other
projects' unchanged container IDs and endpoints. Image rollback is valid only
when persisted schema/data remain compatible; otherwise test a backup in an
isolated directory before planning a data restore. Never overwrite production
data from the restore checker. First-deployment withdrawal disables only the
new an-rag vhost, validates/reloads Nginx and stops only an-rag/api, retaining
its data for inspection.

### Rolling back past the data-domain release

The data-domain release (`rag-20261117T041936Z`) added three SQLite columns,
and the previous release writes with positional `INSERT ... VALUES (...)`. On an
upgraded database the previous image still starts and `/health`, reads and login
stay green, but creating an account or ingesting a document fails with 5xx. An
image-only rollback is therefore not a rollback. Under the maintenance lock:

1. Stop only `api`, so nothing writes while the schema changes.
2. Run `deploy/downgrade_schema.py` inside the new image as the application user
   (it drops exactly `documents.data_domains`, `revisions.created_at` and
   `accounts.data_domains`; the docstring has the full `docker run` command with
   `--network none --read-only --cap-drop ALL --user 10001:10001`). Keep stdout:
   it is the JSON export of the document classifications and account domain
   grants that the downgrade deletes. Progress goes to stderr. The tool needs
   SQLite 3.35 or newer, which the image provides, and a second run changes
   nothing.
3. Run the same command with `--check` and confirm both databases report
   `none (already previous shape)`.
4. Restore the recorded vhost, `deployment.env`, Compose file and backup scripts
   from `config/rollback-<release>/`, then `nginx -t` and reload.
5. Start the previous image with `up -d --wait`, then prove it can write: create
   and delete a temporary account. A green health check alone is not proof.

The cold backup taken before the release stays the authoritative recovery point.
This path was rehearsed on a copy of production data and covered with stubbed
Docker, Compose and Nginx; it has not been triggered on the production server.
