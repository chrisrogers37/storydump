### Tests

- **The media validator's cancellation tests fail at once off Linux instead of waiting forever.** Three tests wait for the validator to create its process, which it refuses to do anywhere but Linux, so on macOS a local run of the suite stopped there and never finished. They now fail straight away with the message the other Linux-only gates give, and run unchanged on Linux.
