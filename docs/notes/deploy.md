# Deploying Incident Commander (U19, KTD18)

This guide puts one Docker host (a Hetzner VPS, or any Linux box with Docker) in front of the
internet:

```text
browser ──https──> app.<domain>   (Vercel, Next.js)
   │
   └──https + cookie, SSE──> api.<domain>  (Caddy :443) ──> api:8000 (uvicorn, 1 worker)
                                                             │
                                          internal network ──┘──> falkordb-prod:6379 (ACL)
```

Only 22, 80 and 443 are open. FalkorDB has no host ports and no Browser, and its network is
`internal`, so it has no internet route at all. The API logs in as the `ic` ACL user; the
`default` user is disabled.

Everything below runs from the repo root on the server unless it says otherwise.

## 1. Provision

- Hetzner Cloud **CPX21** (3 vCPU, 4 GB) or larger, Ubuntu 24.04, with your SSH key. Disable
  password login. FalkorDB is capped at `FALKORDB_MAXMEMORY` (default 2gb). Each live session
  holds a world copy plus snapshots, which is why `IC_MAX_SESSIONS` defaults to 8.
- Install Docker Engine and the Compose plugin (Compose v2.23 or newer is needed for inline
  `configs.content`):

  ```bash
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker $USER && newgrp docker
  docker compose version
  ```

- Clone the repo: `git clone <repo> incident-commander && cd incident-commander`.

## 2. DNS

Add two records for your domain:

| Name | Type | Value |
|---|---|---|
| `api.<domain>` | A (and AAAA if the VPS has IPv6) | the VPS address |
| `app.<domain>` | CNAME | `cname.vercel-dns.com` (Vercel shows the exact target) |

Caddy gets the certificate for `api.<domain>` on first start (HTTP-01 on port 80), so the A
record has to resolve before `up`.

## 3. Configure `.env` (repo root)

```bash
cp .env.example .env
chmod 600 .env
```

Set at least the following:

