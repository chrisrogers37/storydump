---
paths:
  - "src/**/*.py"
  - "storydump_cli/**/*.py"
---

# Development Patterns

## Service Layer (the target tier)

The legacy `BaseService`/repository layering (`src/services/base_service.py`,
`src/repositories/`, `track_execution`) was deleted in the legacy tear-out
(#1216). A target-tier service is a module under `src/services/target/` whose
functions take the unit of work (`src/services/target/unit_of_work.py`) and
write their SQL by hand under it; the models in `src/models/target/` exist for
schema parity, never as an ORM. Follow the module you are extending — the
executors (`command_executors.py`), the readers (`readers.py`, `ops_views.py`),
the lanes (`work_loop.py`) — and raise `StorydumpError` subclasses
(`src/exceptions/`) rather than logging and swallowing.

## Logging

```python
from src.utils.logger import logger

logger.info(f"Indexing media file: {file_path}")
logger.warning(f"Validation warnings: {warnings}")
logger.error(f"Failed: {error}", exc_info=True)
# Levels: DEBUG, INFO, WARNING, ERROR, CRITICAL
```

## Media

Instagram Story specs: 9:16 aspect ratio (ideal), 1080x1920 resolution, max 100MB, JPG/PNG/GIF.
The media type is decided by suffix in the target's Drive adapter (`src/services/target/google_drive_adapter.py`); the legacy `ImageProcessor` went with the legacy tier (#1216).

## Security Patterns

- Always `html.escape()` user-supplied values before interpolating into HTML
- Never `allow_origins=["*"]` — restrict to `OAUTH_REDIRECT_BASE_URL`
- Verify signed initData fields match request body fields
- Use Pydantic `Field(ge=, le=)` on all numeric API inputs
