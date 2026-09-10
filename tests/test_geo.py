import math
import pytest

from aviapars.domain.geo import EARTH_RADIUS_KM, Point, detour_km, haversine_km


def test_haversine_km() -> None:
    # Identical points
    p1: Point = (0.0, 0.0)
    assert haversine_km(p1, p1) == 0.0

    # Points on the equator, 1 degree apart
    p2: Point = (0.0, 1.0)
    # Expected distance formula based on haversine implementation for 1 degree difference on equator
    expected_dist = 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(math.sin(math.radians(0.5))**2))
    assert math.isclose(haversine_km(p1, p2), expected_dist)

    # North pole to south pole
    np: Point = (90.0, 0.0)
    sp: Point = (-90.0, 0.0)
    expected_pole_dist = math.pi * EARTH_RADIUS_KM
    assert math.isclose(haversine_km(np, sp), expected_pole_dist)


def test_detour_km() -> None:
    # Identical origin, via, and destination
    p1: Point = (10.0, 20.0)
    assert detour_km(p1, p1, p1) == 0.0

    # Via point on the great-circle path (e.g., same meridian)
    origin: Point = (10.0, 0.0)
    via: Point = (20.0, 0.0)
    dest: Point = (30.0, 0.0)
    # The detour should be extremely close to 0
    assert detour_km(origin, via, dest) == pytest.approx(0.0, abs=1e-9)

    # Via point exactly at origin
    assert detour_km(origin, origin, dest) == pytest.approx(0.0, abs=1e-9)

    # Via point exactly at dest
    assert detour_km(origin, dest, dest) == pytest.approx(0.0, abs=1e-9)

    # Positive detour (deviating from direct path)
    # Origin: (0,0), Dest: (0, 20), Via: (10, 10)
    origin2: Point = (0.0, 0.0)
    dest2: Point = (0.0, 20.0)
    via2: Point = (10.0, 10.0)
    detour = detour_km(origin2, via2, dest2)
    assert detour > 0.0

    # Verify the logic of triangle inequality
    # detour = dist(orig, via) + dist(via, dest) - dist(orig, dest)
    dist1 = haversine_km(origin2, via2)
    dist2 = haversine_km(via2, dest2)
    dist_total = haversine_km(origin2, dest2)
    assert math.isclose(detour, dist1 + dist2 - dist_total)
