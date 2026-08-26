import pytest

from aviapars.domain.geo import detour_km, haversine_km

def test_detour_km_direct_path():
    # Points on the same meridian (longitude 0)
    # Origin: equator (0, 0)
    # Dest: 10 degrees North (10, 0)
    # Via: 5 degrees North (5, 0) - directly on the path
    origin = (0.0, 0.0)
    via = (5.0, 0.0)
    dest = (10.0, 0.0)

    detour = detour_km(origin, via, dest)
    assert detour == pytest.approx(0.0, abs=1e-5)

def test_detour_km_same_points():
    point = (55.7558, 37.6173)  # Moscow

    # All three points are the same
    assert detour_km(point, point, point) == 0.0

def test_detour_km_backtracking():
    # Origin: equator (0, 0)
    # Via: 10 degrees North (10, 0)
    # Dest: equator (0, 0) - going back to origin
    origin = (0.0, 0.0)
    via = (10.0, 0.0)
    dest = (0.0, 0.0)

    expected_detour = 2 * haversine_km(origin, via)
    detour = detour_km(origin, via, dest)

    assert detour == pytest.approx(expected_detour, abs=1e-5)

def test_detour_km_orthogonal_path():
    # Origin: (0, 0)
    # Dest: (0, 10) - along the equator
    # Via: (10, 5) - forms a triangle
    origin = (0.0, 0.0)
    via = (10.0, 5.0)
    dest = (0.0, 10.0)

    detour = detour_km(origin, via, dest)

    # The sum of two sides of a non-degenerate triangle is strictly greater than the third
    assert detour > 0.0

    # Calculate expected value manually
    dist1 = haversine_km(origin, via)
    dist2 = haversine_km(via, dest)
    dist3 = haversine_km(origin, dest)
    expected_detour = dist1 + dist2 - dist3

    assert detour == pytest.approx(expected_detour, abs=1e-5)

def test_detour_km_triangle_inequality():
    # Any random triangle should satisfy triangle inequality
    origin = (55.7558, 37.6173)  # Moscow
    via = (59.9343, 30.3351)     # St. Petersburg
    dest = (56.8389, 60.6057)    # Yekaterinburg

    detour = detour_km(origin, via, dest)

    assert detour >= 0.0
