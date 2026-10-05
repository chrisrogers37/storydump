### Added

- **`invite_member` returns the invitation's join link.** The web's command route (`POST /api/v1/workspaces/{ws}/commands/invite_member`) answers with `join_url`, the `{WEB_APP_URL}/join/{token}` link, and `expires_at`. `join_url` is null on a deployment with no `WEB_APP_URL`.

### Security

- **An invitation's token reaches the database only as its SHA-256.** The email arm is off (`invitations.EMAIL_DELIVERY_ENABLED`) until it can send without storing the token, so an email invitation reports `delivery.state` `withheld` and nothing is queued.
