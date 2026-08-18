"""
Location matching against the configurable TARGET_LOCATIONS list.
"""
from __future__ import annotations


def location_matches(location_text: str, target_locations: list[str]) -> bool:
    """
    Case-insensitive substring match. Empty/unknown location text is treated
    as a non-match (not a rejection by itself — see cheap_filter.py for how
    this feeds into scoring rather than a hard reject).
    """
    if not location_text:
        return False
    location_lower = location_text.lower()
    return any(target.lower() in location_lower for target in target_locations)
