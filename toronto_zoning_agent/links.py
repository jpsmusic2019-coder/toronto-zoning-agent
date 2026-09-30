# Copyright © 2026 Joshua Seaton. All rights reserved. Evaluation only; see LICENSE.
"""
Shared geo-link helpers — no-API-key Google Maps + Street View URL builders.

Pure string builders from coordinates already resolved elsewhere (the Toronto
address-points index). No network, no key, no quota. Owned by neither agent:
the Zoning Agent uses these for its report and workbook, and other agents'
"Google Maps link/embed" enrichment backlog item is the same call.

    from toronto_zoning_agent.links import maps_link, street_view_link
    maps_link(43.6981, -79.4123)            -> a pin at the parcel
    street_view_link(43.6981, -79.4123)     -> nearest panorama
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import quote


def maps_link(lat: Optional[float], lon: Optional[float], label: Optional[str] = None) -> str:
    """Google Maps URL dropping a pin at (lat, lon).

    When coordinates are missing but a label (address) is given, fall back to a
    text query so the link still resolves. Empty string if nothing usable.
    """
    if lat is not None and lon is not None:
        return f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
    if label:
        return f"https://www.google.com/maps/search/?api=1&query={quote(label)}"
    return ""


def street_view_link(lat: Optional[float], lon: Optional[float]) -> str:
    """Google Street View URL opening the nearest panorama to (lat, lon)."""
    if lat is None or lon is None:
        return ""
    return (
        "https://www.google.com/maps/@?api=1&map_action=pano"
        f"&viewpoint={lat},{lon}"
    )
