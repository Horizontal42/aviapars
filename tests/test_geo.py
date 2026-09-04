import math

import pytest

from aviapars.domain.geo import detour_km, haversine_km


def test_haversine_km_same_point():
    point = (55.7558, 37.6173)  # Moscow
    assert math.isclose(haversine_km(point, point), 0.0, abs_tol=1e-9)


def test_haversine_km_known_distance():
    # 1 degree of longitude at the equator is approximately 111.195 km
    # given Earth radius 6371.0 km, exactly: 2 * pi * 6371 / 360 = 111.1949266...
    point1 = (0.0, 0.0)
    point2 = (0.0, 1.0)
    expected_dist = 2 * math.pi * 6371.0 / 360.0

    assert math.isclose(haversine_km(point1, point2), expected_dist, rel_tol=1e-5)


def test_detour_km_no_detour():
    # Points on the equator
    origin = (0.0, 0.0)
    via = (0.0, 10.0)
    dest = (0.0, 20.0)

    # Distance O->V + V->D should be equal to O->D, so detour should be 0
    assert math.isclose(detour_km(origin, via, dest), 0.0, abs_tol=1e-9)


def test_detour_km_with_detour():
    # Origin and Dest on the equator
    origin = (0.0, 0.0)
    dest = (0.0, 20.0)

    # Via is further north
    via = (10.0, 10.0)

    detour = detour_km(origin, via, dest)

    # Detour should be strictly positive
    assert detour > 0

    # It should be exactly: dist(O, V) + dist(V, D) - dist(O, D)
    expected_detour = haversine_km(origin, via) + haversine_km(via, dest) - haversine_km(origin, dest)
    assert math.isclose(detour, expected_detour, abs_tol=1e-9)
