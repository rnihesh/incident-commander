# Deploying Incident Commander (U19, KTD18)

The public instance runs at **https://commander.niheshr.com** on one AWS EC2 host. Web and API
share that origin:

```text
browser ──https──> commander.<domain>  (Caddy :443, TLS from Let's Encrypt)
                     ├── /api/*, /mcp, /health ──> api:8000  (uvicorn, 1 worker, SSE unbuffered)
                     │                               └── internal network ──> falkordb-prod:6379 (ACL)
                     └── everything else ──────────> web:3000 (Next.js standalone server)
```

Only 22, 80 and 443 are open. FalkorDB has no host ports and no Browser, and its network is
`internal`, so it has no internet route at all. The API logs in as the `ic` ACL user; the
`default` user is disabled. The web container sits on its own network with Caddy and cannot
reach the API directly. The browser calls `/api/*` on the page's own origin, so the session
cookie is host-only and CORS allows exactly that origin.

Everything below runs from the repo root on the server unless it says otherwise.

## 1. Provision

- Any Linux host with Docker. The public instance is an EC2 `aarch64` instance with 2 vCPU,
  1.8 GB RAM and a 20 GB disk, running Ubuntu. Both images build natively on arm64 and x86_64.
- Memory: FalkorDB holds about 150 MB for the seed world plus about 58 MB per live session,
  the API about 120 MB, the web server about 50 MB and Caddy about 30 MB. On a host under
  2 GB, add a 2 GB swapfile (the Next.js build needs it) and size the session cap to fit:

  ```bash
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile
  sudo swapon /swapfile && echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
  echo vm.swappiness=10 | sudo tee /etc/sysctl.d/99-swap.conf && sudo sysctl -p /etc/sysctl.d/99-swap.conf
  ```

  The public instance uses `IC_MAX_SESSIONS=8`, `IC_IP_CONCURRENT_SESSIONS=3` and `FALKORDB_MAXMEMORY=700mb`. A 4 GB host can
  keep the defaults (`8` and `2gb`).
- Install Docker Engine and the Compose plugin (Compose v2.23 or newer is needed for inline
  `configs.content`):

  ```bash
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker $USER && newgrp docker
  docker compose version
  ```

- Clone the repo with a read-only deploy key (the repo is private):

  ```bash
  ssh-keygen -t ed25519 -N "" -f ~/.ssh/ic_repo_deploy
  # on a machine with gh: gh repo deploy-key add ic_repo_deploy.pub --read-only
  printf 'Host github.com\n  IdentityFile ~/.ssh/ic_repo_deploy\n  IdentitiesOnly yes\n' >> ~/.ssh/config
  git clone git@github.com:rnihesh/incident-commander.git ~/incident-commander
  ```

## 2. DNS

One A record (and AAAA if the host has IPv6) for the site name, for example
`commander.<domain>`, pointing at the host. Caddy gets the certificate on first start
(HTTP-01 on port 80), so the record has to resolve and port 80 has to be reachable before `up`.

## 3. Configure `.env` (repo root)

```bash
cp .env.example .env
chmod 600 .env
```

Set at least the following:

