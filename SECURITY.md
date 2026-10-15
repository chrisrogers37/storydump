# Security policy

Storydump is a hosted, multi-tenant service: one deployment holds many
workspaces' Instagram, Telegram and Google Drive connections, and keeping each
tenant's data to itself is a product requirement.

## Reporting a vulnerability

Report it privately through GitHub: **Security › Report a vulnerability** on
this repository. Please do not open a public issue, pull request or discussion
for a vulnerability; everything else here is public.

Include what you found, how to reproduce it and what it exposes. The answer
comes in the advisory thread, and a fix is coordinated there before anything
is published.

## Scope

This repository's code and the hosted service at `storydump.app` and
`api.storydump.app`: above all, anything that lets one workspace read or change
another's data, plus sign-in and API tokens, the Telegram webhook, and the
handling of Instagram and Google credentials.

Test only with a workspace and accounts you own. Do not access another
tenant's data, degrade the service, or post to an account that is not yours.

## Supported versions

Only the deployed `main` branch. There are no released versions to patch.
