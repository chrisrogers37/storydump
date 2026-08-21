"""Channel adapters — the transports the composition root injects.

Outside `src/services/target/` on purpose: the tier carries zero Telegram
references (the FC-2 ratchet's measured fact) and receives channels only as
injected callables. Modules here follow the ratchet's adapter naming rule
(stem `telegram` / `telegram_*`), so their chat-ref functions are adapter-side
by declaration.
"""
