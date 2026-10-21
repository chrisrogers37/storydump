### Security

- **Pillow 13.0.0, which fixes eight vulnerabilities reported against 12.3.0.** Pillow decodes the JPEG previews of Instagram posts (`archive_preview`). `pip-audit` failed every CI run on 12.3.0, and 13.0.0 is the only fixed version.

### Changed

- **CI runs Python 3.12, the version production already runs.** Railway's Nixpacks build installs into Python 3.12, while CI tested on 3.10, and Pillow 13 needs 3.11 or later. Every CI job now uses 3.12, `setup.py` requires it, and ruff targets it.
