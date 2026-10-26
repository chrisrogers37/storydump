### Fixed

- **A story in review says why it went into review, never an earlier attempt's reason (#60).** A readiness poll that stays pending, a lost answer to a container create, a spent job budget, the reconciler's park of a publish it could not confirm, and the reaper's park of a story that waited approved too long each record their own reason in the story's `last_error -> 'error' -> 'refusal'`, in place of whatever an earlier attempt left there, such as a dead container's status. The review card's line says that reason, opened by "Why:" since nothing refused the story; a refusal's line still opens with "Refused:".

### Security

- **The shared provider-text scrub covers more credential forms.** `refusal_evidence.scrub_text`, which every provider text a refusal or a publish failure records passes through, masks more of the ways a credential can be written.
