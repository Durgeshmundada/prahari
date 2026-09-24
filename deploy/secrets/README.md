# Local Compose secret files

The Compose scaffold mounts one secret per file. Keep real files only on the
authorized Linux host, outside source control and outside image build contexts.
Each file must contain exactly one value and no surrounding quotes.

| Real file | Contents | Mounted only into | Required Linux owner |
|---|---|---|---|
| `postgres_bootstrap_password` | PostgreSQL bootstrap-role password | PostgreSQL and post-migration grant job | UID 70, mode `0600` |
| `prahari_migrator_password` | migration-owner password | PostgreSQL initialization only | UID 70, mode `0600` |
| `prahari_app_password` | least-privilege runtime-role password | PostgreSQL initialization only | UID 70, mode `0600` |
| `prahari_migrator_database_url` | URL with the migrator password URL-encoded | migration job only | UID 10001, mode `0600` |
| `prahari_app_database_url` | URL with the app password URL-encoded | API only | UID 10001, mode `0600` |
| `prahari_api_keys_json` | JSON object mapping API tokens to actor identities and roles | API only | UID 10001, mode `0600` |

The URL files must use the Compose service hostname `postgres`, for example:

```text
postgresql+psycopg://prahari_app:URL_ENCODED_PASSWORD@postgres:5432/prahari
```

Never put raw passwords in `deploy/.env`, Compose environment variables, shell
history, command-line arguments, or a raw `PRAHARI_API_KEYS_JSON` environment
variable. Copy each matching `*.example` to the real file name, populate it
through the approved secret mechanism, then apply the table's owner and mode
through a root-managed process. The API-key file must be a one-line JSON object,
for example
`{"token-value":{"actor_id":"operator-1","role":"operator"}}`.
The validation script rejects placeholder values in real files.

## Linux ownership for local Compose

This local Compose scaffold uses `file:` secrets. Docker Compose implements
those as host bind mounts, so it does not remap the file owner, group, or mode
inside a container. The table above is therefore an operational requirement,
not just a host permission preference: the PostgreSQL Alpine image runs the
init/grant scripts as UID 70, while API and migration containers run as UID
10001. Provision real files with the listed numeric owner and mode `0600`, and
run real-secret validation and `docker compose` through `sudo` on a **rootful**
Linux Docker Engine. Rootless Docker and Docker user-namespace remapping are
not validated by this scaffold.

If `POSTGRES_IMAGE` changes from the pinned Alpine family, verify the image's
`postgres` UID and update this documented/validated ownership contract before
deployment. Docker Compose's documented file-secret limitation is described at
<https://docs.docker.com/reference/compose-file/services/#secrets>.
