# Fetching a preview deployment headlessly

Preview deployments are behind Vercel's deployment protection. A plain request
returns `302 → vercel.com/sso-api`, so an agent cannot verify a frontend change
by looking at it — only by reading code and trusting CI. This note is how to get
real HTML instead.

## Current status: not yet possible for this project

**As of 2026-08-24 there is no working bypass for storydump previews.** Measured:
a storydump preview URL returns `302` both with and without a bypass credential,
in both the query-parameter and header forms. The same credential and the same
two forms were also tried against another project on this account and also did
not open it, so this is not a malformed request — the credential in the fleet
environment does not authorise this project.

Following the redirect returns `200`, and that `200` is Vercel's own
`Login – Vercel` page rather than the app. **Do not read the status code alone.**
Check the content — a successful fetch contains the app's markup; a failed one
contains a Vercel login page at `200`.

### What has to happen first, and who can do it

Protection Bypass for Automation is generated per project in the Vercel
dashboard, under **Project Settings → Deployment Protection → Protection Bypass
for Automation**. It cannot be created from a CI environment without a Vercel API
token, and the CLI on the fleet has no stored credentials — `vercel whoami`
starts an interactive device-authorisation flow, which a headless agent cannot
complete.

So this needs one action from someone with dashboard access:

1. Enable Protection Bypass for Automation on the **storydump** project.
2. Put the generated value in the fleet environment under the variable name
   `VERCEL_AUTOMATION_BYPASS_SECRET`, scoped so the bots can read it.

The value is a credential. It belongs in the fleet environment only — never in a
commit, a PR body, an issue, a doc, or a chat message. This file names the
variable and nothing else, deliberately.

## How to fetch, once it is enabled

Two forms; both take the same secret, and neither should ever print it.

**Header form** — preferred for scripts, because the credential never lands in a
URL, a log line, or a shell history entry:

```bash
curl -sS -H "x-vercel-protection-bypass: $VERCEL_AUTOMATION_BYPASS_SECRET" \
  "https://<deployment>.vercel.app/some/path"
```

**Query-parameter form** — sets a `_vercel_jwt` cookie so subsequent requests in
the same jar need no credential, which is what a browser automation session
wants:

```bash
curl -sS -c /tmp/jar.txt \
  "https://<deployment>.vercel.app/?x-vercel-protection-bypass=$VERCEL_AUTOMATION_BYPASS_SECRET&x-vercel-set-bypass-cookie=true"
curl -sS -b /tmp/jar.txt "https://<deployment>.vercel.app/some/path"
```

## Verifying it actually worked

Assert on content, not on the status code:

```bash
curl -sS -H "x-vercel-protection-bypass: $VERCEL_AUTOMATION_BYPASS_SECRET" \
  "https://<deployment>.vercel.app/" -o /tmp/page.html
grep -q "Login – Vercel" /tmp/page.html && echo "BLOCKED — this is Vercel's login page" || echo "OK"
```

A bypass that is not working returns `200` with a login page, which is
indistinguishable from success on status code alone.

## Finding the preview URL for a branch

The GitHub deployments API carries it, and does not need Vercel auth:

```bash
SHA=$(git rev-parse HEAD)
gh api "repos/<owner>/<repo>/deployments?sha=$SHA" --jq '.[].id' | while read -r id; do
  gh api "repos/<owner>/<repo>/deployments/$id/statuses" --jq '.[0].environment_url'
done
```

## The web sign-up surface on previews

`webSignupEnabled()` (`landing/src/lib/web-signup.ts`) is ON for preview
deployments and OFF everywhere else, production included. An explicit
`WEB_SIGNUP_ENABLED` wins in either direction. So a preview shows the sign-up
screens without anyone changing a dashboard setting, and production is unaffected.
