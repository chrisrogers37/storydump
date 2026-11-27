### Changed

- **The web app's typescript-eslint and ts-morph take minimatch 11.0.9.** typescript-eslint and the @typescript-eslint packages move from 8.56.1 to 8.73.0, and an `overrides` entry in `landing/package.json` gives @ts-morph/common, which ts-morph 26 pins at 0.27 for shadcn 3, minimatch `^11.0.1` in place of `^10.0.1`. Both were minimatch 10.2.4. The lockfile also moves ts-api-utils, semver, brace-expansion and ignore within their ranges, and adds @isaacs/nonrecursive-glob-match 1.1.0, a dependency of minimatch 11. All of these are development dependencies.
