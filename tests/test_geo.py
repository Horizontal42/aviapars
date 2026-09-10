import pytest
from math import isclose

from aviapars.domain.geo import Point, haversine_km, detour_km

def test_haversine_km_same_point() -> None:
    a: Point = (51.5, -0.1)
    assert haversine_km(a, a) == 0.0

def test_haversine_km_known_distance() -> None:
    # Approximate distance between London and Paris is ~344 km
    london: Point = (51.5072, -0.1275)
    paris: Point = (48.8566, 2.3522)
    distance = haversine_km(london, paris)
    assert 340 < distance < 350

def test_haversine_km_equator() -> None:
    # 1 degree of longitude at the equator is roughly 111.32 km
    p1: Point = (0.0, 0.0)
    p2: Point = (0.0, 1.0)
    distance = haversine_km(p1, p2)
    assert isclose(distance, 111.19, rel_tol=1e-3)

def test_haversine_km_symmetry() -> None:
    p1: Point = (40.7128, -74.0060) # New York
    p2: Point = (34.0522, -118.2437) # Los Angeles
    assert haversine_km(p1, p2) == haversine_km(p2, p1)

def test_detour_km_no_detour_at_endpoints() -> None:
    origin: Point = (40.0, 40.0)
    dest: Point = (50.0, 50.0)

    assert isclose(detour_km(origin, origin, dest), 0.0, abs_tol=1e-9)
    assert isclose(detour_km(origin, dest, dest), 0.0, abs_tol=1e-9)

def test_detour_km_positive_detour() -> None:
    origin: Point = (0.0, 0.0)
    dest: Point = (0.0, 10.0)
    via: Point = (10.0, 5.0) # Significantly off the direct path

    detour = detour_km(origin, via, dest)
    assert detour > 0.0

    # Distance from (0,0) to (10,5) is roughly 1248 km
    # Distance from (10,5) to (0,10) is roughly 1248 km
    # Total distance is roughly 2496 km
    # Direct distance from (0,0) to (0,10) is roughly 1112 km
    # Detour should be roughly 1384 km
    assert detour > 1000.0
