# Deploying to a Hetzner Cloud VPS

This walks through putting the app on a fresh Hetzner Cloud server, using
Docker Compose exactly as it runs locally. It assumes no prior Hetzner
account setup. Read [Caveats](README.md#caveats-read-this) in the README
before deploying this anywhere publicly reachable — it's a simulation, not
a live trading system, and its sign-in is a simple email match, not real
Google authentication.

Console steps are given as the primary path, with the `hcloud` CLI as an
optional alternative for each step (skip those blocks if you're using the
web console).

## 1. Create the server

**Console:** [console.hetzner.cloud](https://console.hetzner.cloud) → create
a project → **Add Server**:
- **Location**: whichever region is closest to you or your users.
- **Image**: Ubuntu 24.04.
- **Type**: a shared-vCPU **CX22** (2 vCPU / 4 GB RAM / 40 GB disk) is
  enough to run this app comfortably; go up a size if you expect to run it
  alongside much else on the same box.
- **SSH key**: add your public key here rather than using a password —
  paste the contents of `~/.ssh/id_ed25519.pub` (or generate one first with
  `ssh-keygen -t ed25519` if you don't have one).
- Leave **Volumes**/**Backups** off for now; both can be added later if
  you want extra disk or automatic snapshots.

Note the server's public IP once it's created.

**CLI alternative:**
```
hcloud context create my-project        # one-time, prompts for an API token from the console
hcloud ssh-key create --name my-key --public-key-from-file ~/.ssh/id_ed25519.pub
hcloud server create --name trading-app --type cx22 --image ubuntu-24.04 \
  --location nbg1 --ssh-key my-key
hcloud server list                      # note the IP
```

## 2. First login

```
ssh root@<server-ip>
apt update && apt upgrade -y
```

## 3. Install Docker

```
apt-get install -y ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | tee /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
docker compose version   # sanity check
```

## 4. Lock down the firewall

The app only needs to expose SSH (22) and the dashboard port (8000, or
80/443 if you put a reverse proxy in front — see step 7) to the internet.
**Postgres (5432) should never be reachable from outside the server** —
`docker-compose.yml` publishes it to the host for local development
convenience (connecting a DB GUI client), but on a public VPS the Hetzner
Cloud Firewall should block it before it ever reaches the box.

**Console:** project → **Firewalls** → **Create Firewall**. Add inbound
rules for TCP 22 (source `0.0.0.0/0, ::/0`) and TCP 8000 (same source; or
80+443 instead if you're adding HTTPS per step 7). Leave everything else
un-added — Hetzner Cloud Firewalls default-deny anything not explicitly
allowed. Then attach the firewall to your server under its **Firewalls**
tab.

**CLI alternative:**
```
hcloud firewall create --name web-firewall
hcloud firewall add-rule web-firewall --direction in --source-ips 0.0.0.0/0,::/0 --protocol tcp --port 22
hcloud firewall add-rule web-firewall --direction in --source-ips 0.0.0.0/0,::/0 --protocol tcp --port 8000
hcloud firewall apply-to-resource web-firewall --type server --server trading-app
```

This is a network-level firewall managed by Hetzner upstream of the VM —
it doesn't require anything installed on the server itself, and is simpler
to get right than hand-rolling `ufw` rules.

## 5. Get the project onto the server

From your own machine, copy the project directory up (this assumes you
have the project as a local folder — e.g. after unzipping the delivered
`fixed_project.zip`):
```
scp -r ./fixed_project root@<server-ip>:/opt/trading-app
```
Or, if you're keeping this in a git repo instead:
```
ssh root@<server-ip>
git clone <your-repo-url> /opt/trading-app
```

## 6. Configure secrets

```
ssh root@<server-ip>
cd /opt/trading-app
cp .env.example .env
```

Edit `.env` and fill in real values — **do not deploy with the defaults**:

```
AUTHORIZED_GOOGLE_EMAIL=you@example.com
SESSION_SECRET_KEY=$(openssl rand -hex 32)
POSTGRES_PASSWORD=$(openssl rand -hex 24)
WATCHLIST_TOKEN_ADDRESSES=<comma-separated real Solana token mint addresses>
```

`WATCHLIST_TOKEN_ADDRESSES` has no default — leave it unset and the
pipeline just logs a warning and idles instead of evaluating anything.
See [Real market data](README.md#real-market-data-stage-1) in the README
for what the other market-data env vars (`SOLANA_RPC_URL`, etc.) do and
where to find real token addresses.

This `up -d --build` does **not** start the Stage 3 signer service — it
stays off (both at the Compose level and behind
`ENABLE_STAGE3_EXECUTION=false`) unless you separately follow
[STAGE3_SETUP.md](STAGE3_SETUP.md), which has its own setup and its own
`signer_service/.env`. Nothing below in this guide changes if you skip
Stage 3 entirely.

(Run the two `openssl rand -hex ...` commands separately and paste their
output in — `.env` doesn't evaluate shell substitutions.) Use `openssl
rand -hex` specifically, not a generator that can produce a `$` character:
Docker Compose treats `$` specially when substituting `.env` values into
`docker-compose.yml`, and a stray `$` will silently corrupt the password
instead of failing loudly. Hex output never contains one.

## 7. Start it

```
docker compose up -d --build
docker compose ps                # both services should show healthy/running
docker compose logs -f web       # watch it come up; Ctrl+C to stop watching
```

Visit `http://<server-ip>:8000` and sign in with the Gmail address you put
in `.env`.

### Optional: HTTPS via a reverse proxy

Running the app directly on port 8000 over plain HTTP is fine for testing,
but for anything real, put [Caddy](https://caddyserver.com) in front of it
— it gets you a free, auto-renewing Let's Encrypt certificate with almost
no configuration. First point a domain's DNS **A record** at the server's
IP, then:

```
apt-get install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
apt-get update && apt-get install -y caddy
```

Then replace `/etc/caddy/Caddyfile` with:
```
your-domain.example.com {
    reverse_proxy localhost:8000
}
```
and `systemctl reload caddy`. Update the Hetzner Cloud Firewall (step 4) to
allow 80 and 443 instead of 8000, and you no longer need 8000 open to the
internet at all (Caddy talks to the app over `localhost`).

## Applying schema changes later

If you ever update `schema.sql` after this database volume already
exists, Postgres will **not** re-run it automatically — that only happens
the first time a volume is created. Apply the change by hand instead:
```
docker compose exec -T db psql -U postgres -d memecoin_trading < migrate.sql
docker compose restart web
```
`migrate.sql` is written to be safe to re-run (it only adds what's
missing) and won't touch data already in the tables.

## Updating the app

```
cd /opt/trading-app
# pull or re-upload your changed files, then:
docker compose up -d --build
```
This rebuilds only the `web` image; `db` and its data volume are
untouched unless you explicitly run `docker compose down -v` (which
deletes the volume — see the schema-changes note above for the
non-destructive path).

## Rotating the Postgres password

`POSTGRES_PASSWORD` is **required** (Compose refuses to start without it)
and must never be a default. The database volume keeps whatever password
it was *initialised* with, so changing `.env` alone locks the app out.
Rotate in this order -- the new value is generated locally, goes straight
into the database and `.env`, and is never printed:

Linux/macOS (bash):
```
NEW=$(openssl rand -hex 24)
printf "ALTER USER postgres PASSWORD '%s';\n" "$NEW" | docker exec -i trading_postgres_db psql -U postgres -d memecoin_trading
sed -i '/^POSTGRES_PASSWORD=/d' .env && echo "POSTGRES_PASSWORD=$NEW" >> .env
unset NEW
docker compose up -d
```

Windows (PowerShell):
```
$b = New-Object byte[] 24; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
$p = ($b | ForEach-Object { $_.ToString('x2') }) -join ''
"ALTER USER postgres PASSWORD '$p';" | docker exec -i trading_postgres_db psql -U postgres -d memecoin_trading
$lines = [IO.File]::ReadAllLines("$PWD\.env") | Where-Object { $_ -notmatch '^\s*POSTGRES_PASSWORD=' }
[IO.File]::WriteAllLines("$PWD\.env", [string[]]($lines + "POSTGRES_PASSWORD=$p"))
Remove-Variable p, b, lines
docker compose up -d
```

Plain `docker exec`, not `docker compose exec`: while `.env` lacks the
variable, every `docker compose` command refuses to parse the file -- by
design. The `ALTER USER` runs over the container's local socket, which the
postgres image trusts, so it needs no current password. The statement is
piped on stdin rather than passed as an argument, so it never appears in
a process list. `docker compose up -d` then recreates `web` with the new
`DATABASE_URL`. If you use the scoped `signer_svc` role, its password is
separate and lives only in `signer_service/.env`.

## Backups

The simplest approach is a scheduled `pg_dump` to a file, copied off the
server periodically:
```
docker compose exec -T db pg_dump -U postgres memecoin_trading > backup-$(date +%F).sql
```
Wire that into a daily cron job and copy the output somewhere off-server
(e.g. with `scp` or `rsync` from your own machine, or Hetzner Storage
Box). Separately, Hetzner also offers server-level **Snapshots** (on-demand,
paid per GB) and an automatic **Backups** add-on (~20% of the server's
price/month) if you'd rather back up the whole disk than just the
database.

## Troubleshooting

**"column ... does not exist" / "relation ... does not exist" in the
logs.** The `db` container only runs `schema.sql` the first time its data
volume is initialized. If you're redeploying onto a volume that already
existed (e.g. you rebuilt the `web` image but kept `db`'s volume from an
earlier version of this project), the new tables/columns never got
created. Fix it with `migrate.sql` per
[Applying schema changes later](#applying-schema-changes-later) above, or
start clean with `docker compose down -v && docker compose up -d --build`
(this deletes all existing data).

**"Sign-in is not configured."** `AUTHORIZED_GOOGLE_EMAIL` is empty in the
running container — almost always because the values were put into
`.env.example` instead of `.env` (Docker Compose only reads a file
literally named `.env`), or because `.env` was edited after the container
was already started. Fix `.env`, then `docker compose up -d --build` to
pick up the change.

**Can't reach the dashboard at all.** Check the Hetzner Cloud Firewall
(step 4) actually allows the port you're using (8000, or 80/443 if you set
up Caddy), and that `docker compose ps` shows `web` as running/healthy —
check `docker compose logs web` for a crash loop if not.
