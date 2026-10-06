### Tests

- **The Members card's invite section is pinned to admins and owners (#1600).** A test calls the settings page as a member, an admin and the owner. A member is shown the card but neither the invite form nor the pending list, and the page never reads the invitations for them (#1571). An admin and the owner get both. Removing either the render gate or the read gate fails it.
