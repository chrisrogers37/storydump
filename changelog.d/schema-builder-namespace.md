### Fixed

- **Schema construction targets public regardless of the caller's search path.** Failed builds roll back their objects and restore the caller's path, installed packages include the required SQL files, and local setup configures separate API and worker logins.
