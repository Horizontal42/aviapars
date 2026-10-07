from __future__ import annotations

import pytest

from aviapars.domain.geo import detour_km, haversine_km

# Coordinates for testing
LONDON = (51.5074, -0.1278)
PARIS = (48.8566, 2.3522)
BERLIN = (52.5200, 13.4050)
NEW_YORK = (40.7128, -74.0060)
TOKYO = (35.6762, 139.6503)

def test_haversine_km() -> None:
    # Distance to self is 0
    assert haversine_km(LONDON, LONDON) == pytest.approx(0.0)

    # Distance is symmetric
    assert haversine_km(LONDON, PARIS) == pytest.approx(haversine_km(PARIS, LONDON))

    # Known approximate distances
    # London to Paris is ~344 km
    assert 340 < haversine_km(LONDON, PARIS) < 350

    # London to New York is ~5570 km
    assert 5500 < haversine_km(LONDON, NEW_YORK) < 5600


def test_detour_km() -> None:
    # Detour via same point is 0
    assert detour_km(LONDON, LONDON, LONDON) == pytest.approx(0.0)

    # Detour via origin or destination is 0
    assert detour_km(LONDON, LONDON, PARIS) == pytest.approx(0.0)
    assert detour_km(LONDON, PARIS, PARIS) == pytest.approx(0.0)

    # Triangle inequality: origin -> via -> dest is always >= origin -> dest
    # Hence detour >= 0
    assert detour_km(LONDON, BERLIN, PARIS) >= 0.0

    # Check specific detour calculation
    # London -> Berlin -> Paris vs London -> Paris
    direct_dist = haversine_km(LONDON, PARIS)
    via_berlin_dist = haversine_km(LONDON, BERLIN) + haversine_km(BERLIN, PARIS)
    expected_detour = via_berlin_dist - direct_dist

    assert detour_km(LONDON, BERLIN, PARIS) == pytest.approx(expected_detour)

    # Detour via point on the great circle path (equator)
    # Origin: (0, 0), Dest: (0, 20), Via: (0, 10)
    p_origin = (0.0, 0.0)
    p_dest = (0.0, 20.0)
    p_via = (0.0, 10.0)
    # Should be exactly 0 (or very close)
    assert detour_km(p_origin, p_via, p_dest) == pytest.approx(0.0, abs=1e-9)

    # Detour via opposite direction
    p_opposite = (0.0, -10.0)
    # Distance from 0 to -10 is same as 0 to 10. Total path 0->-10->20 = 10 + 30 = 40 degrees.
    # Direct path 0->20 = 20 degrees. Detour should be 20 degrees worth of distance.
    expected_opposite_detour = haversine_km(p_origin, p_opposite) + haversine_km(p_opposite, p_dest) - haversine_km(p_origin, p_dest)
    assert detour_km(p_origin, p_opposite, p_dest) == pytest.approx(expected_opposite_detour)
    assert detour_km(p_origin, p_opposite, p_dest) > 0
