### Fixed

- **An expired, used or withdrawn invitation link read "This one is on us" (#1603).** The accept route answers such a link with `not_acceptable`, which the join page did not handle; it kept sentences for five codes nothing sends. `not_acceptable` now says the invitation has expired, has already been used, or was withdrawn, and to ask the person who invited you for a new link. The page's sentences are one table keyed by the codes that are actually sent, and a test reads the API's `invitations.REASONS` so the table cannot name a code that is never sent.
