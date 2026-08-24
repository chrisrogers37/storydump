# Fetching a preview deployment headlessly

Preview deployments are behind Vercel's deployment protection. A plain request
returns `302 → vercel.com/sso-api`, so an agent cannot verify a frontend change
by looking at it — only by reading code and trusting CI. This note is how to get
real HTML instead.

## Status: working

**Verified 2026-08-24.** Protection Bypass for Automation is enabled on the
storydump project, and the header form below returns real application HTML.

Measured on a live preview: a plain request returns `302` with
`location: vercel.com/sso-api`; the same request carrying the bypass header
returns the app — `/login` at `200`, ~14 KB, `<title>Login — Storydump</title>`.

## When a correct command still returns 302

**A `302` while you are sending a bypass credential does NOT mean the bypass is
off. It usually means your secret is stale.**

This is worth stating plainly because it cost real time. Before the bypass was
enabled on this project, a `VERCEL_AUTOMATION_BYPASS_SECRET` was already present
in the fleet environment, left over from an earlier setup. It was stale. So a
correctly-formed command carrying a correct-looking credential returned `302` —
which is byte-for-byte what "no bypass exists at all" looks like. The two states
are indistinguishable from the response.

There is a second way to hold a stale value, and it bites even after the secret
is fixed: **a running process keeps the environment it started with.** Rotating
the value in the fleet `.env` does not reach a session that is already up. A
long-running agent will keep presenting the old secret, and keep getting `302`,
until it restarts or re-reads the file.

### Telling them apart without printing either value

Compare fingerprints, never values:

```bash
# what this process is actually sending
printenv VERCEL_AUTOMATION_BYPASS_SECRET | tr -d '\n' | sha256sum | cut -c1-12

# what the fleet environment currently holds
grep -E '^VERCEL_AUTOMATION_BYPASS_SECRET=' <fleet-env-file> | tail -1 \
  | sed 's/^[^=]*=//' | tr -d "\"'\n" | sha256sum | cut -c1-12
```

Different fingerprints mean the process is stale — restart it, or read the value
from the file for a one-off call. Matching fingerprints mean the secret in play
is the one on disk, and a `302` then points at the credential itself rather than
at your process.

`lib/env-tiers.sh` prints the tier files in the order the runtime reads them, if
you need to find which one holds the key.

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

Two failure shapes, and neither is caught by a status-code check alone.

**A blocked fetch can return `200`.** Following the SSO redirect lands on
Vercel's own `Login – Vercel` page, which is a perfectly good `200`.

**A working fetch can return `3xx`.** The app redirects unauthenticated requests
to `/login`, so a successful bypass often produces a redirect too. What
distinguishes them is *where it points*:

```bash
loc=$(curl -s -o /tmp/page.html -w '%{redirect_url}' \
  -H "x-vercel-protection-bypass: $VERCEL_AUTOMATION_BYPASS_SECRET" \
  "https://<deployment>.vercel.app/welcome")

case "$loc" in
  *sso-api*) echo "BLOCKED — Vercel protection, check the secret" ;;
  *)         grep -q "Login – Vercel" /tmp/page.html \
               && echo "BLOCKED — landed on Vercel's login page" \
               || echo "OK — app content or an app redirect to $loc" ;;
esac
```

Check the redirect target first, then the body. A content-only check reports OK
on a `302`, because a 15-byte redirect body contains no Vercel login markup.

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
