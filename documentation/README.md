# Storydump Documentation

Guides for using, developing and operating Storydump.

## Guides

- [Quick Start](guides/quickstart.md) — where to begin, by role: use, develop or operate
- [Development environment setup](guides/dev-environment-setup.md) — local setup: virtualenv, database, environment files
- [Testing guide](guides/testing-guide.md) — how the suite is laid out, what needs a PostgreSQL, how to run it
- [Test coverage](guides/TEST_COVERAGE.md) — what the suite covers and how coverage is measured
- [CI/CD pipeline](guides/ci-cd-pipeline.md) — the GitHub Actions jobs and the Railway auto-deploy
- [Cloud deployment](guides/cloud-deployment.md) — the hosted deployment: Railway, Neon, variables and providers
- [Deployment checklist](guides/deployment.md) — the step-by-step checklist for standing up an environment
- [Deployment options](guides/deployment-options.md) — how deploys reach production, and the multitenancy model
- [Instagram Login setup](guides/instagram-login-setup.md) — the Meta app, redirect URI and variables for connecting accounts
- [Landing site deployment](guides/landing-vercel-deployment.md) — deploying the Next.js site to Vercel
- [Design system](guides/design-system.md) — the tokens, fonts and component rules the site and the app share

## Operations

- [Instagram archive](operations/instagram-archive-review.md) — testing it locally, the prerequisites, activation, Meta permissions, the key ring and its limits
- [Instagram source gate, run in isolation](operations/instagram-source-isolated-gate.md) — the contributor recipe for the full-composition gate
- [Meta callback endpoints](operations/meta-callback-endpoints.md) — the deauthorize and data-deletion callbacks
