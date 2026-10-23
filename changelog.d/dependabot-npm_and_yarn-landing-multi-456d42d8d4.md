### Security

- **The web app's development dependencies take vitest 4.1.12.** It was 4.1.11, whose `@vitest/mocker` GHSA-qxg6-89mg-rqv8 and GHSA-xp26-3454-mjgp affect: a manual mock could be registered without authentication and inject code, and on Windows a backslash path got past the mocker's redirect allowlist. The lockfile also moves vite, rolldown, postcss and nanoid up a patch release. Vitest runs only in the web app's tests and is not part of the deployed site.
