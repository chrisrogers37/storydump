### Fixed

- **A story parked for review whose item was removed can be resolved from its card (#48).** Its review card had no buttons, because a removed item's card carries none, so the story could be resolved only on the web. Its card now shows 🚫 Give up: the item cannot post again, and giving the story up resolves it. Once a publish call was made, ✅ It posted stays above it, as on the web, since the story may have posted before its item was removed. A tap on Configure on an older card of such a story brings that card to the same buttons.
