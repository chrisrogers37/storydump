# Deployment Options

**Repository Status**: PUBLIC

---

## Current Setup: Railway Auto-Deploy

### How It Works

Railway automatically deploys when changes are pushed to `main`:

1. Push to `main` (or merge a PR)
2. Railway detects the change via GitHub integration
3. Railway builds both services (worker + web)
4. Railway restarts services with new code
5. Health checks verify the deployment

### Pros
- **Automated** -- deploy on every push to main
- **Safe for public repos** -- no self-hosted runners
- **Simple** -- no SSH keys, VPNs, or tunneling
- **Reliable** -- Railway manages restarts and health checks
- **Fast** -- builds typically complete in 1-2 minutes

### Cons
- Monthly cost (~$5-10/month for Railway)
- Dependent on Railway infrastructure

---

## CI/CD Pipeline

### Continuous Integration (Automated)

GitHub Actions runs automatically on every push/PR:
- **Linting** (ruff) -- code style checks
- **Tests** (pytest) -- unit and integration tests
- **Security** (pip-audit, bandit) -- vulnerability scanning

All CI runs on **GitHub cloud runners** (`ubuntu-latest`) -- safe for public repos.

### Continuous Deployment (Automated via Railway)

Railway deploys automatically when CI passes and changes land on `main`. No manual deployment step required.

### Manual Deployment (if needed)

```bash
# Force a redeploy via Railway CLI
railway up --service worker
railway up --service web

# Or trigger via Railway dashboard
```

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
| **View web logs** | `railway logs --service web` |
| **Force redeploy** | `railway up` or push to `main` |
| **Run health check** | `railway shell --service worker -c "storydump-cli check-health"` |
| **Run tests locally** | `pytest tests/ -v` |
| **View CI status** | GitHub Actions tab in repo |

---

## Summary

- **CI runs automatically** on GitHub cloud runners (`ubuntu-latest`) -- safe for public repos
- **CD runs automatically** via Railway GitHub integration on push to `main`
- **No manual deployment required** -- merge to main and Railway handles the rest
- **No self-hosted runners** -- all CI/CD uses managed cloud infrastructure
