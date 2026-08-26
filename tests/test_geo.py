from aviapars.domain.geo import haversine_km, detour_km, EARTH_RADIUS_KM
import math
import pytest


def test_haversine_km_zero_distance():
    point = (51.5074, -0.1278)
    assert haversine_km(point, point) == 0.0

def test_haversine_km_antipodes():
    north_pole = (90.0, 0.0)
    south_pole = (-90.0, 0.0)
    expected_dist = math.pi * EARTH_RADIUS_KM
    assert haversine_km(north_pole, south_pole) == pytest.approx(expected_dist)

def test_haversine_km_quarter_circumference():
    equator_0 = (0.0, 0.0)
    equator_90 = (0.0, 90.0)
    expected_dist = (math.pi * EARTH_RADIUS_KM) / 2
    assert haversine_km(equator_0, equator_90) == pytest.approx(expected_dist)

def test_haversine_km_real_world():
    # London, UK
    london = (51.5074, -0.1278)
    # New York City, USA
    nyc = (40.7128, -74.0060)
    # Expected distance is around 5570 km
    assert haversine_km(london, nyc) == pytest.approx(5570.22, abs=0.1)


def test_detour_km_no_detour():
    origin = (51.5074, -0.1278)
    dest = (40.7128, -74.0060)
    # If via is the origin or destination, there is no detour
    assert detour_km(origin, origin, dest) == pytest.approx(0.0)
    assert detour_km(origin, dest, dest) == pytest.approx(0.0)

def test_detour_km_real_world():
    # London
    london = (51.5074, -0.1278)
    # New York
    nyc = (40.7128, -74.0060)
    # Tokyo
    tokyo = (35.6762, 139.6503)

    # Distance London -> Tokyo -> NYC - (London -> NYC)
    # This should be quite large
    london_to_tokyo = haversine_km(london, tokyo)
    tokyo_to_nyc = haversine_km(tokyo, nyc)
    london_to_nyc = haversine_km(london, nyc)

    expected_detour = london_to_tokyo + tokyo_to_nyc - london_to_nyc
    assert detour_km(london, tokyo, nyc) == pytest.approx(expected_detour)

def test_detour_km_straight_line():
    # Points on the equator
    origin = (0.0, 0.0)
    via = (0.0, 10.0)
    dest = (0.0, 20.0)

    # Going from 0 to 20 via 10 is perfectly on the great circle (equator)
    # so detour should be virtually 0
    assert detour_km(origin, via, dest) == pytest.approx(0.0, abs=1e-9)
