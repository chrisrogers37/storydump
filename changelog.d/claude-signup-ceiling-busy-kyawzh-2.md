### Changed

- **A busy waitlist refusal is its own analytics reason, and the closing form's error is readable (#1613).** When the API turns a signup away because a shared limit is full or no slot came free, the form's "Waitlist Error" event now records `reason: busy` instead of `server_error`, so the busy refusals can be counted apart from real failures. The event carries nothing new. The closing form's error sentence, on the orange band, now uses the band's ink at about 6:1 contrast instead of the alarm red at 2.5:1; the hero form keeps the red, which reads at 7.9:1 on white.
