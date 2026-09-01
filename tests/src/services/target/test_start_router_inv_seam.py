"""The `inv-` seam of the shared `/start` door, asserted from lane C's side.

**Why this test is HERE and not in `test_start_router.py`.** `07` §2 D33/D35
puts `inv-` and `link-` through one door, and the contract that came out of
that put the router in lane A and the `inv-` prefix in lane C. The safety
property — *an unrouted payload never dispatches* — is pinned by **the party a
leak would harm**, which is the lane whose handler would receive a payload that
is not its own. That is this lane, so this is its assertion, written against
the shipped router rather than copied from its suite.

**What would go wrong without it.** An invitation token is a bearer credential:
the handler registered under `inv-` consumes one. A router that fell back to
"nearest handler" on an unrecognised prefix, or matched case-insensitively, or
treated a bare `/start` as routable, would hand this lane a payload it would
then try to consume as a token. Nothing routes yet, so this is not live — which
is exactly when the pin is cheap.

**The real `inv-` handler does not exist yet** (it lands with the invitation
outbox producer). The spy below stands in the place it will occupy and
registers under the same prefix, so the property is pinned against the seam
rather than against an implementation.
"""

from __future__ import annotations

import pytest

from src.services.target.start_router import (
    GREETED,
    UNROUTED,
    StartResult,
    StartRouter,
)

INV = "inv-"
LINK = "link-"


class Spy:
    """Records every dispatch it receives. A call is the failure."""

    def __init__(self, name: str):
        self.name = name
        self.calls: list[str] = []

    async def __call__(self, conn, ctx) -> StartResult:
        self.calls.append(ctx.payload)
        return StartResult(outcome=f"{self.name}-handled", handled=True)


def _update(text: str, *, uid: int = 4242, cid: int = -100777, ctype: str = "private"):
    return {
        "message": {
            "text": text,
            "from": {"id": uid, "username": "tapper"},
            "chat": {"id": cid, "type": ctype},
        }
    }


@pytest.fixture()
def seam():
    """The router as this lane will meet it: `inv-` ours, `link-` someone
    else's. Both registered, because the leak that matters most is the one
    where a sibling lane's payload reaches ours."""
    router = StartRouter()
    inv, link = Spy("inv"), Spy("link")
    router.register(INV, inv)
    router.register(LINK, link)
    return router, inv, link


class TestTheInvSeam:
    async def test_a_real_inv_payload_reaches_us_with_the_prefix_stripped(self, seam):
        """**Positive control, and it must come first.** Every assertion below
        is "the handler was not called". A router that dispatched NOTHING would
        satisfy all of them, so the suite would pass while proving the opposite
        of what it claims."""
        router, inv, _ = seam
        result = await router.dispatch(None, _update(f"/start {INV}TOKEN123"))
        assert result.handled is True
        assert inv.calls == ["TOKEN123"], "prefix stripped, payload intact"

    @pytest.mark.parametrize(
        "payload",
        [
            "inv",  # the prefix without its separator
            "in",  # a proper prefix of the prefix
            "INV-TOKEN",  # case: Telegram payloads are case-sensitive
            "Inv-TOKEN",
            "xinv-TOKEN",  # our prefix, not at the start
            "-inv-TOKEN",
            "invite-TOKEN",  # a longer word that starts the same way
            "nonsense",
            "0",
        ],
    )
    async def test_an_unrouted_payload_reaches_nobody(self, seam, payload):
        """The contract's whole sentence: unrouted NEVER dispatches. Not to the
        nearest prefix, not to a default, not to us."""
        router, inv, link = seam
        result = await router.dispatch(None, _update(f"/start {payload}"))
        assert result.outcome == UNROUTED
        assert result.handled is False
        assert inv.calls == [], f"{payload!r} must not reach the inv handler"
        assert link.calls == [], f"{payload!r} must not reach any handler"

    async def test_an_unrouted_refusal_carries_no_reply_text(self, seam):
        """A refusal that said anything specific would make the shared door an
        oracle for which tokens exist. The router owns refusal copy; this pins
        that an unrouted result carries none of ours."""
        router, _, _ = seam
        result = await router.dispatch(None, _update("/start nonsense"))
        assert result.reply is None

    async def test_a_bare_start_greets_rather_than_routing_to_us(self, seam):
        """`payload_of` returns "" for a bare `/start`, which is a different
        fact from "no payload matched". It must not fall through to a prefix."""
        router, inv, link = seam
        result = await router.dispatch(None, _update("/start"))
        assert result.outcome == GREETED
        assert inv.calls == [] and link.calls == []

    async def test_a_sibling_lanes_payload_goes_to_the_sibling_not_to_us(self, seam):
        """The cross-purpose case D33/D35's disjoint tables exist to prevent: a
        `link-` payload consumed as an invitation token would be this lane
        spending someone else's credential."""
        router, inv, link = seam
        await router.dispatch(None, _update(f"/start {LINK}STATE99"))
        assert link.calls == ["STATE99"]
        assert inv.calls == []

    async def test_a_non_start_message_reaches_nobody(self, seam):
        router, inv, link = seam
        for text in ("hello", "/help", "/startle now", ""):
            result = await router.dispatch(None, _update(text))
            assert result.handled is False, text
        assert inv.calls == [] and link.calls == []

    async def test_an_unattributable_start_reaches_nobody(self, seam):
        """No sender means nothing to bind an invitation to. It must refuse
        rather than dispatch with a hole where the identity goes."""
        router, inv, link = seam
        update = _update(f"/start {INV}TOKEN")
        del update["message"]["from"]
        result = await router.dispatch(None, update)
        assert result.handled is False
        assert inv.calls == []


class TestTheSeamCannotBeReopenedByRegistration:
    def test_a_prefix_that_shadows_ours_is_refused_at_registration(self):
        """Ambiguity is refused when the door is built, not resolved when a
        delivery arrives. Pinned from this side because the payload that would
        be mis-routed is ours."""
        router = StartRouter()
        router.register(INV, Spy("inv"))
        for clashing in ("inv-", "inv", "i", "inv-x"):
            with pytest.raises(ValueError):
                router.register(clashing, Spy("other"))
