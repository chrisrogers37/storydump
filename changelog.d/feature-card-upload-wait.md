### Fixed

- **A chat card's photo or video is no longer sent two to four times when Telegram is slow to answer its upload.** A card's upload waited 60 s for Telegram's answer; an answer later than that read as lost, and the outbox sent the card again, up to three more times, each extra copy without its buttons. The upload now has its own wait of 2 minutes (the `card_upload` timeout class), so an answer that is slow but comes is confirmed instead of sent again. The 60 s wait of the other uploads and downloads (the transit store, Drive, Instagram) is unchanged, and a slow card holds back only its own chat's next message.

### Added

- **A failed attempt of a call that creates something now logs how long it waited and how it failed.** For a card upload, a message, a transit upload or any other call that is never sent twice, the worker logs each attempt that fails at the transport: the provider's host, the seconds it waited and the transport's exception (a read or write timeout, a connection that failed), and whether the request may have reached the provider. A slow answer can then be told from a lost one. The path is never logged.
