### Changed

- **A past post's file is held only to what the transit store takes.** The source's byte cap is the transit store's limit (`PUBLISH_MAX_BYTES`: 8 MiB for an image, 40 MB for a video); Meta's own size limits are judged on the story derivation it fetches. An image between 8,000,000 bytes and 8 MiB, refused for its size before, is now judged; one already refused for it stays refused until its asset is observed again.

### Fixed

- **A past-posts video no longer stays pending for a reason only its file could settle (#52).** A container the walker cannot read whole (a fragmented movie, more boxes than it reads, a reference of a kind it does not know) is judged on what the probe reads, and its evidence records where the walk stopped and what it read. The probe opens up to 16 streams (`ValidationLimits.streams`, up from 3, and it may only be tightened); a file with more is not probed, since its derivation takes the first video and audio stream, and its evidence says so. A video's duration is the longest the container or any of its streams states, so a 59 s video track with 62 s of audio is a 62 s video, and a duration nothing states is not judged, as a frame rate the probe does not state.
