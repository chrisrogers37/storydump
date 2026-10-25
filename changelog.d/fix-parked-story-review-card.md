### Fixed

- **A story parked for review before its Telegram card went out now gets its card (#38).** The sender renders a card again when it claims it, and it retired the card of a story in `review_required` unsent, so the bound chats heard only the text notice and had no buttons to resolve the story with. The claim now renders the review card for that state, as text with the review keyboard (It posted, Not there — post again, Give up, and Configure), the same card a story that already had one shows once it is parked.
