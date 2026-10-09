### Changed

- **The binding writer binds a chat only when its caller names that chat's type (#1656).** `bindings.bind` takes `accepts`, the Telegram chat types its caller means to bind: a group's by default. Any other type the mapping knows, a private chat included, answers `chat_type_not_accepted` before the database sees anything. The group bind door, its one caller, names nothing and still answers the person itself first, so nothing a person can do changes. The rule that a private-chat binding is written only on purpose now belongs to the writer, where it used to rest on its caller. No migration.

### Tests

- **The private-chat card rule's remaining cases are pinned (#1656).** A database case holds the review resolutions' door, `restate_everywhere_touched`, to the rule: the outcome is written onto the card in every live binding, and the edit that shows it is queued only where a card may go. The outcome doors' case gains a member's private chat as a second positive control, beside the group. The tap gate now counts every statement that reads `user_identities` except the supersede, so a read added anywhere else on the tap is counted too. Two docstrings in `prompts.py` name the bindings a card goes to.