| Variable | Value |
|---|---|
| `SITE_ADDRESS` | `commander.example.com` (Caddy serves it; CORS allows `https://commander.example.com`) |
| `API_SITE_ADDRESS` | optional, e.g. `api.commander.example.com`: a second hostname that serves only the API, for direct clients. Needs its own DNS record. The web app keeps calling `/api` on `SITE_ADDRESS` |
| `ACME_EMAIL` | your email, for Let's Encrypt |
| `IC_COOKIE_DOMAIN` | empty (host-only cookie; Secure and SameSite=Lax) |
| `FALKORDB_IC_PASSWORD` | `openssl rand -hex 32` (it goes into a URL, so keep it hex) |
| `FALKORDB_PASSWORD` | `openssl rand -hex 32` (the operator `admin` user) |
| `IC_MAX_SESSIONS`, `FALKORDB_MAXMEMORY` | sized to the host (section 1) |
| `IC_TRUSTED_PROXIES` | leave as `["172.30.213.10"]` (Caddy's fixed edge address in `docker-compose.yml`) |
| `IC_ALERT_HMAC_SECRET` | `openssl rand -hex 32` if you want signed alerts; leave empty to refuse them all |
| `IC_LLM_PROVIDER`, `IC_LLM_MODEL`, `IC_LLM_API_KEY` | optional (see section 9). The public instance uses `anthropic` and `claude-haiku-5-5`. Without a key, the deterministic agent runs and verdicts are tagged DEGRADED |
| `IC_EMBEDDING_PROVIDER` | `hashing`: seeding makes no embedding calls and the seed hash stays the documented one |
| `IC_ADMIN_TOKEN` | `openssl rand -hex 32`: the bearer token for `/admin` and `/api/admin/*`. Unset, the admin API is off |
| `IC_GITHUB_TOKEN`, `IC_GITHUB_REPO` | optional (see section 10). Without them, PRs and statuses are simulated |
| `IC_DISCORD_WEBHOOK_URL` | optional. Without it, pages are simulated |
| Limits | the defaults are the public limits (KTD10): `IC_ACTIONS_PER_HOUR=20`, per-IP/session run caps, `IC_PROOF_VERIFIES_PER_IP_HOUR=12`. `IC_DAILY_USD_BREAKER` is the daily LLM spend cap; keep it above 0, because the breaker trips when spend reaches the cap |

`docker-compose.yml` overrides the container-specific values itself: `IC_FALKORDB_URL` (the
`ic` user), `IC_STORE_PATH=/app/data/ic.sqlite3`, `IC_WEB_ORIGIN=https://${SITE_ADDRESS}`
(set `IC_WEB_ORIGIN_PROD` to change it) and `IC_COOKIE_SECURE=true`. The web image is built
with an empty `NEXT_PUBLIC_API_URL`, which makes every API call same-origin; set
`NEXT_PUBLIC_API_URL_PROD` only if the API lives on another host.

`falkordb-prod` refuses to start when either FalkorDB password is shorter than 24 characters.

## 4. Start

```bash
docker compose --profile prod up -d --build    # the first build takes about 10 minutes on 2 vCPU
docker compose --profile prod ps     # falkordb-prod, api and web become "healthy"
docker compose --profile prod logs -f caddy   # wait for "certificate obtained successfully"
curl -s https://commander.<domain>/health  # {"status":"ok","falkordb":true}
```

Every service has `restart: unless-stopped`, so the stack comes back after a reboot (Docker
itself starts on boot).

## 5. Seed the world

```bash
docker compose --profile prod exec api ic seed --seed 42
```

The seed builds into `seed:world:building` and then `GRAPH.COPY`s it onto `seed:world`, so
re-seeding never leaves a half-built world. It prints a content hash, which must match the one
in the README (`879505a4…`). With `IC_LLM_API_KEY` set, postmortem embeddings come from the
API, so seed after setting the key. The world persists in the `falkordb-prod-data` volume
(AOF). Check that it survives a restart:

```bash
docker compose --profile prod restart falkordb-prod api
docker compose --profile prod exec falkordb-prod sh -c \
  'redis-cli --no-auth-warning --user ic --pass "$FALKORDB_IC_PASSWORD" GRAPH.LIST'   # lists seed:world
```

## 6. Check a live run

```bash
O=https://commander.<domain>
curl -s -c jar -b jar -H 'Content-Type: application/json' -H "Origin: $O" \
  -X POST $O/api/inject -d '{"fault_class":"bad_deploy"}'      # returns events_url
curl -sN -b jar -H "Origin: $O" "$O/api/runs/<run_id>/events"  # streams to RESOLVED, then LEARNED
```

In a browser, open the site, press **Inject fault**, and confirm that the run streams with no
console errors. In DevTools, the `ic_sid` cookie is host-only, Secure and HttpOnly.

## 7. Continuous deployment

`.github/workflows/deploy.yml` runs after the CI workflow succeeds on a push to `main`. It
SSHes into the host with a dedicated key, fast-forwards the checkout to the tested commit,
runs `docker compose --profile prod up -d --build --remove-orphans`, prunes dangling images
and old build cache, and then polls `/health` and `/` until both answer. A `deploy-prod`
concurrency group keeps deploys from overlapping: a deploy that is running always finishes,
and when several pushes land back to back only the newest waiting deploy runs (it includes
the earlier commits).

Repository secrets:

| Secret | Value |
|---|---|
| `DEPLOY_HOST` | the host address |
| `DEPLOY_USER` | the SSH user (`ubuntu`) |
| `DEPLOY_SSH_KEY` | a private key used only by this workflow; its public key is in the user's `authorized_keys` |
| `DEPLOY_KNOWN_HOSTS` | `ssh-keyscan <host>` output, checked against the fingerprints from the first SSH login |

To rotate the key, generate a new pair, replace the line in `authorized_keys`, and run
`gh secret set DEPLOY_SSH_KEY < newkey`.

## 8. Firewall: only 22, 80, 443

Use both layers:

- **Cloud firewall** (EC2 security group, or the provider's equivalent): allow inbound TCP 22
  (ideally from your IP only), TCP 80, TCP 443 and UDP 443 (HTTP/3). Drop everything else.
  Docker's iptables rules bypass `ufw` for published ports, so the cloud firewall is the one
  that counts. If deploys run from GitHub Actions, port 22 has to accept GitHub's runner
  addresses as well.
- **Host**: allow SSH before enabling ufw so the session is not cut off:
  `sudo ufw allow 22/tcp && sudo ufw allow 80/tcp && sudo ufw allow 443 && sudo ufw default deny incoming && sudo ufw enable`.

Compose publishes only Caddy's 80/443. `falkordb-prod`, `api` and `web` have no `ports:`.
Check from another machine:

```bash
nmap -Pn -p- --min-rate 2000 commander.<domain>   # expect 22, 80, 443 only
redis-cli -h commander.<domain> -p 6379 ping      # must time out or be refused
curl -sI https://commander.<domain>/health | grep -i strict-transport-security
```

## 9. OpenAI budget (if `IC_LLM_API_KEY` is set)

- Create a dedicated OpenAI **project** for the demo and a project-scoped key.
- Set a monthly **budget** with a hard limit (for example $20) and an email alert at 50%. This
  is the backstop. The in-app breaker (`IC_DAILY_USD_BREAKER`, from logged token usage) trips
  first and serves recorded runs.
- Limit the project to the models in use (`gpt-4.1-mini`, `text-embedding-3-small`).

## 10. GitHub PAT scope (if real PRs)

- Use a **fine-grained** PAT on a dedicated sandbox repo, with only that repository selected.
- Grant these permissions: **Contents: read/write**, **Pull requests: read/write**, **Commit
  statuses: read/write**. Grant nothing else (no admin, no workflows).
- Set the expiration to the day after judging ends.
- The sandbox repo holds `services/{service}.yaml` manifests. PRs open on `ic/rollback-*`
  branches. Their `incident-commander/proofs` status links to `https://commander.<domain>/proof/<ref>`.
- Auto-close: every 10 minutes the API closes `ic/rollback-*` PRs older than one hour on that
  repo (fork branches are never touched) and deletes their branches and `pr:` graphs.
  `IC_ACTIONS_PER_HOUR` caps real PRs, pages and status re-runs. Over the cap they are
  simulated.

## 11. Uptime monitor

- Add an HTTP(S) monitor on `https://commander.<domain>/health` that checks for the keyword
  `"falkordb":true` every 1 minute (UptimeRobot, Better Stack or Hetzner's own). Alert by
  email or phone.
- Add a second monitor on `https://commander.<domain>/`.
- Optional: an alert on FalkorDB memory. `docker compose exec falkordb-prod redis-cli --user
  admin --askpass INFO memory` shows `used_memory_human` against `maxmemory`. With
  `noeviction`, a full instance fails writes. New sessions then error and visitors fall back
  to recorded runs.

## 12. Operations

- **Logs**: `docker compose --profile prod logs -f api` (a cleanup pass, PR auto-close and any
  GitHub errors are logged there).
- **Update**: pushes to `main` deploy automatically (section 7). By hand:
  `git pull --ff-only && docker compose --profile prod up -d --build`. Sessions are
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
5. **Deploy access**: remove the deploy key from `authorized_keys` and the repo's deploy keys,
   and delete the `DEPLOY_*` secrets.
6. To take the demo down: `docker compose --profile prod down` (add `-v` to delete the
   volumes) and delete the DNS record.

## Verification

On the public instance:

- `https://commander.niheshr.com` serves the web app with a Let's Encrypt certificate, and
  `/health` reports `"falkordb":true`. `ic seed --seed 42` prints the documented content hash.
- An injected `bad_deploy` run streams over SSE through Caddy to RESOLVED and LEARNED, both
  from `curl` and from the browser console (no console or CSP errors). The `ic_sid` cookie is
  host-only, Secure, HttpOnly and SameSite=Lax.
- `https://api.commander.niheshr.com/health` answers from the API alone, with its own
  certificate.
- From outside, only 22, 80 and 443 accept connections; 6379, 3000 and 8000 do not. API
  responses carry `Content-Security-Policy: default-src 'none'`; web pages carry their own
  `default-src 'self'` policy.

Before the public deploy:


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
