### Fixed

- **Post now on a Telegram card approves a story whose media comes from Instagram past posts.** A tap ran its command on the webhook's database connection, which never carried the API's past-posts capability, so every past-posts story read as switched off and Post now answered "This post's media isn't ready to use" while Approve on the web worked. The webhook's dispatcher now reads the API's own capability as each delivery runs, carries it onto that delivery's connection alone, and puts back what the pooled connection held before, so a tap and a web click reach the same verdict. Skip, Reject, Posted myself, the review card and Configure behave as before.

### Changed

- **The tap's log line names why media was refused.** The API's `tap answered` line now ends with `source_reason=<code>`: the closed suitability reason of a `media_unsuitable` refusal (for example `validation_pending` or `source_off`), or `None`. The dispatcher's `tap` line ends the same way. What the tapper reads is unchanged.
- **The API says at startup whether a Telegram tap reads Instagram past posts.** One line on the API's own log, `telegram ingress: instagram past posts on` or `off`, shows a deploy's state before anyone taps.

### Tests

- **A Telegram tap is tested end to end on a past-posts story.** The Instagram source gate taps Post now on a delivered card through the real webhook and the story posts. The tap gate checks that a pooled connection carries no capability after a tap, and a unit test pins the dispatcher's key to the one each past-posts reader asks for.
