# Deployment Options

**Repository Status**: PUBLIC

---

## Current Setup: Railway Auto-Deploy

### How It Works

Railway automatically deploys when changes are pushed to `main`:

1. Push to `main` (or merge a PR)
2. Railway detects the change via GitHub integration
3. Railway builds both services (`worker` + `storydump`, the API) with
   `railway.toml`'s `buildCommand`
4. Each service's pre-deploy step applies pending migrations
   (`railway.toml`: `python -m scripts.migration_runner apply`); a failing
   migration aborts the deploy with the old version still serving
5. The new version starts and must answer `GET /health` within 30 s

`railway.toml` (build, pre-deploy, health check, restart and draining policy)
and the `Procfile` (the two process commands) are the whole deployment
configuration; `cloud-deployment.md` §2 walks through them.

### Pros
- **Automated** -- deploy on every push to main
- **Safe for public repos** -- no self-hosted runners
- **Simple** -- no SSH keys, VPNs, or tunneling
- **Reliable** -- Railway manages restarts and health checks

### Cons
- Monthly cost (Railway's usage-based plan)
- Dependent on Railway infrastructure

---

## CI/CD Pipeline

### Continuous Integration (Automated)

GitHub Actions runs automatically on every push/PR (`.github/workflows/ci.yml`):
- **Linting** (ruff) -- code style checks over the whole repository
- **FC-2 ratchet** (`scripts/telegram_ratchet.py`) -- the allowlist of modules
  that may reference Telegram
- **Tests** (pytest) -- unit and integration tests against a PostgreSQL 15
  service container
- **Security** -- vulnerability scanning: `pip-audit` gates (a known-vulnerable
  pin in `requirements.txt` fails the job); `bandit` is advisory, read its
  uploaded report rather than the step's colour
- **Front end** (`landing/`: vitest, `tsc --noEmit`, eslint)
- **Changelog** -- a PR must touch `CHANGELOG.md` unless it is docs-only

A second workflow, `schema-drift.yml`, runs daily at 06:00 UTC and compares a
live database's schema with the one the repository declares; it is an audit,
not a PR gate.

All CI runs on **GitHub cloud runners** (`ubuntu-latest`) -- safe for public repos.

### Continuous Deployment (Automated via Railway)

Railway deploys automatically when CI passes and changes land on `main`. No manual deployment step required.

### Manual Deployment (if needed)

```bash
# Re-run a service's latest deployment (nothing local is uploaded or rebuilt)
railway redeploy --service worker --yes
railway redeploy --service storydump --yes

# Or push to main (an empty commit will do) -- the normal path, CI included;
# or trigger via the Railway dashboard
```

Never `railway up`: it uploads and deploys the laptop's working tree --
uncommitted edits included -- bypassing GitHub and CI. And `redeploy` re-runs
whatever Railway holds as the service's latest deployment, which after a
`railway down` can be an old build (`operations/legacy-window-close.md`
step 8); when in doubt, push to `main` and confirm the commit with
`storydump deploys`.

---

## Why Not Self-Hosted Runners?

**Self-hosted runners on public repos are DANGEROUS**:
- Attackers can submit PRs with malicious code
- Workflows execute on YOUR infrastructure
- Your secrets, network, and data are exposed

See: [GitHub's security warning](https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners/about-self-hosted-runners#self-hosted-runner-security)

All CI runs on **GitHub cloud runners** (`ubuntu-latest`), which are safe for public repositories.

---

## Multitenancy Model

Storydump runs as **one deployment we operate**, serving many tenants on it.
Onboarding a tenant is a row, not a deploy: no per-tenant Railway project, Neon
database, environment variables, or bot token. Tenant isolation is enforced
inside that single deployment — per-workspace scoping in the database and in
every service boundary — which is what makes it safe for unrelated customers to
share it.

This replaces an earlier fork-and-run-your-own model. That model was never
operated, and it is not what the product is: see the consolidated design plan's
FC-9 (hosted product) and its T3 constraint, "workspaces are rows, not
deploys".

---

## Quick Reference

| Task | Command |
|------|---------|
| **Check deploy status** | Railway dashboard or `railway logs` |
| **View worker logs** | `railway logs --service worker` |
| **View web logs** | `railway logs --service storydump` |
| **Force redeploy** | `railway redeploy --service <svc> --yes`, or push to `main` |
| **Run health check** | `storydump health` |
| **Run tests locally** | `pytest tests/ -v` |
| **View CI status** | GitHub Actions tab in repo |

---

## Summary

- **CI runs automatically** on GitHub cloud runners (`ubuntu-latest`) -- safe for public repos
- **CD runs automatically** via Railway GitHub integration on push to `main`
- **No manual deployment required** -- merge to main and Railway handles the rest
- **No self-hosted runners** -- all CI/CD uses managed cloud infrastructure
