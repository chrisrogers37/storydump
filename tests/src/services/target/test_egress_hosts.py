"""FC-4 — Instagram API with Instagram Login; never a Facebook Page.

The egress floor's allow-list is the closed set of provider hosts the target
tier may reach. A Facebook Graph host on it was the legacy Facebook-Login
credential path's (#739); nothing in the target tier calls it — every
Instagram call goes to `graph.instagram.com` (`instagram_graph.GRAPH_BASE`) or
`api.instagram.com` — so its presence was a door left open, not a need."""

from __future__ import annotations

from src.services.target.egress import DEFAULT_ALLOWED_HOSTS
from src.services.target.instagram_graph import GRAPH_BASE


def test_no_facebook_host_is_allowed():
    facebook = sorted(h for h in DEFAULT_ALLOWED_HOSTS if "facebook" in h)
    assert facebook == [], f"FC-4: {facebook} on the egress allow-list"


def test_the_instagram_graph_host_is_allowed():
    host = GRAPH_BASE.split("://", 1)[1].split("/", 1)[0]
    assert host in DEFAULT_ALLOWED_HOSTS