| Variable | Value |
|---|---|
| `DOMAIN` | `example.com` (Caddy serves `api.example.com`; CORS allows `https://app.example.com`) |
| `ACME_EMAIL` | your email, for Let's Encrypt |
| `IC_COOKIE_DOMAIN` | `.example.com` (the cookie is shared by `app.` and `api.`; Secure and SameSite=Lax) |
| `FALKORDB_IC_PASSWORD` | `openssl rand -hex 32` (it goes into a URL, so keep it hex) |
| `FALKORDB_PASSWORD` | `openssl rand -hex 32` (the operator `admin` user) |
| `IC_TRUSTED_PROXIES` | leave as `["172.30.213.10"]` (Caddy's fixed edge address in `docker-compose.yml`) |
| `IC_ALERT_HMAC_SECRET` | `openssl rand -hex 32` if you want signed alerts; leave empty to refuse them all |
| `IC_LLM_API_KEY` | optional (see section 7). Without it, the deterministic agent runs and verdicts are tagged DEGRADED |
| `IC_GITHUB_TOKEN`, `IC_GITHUB_REPO` | optional (see section 8). Without them, PRs and statuses are simulated |
| `IC_DISCORD_WEBHOOK_URL` | optional. Without it, pages are simulated |
| Limits | the defaults are the public limits (KTD10): `IC_MAX_SESSIONS=8`, `IC_DAILY_USD_BREAKER=5`, `IC_ACTIONS_PER_HOUR=20`, per-IP/session run caps, `IC_PROOF_VERIFIES_PER_IP_HOUR=12` |

`docker-compose.yml` overrides the container-specific values itself: `IC_FALKORDB_URL` (the
`ic` user), `IC_STORE_PATH=/app/data/ic.sqlite3`, `IC_WEB_ORIGIN=https://app.${DOMAIN}`
(set `IC_WEB_ORIGIN_PROD` to change it) and `IC_COOKIE_SECURE=true`.

`falkordb-prod` refuses to start when either FalkorDB password is shorter than 24 characters.

## 4. Start

```bash
docker compose --profile prod up -d --build
docker compose --profile prod ps     # falkordb-prod and api become "healthy"
docker compose --profile prod logs -f caddy   # wait for "certificate obtained successfully"
curl -s https://api.<domain>/health  # {"status":"ok","falkordb":true}
```

Every service has `restart: unless-stopped`, so the stack comes back after a reboot (Docker
itself starts on boot).

## 5. Seed the world

```bash
docker compose exec api ic seed --seed 42
```

The seed builds into `seed:world:building` and then `GRAPH.COPY`s it onto `seed:world`, so
re-seeding never leaves a half-built world. It prints a content hash, which must match the one
in the README (`879505a4…`). With `IC_LLM_API_KEY` set, postmortem embeddings come from the
API, so seed after setting the key. The world persists in the `falkordb-prod-data` volume
(AOF). Check that it survives a restart:

```bash
docker compose --profile prod restart falkordb-prod api
docker compose exec falkordb-prod sh -c \
  'redis-cli --no-auth-warning --user ic --pass "$FALKORDB_IC_PASSWORD" GRAPH.LIST'   # lists seed:world
```

## 6. Web on Vercel

1. Import the repo in Vercel and set the **root directory** to `web/` (pnpm is detected from
   `pnpm-lock.yaml`).
2. Add the environment variable `NEXT_PUBLIC_API_URL=https://api.<domain>` (Production and
   Preview). It is baked in at build time, so redeploy after you change it.
3. Under Domains, add `app.<domain>`.
4. Open `https://app.<domain>`, press **Inject fault**, and confirm that the run streams. In
   DevTools, check that the `ic_sid` cookie has the domain `.<domain>` and is Secure and
   HttpOnly. Do this in both Safari and Chrome (U19 verification).

The browser calls the API directly with `credentials: include`. SSE is never proxied through
Vercel. Preview deployments on `*.vercel.app` are a different origin, so CORS refuses them.
That is intended.

## 7. OpenAI budget (if `IC_LLM_API_KEY` is set)

- Create a dedicated OpenAI **project** for the demo and a project-scoped key.
- Set a monthly **budget** with a hard limit (for example $20) and an email alert at 50%. This
  is the backstop. The in-app breaker (`IC_DAILY_USD_BREAKER`, from logged token usage) trips
  first and serves recorded runs.
- Limit the project to the models in use (`gpt-4.1-mini`, `text-embedding-3-small`).

## 8. GitHub PAT scope (if real PRs)

- Use a **fine-grained** PAT on a dedicated sandbox repo, with only that repository selected.
- Grant these permissions: **Contents: read/write**, **Pull requests: read/write**, **Commit
  statuses: read/write**. Grant nothing else (no admin, no workflows).
- Set the expiration to the day after judging ends.
- The sandbox repo holds `services/{service}.yaml` manifests. PRs open on `ic/rollback-*`
  branches. Their `incident-commander/proofs` status links to `https://app.<domain>/proof/<ref>`.
- Auto-close: every 10 minutes the API closes `ic/rollback-*` PRs older than one hour on that
  repo (fork branches are never touched) and deletes their branches and `pr:` graphs.
  `IC_ACTIONS_PER_HOUR` caps real PRs, pages and status re-runs. Over the cap they are
  simulated.

## 9. Firewall: only 22, 80, 443

Use both layers:

- **Hetzner Cloud Firewall** (in the console): allow inbound TCP 22 (ideally from your IP
  only), TCP 80, TCP 443 and UDP 443 (HTTP/3). Drop everything else. Docker's iptables rules
  bypass `ufw` for published ports, so the cloud firewall is the one that counts.
- **Host**: `sudo ufw default deny incoming && sudo ufw allow 22/tcp && sudo ufw allow 80/tcp && sudo ufw allow 443 && sudo ufw enable`.

Compose publishes only Caddy's 80/443. `falkordb-prod` and `api` have no `ports:`.

## 10. Port-scan check (from another machine)

```bash
nmap -Pn -p- --min-rate 2000 api.<domain>        # expect: 80/tcp open, 443/tcp open (22 if not IP-restricted)
nmap -Pn -sU -p 443 api.<domain>
redis-cli -h api.<domain> -p 6379 ping           # must time out / refuse
curl -sI http://api.<domain>:3000                # must fail (no Browser)
curl -sI https://api.<domain>/health | grep -i strict-transport-security
```

On the server, `docker compose --profile prod ps` should show no `0.0.0.0` mapping except
Caddy's 80/443.

## 11. Uptime monitor

- Add an HTTP(S) monitor on `https://api.<domain>/health` that checks for the keyword
  `"falkordb":true` every 1 minute (UptimeRobot, Better Stack or Hetzner's own). Alert by
  email or phone.
- Add a second monitor on `https://app.<domain>/`.
- Optional: an alert on FalkorDB memory. `docker compose exec falkordb-prod redis-cli --user
  admin --askpass INFO memory` shows `used_memory_human` against `maxmemory`. With
  `noeviction`, a full instance fails writes. New sessions then error and visitors fall back
  to recorded runs.

## 12. Operations

- **Logs**: `docker compose --profile prod logs -f api` (a cleanup pass, PR auto-close and any
  GitHub errors are logged there).
- **Update**: `git pull && docker compose --profile prod up -d --build`. Sessions are
  in-memory runs plus SQLite (`api-data` volume), so a restart ends live runs but keeps
  limits, anchors and PR tracking.
- **Operator shell on FalkorDB**: `docker compose exec falkordb-prod redis-cli --user admin --askpass`.
- **Backups** (optional): `docker run --rm -v incident-commander_falkordb-prod-data:/d -v $PWD:/b alpine tar czf /b/falkordb.tgz -C /d .`.
  The world can always be re-seeded from `--seed 42`.

## 13. After judging: rotate and revoke

1. **Discord**: delete the webhook (Server Settings, Integrations, Webhooks) and create a new
   one if the demo stays up. The old URL was in `.env`.
2. **GitHub**: revoke the PAT (or let it expire). Close any remaining `ic/rollback-*` PRs.
3. **OpenAI**: delete the project key, or lower the project budget to $0.
4. **Alert HMAC secret and FalkorDB passwords**: regenerate them in `.env`, then run
   `docker compose --profile prod up -d --force-recreate`.
5. To take the demo down: `docker compose --profile prod down` (add `-v` to delete the
   volumes), delete the DNS records, and remove the Vercel domain.

## Verified locally (no public server yet)

- `docker build -f api/Dockerfile api` builds. The container runs as uid 10001, `/app/src`
  is read-only, and `/health` answers against FalkorDB.
- `falkordb-prod` was started from this compose file with throwaway passwords and no host
  ports. Unauthenticated and `default` logins are refused. `ic` can run `GRAPH.QUERY`,
  `RO_QUERY`, `COPY`, `DELETE`, `LIST`, `EXPLAIN` and `PROFILE` on `seed:*`, `s:*`, `pr:*`
  and `b:*` (the `falkordb_acl` grant in `docker-compose.yml`; the console's query X-ray
  uses `EXPLAIN` and `PROFILE`).
  `ic` gets NOPERM for other keys, for a `GRAPH.COPY` into another namespace, and for
  `GRAPH.CONFIG`, `GRAPH.UDF`, `KEYS`, `SET`, `FLUSHALL`, `CONFIG`, `ACL` and `CLIENT KILL`.
  The Browser process does not run.
- The `api` service then ran against it as `ic`: `ic seed --seed 42` (same content hash), a
  full inject run (PR, commit status, page, all simulated), the public proof page and a
  re-verify (VERIFIED). The seed world survived a `restart`.
- `caddy validate` accepts the `Caddyfile` with `caddy:2.10.2-alpine`.
