### Added

- **A scheduling monitor that pages when scheduling or the worker stops.** `scripts/scheduling_monitor.py` polls `/health/scheduling` from a separate host, classifies each reading with the same classifier `storydump health` uses (`storydump_cli/health_classify/scheduling.py`), and hands a message to a deployment-supplied notify command: a stalled cursor or a worker that has stopped finishing system jobs pages on the first reading, an unreachable endpoint after two, a fault repeats every six hours, and recovery is announced. It exits 0 when it has nothing to say, 10 when it spoke and 11 when it could not deliver, keeping the message to retry on the next poll. It needs nothing installed: it runs on the system Python from a checkout. `documentation/operations/scheduling-monitor.md` covers how it is run and checked.

### Fixed

- **A `/health/scheduling` reading without the worker block is unreachable, never healthy.** The route always serves the block, so a reading without it means a broken instrument; with a destination active, the classifier used to answer healthy on the cursor axis alone and drop the worker axis without a word. `storydump health` and the monitor now report it as unreachable.
