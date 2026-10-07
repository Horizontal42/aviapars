from aviapars.domain.geo import haversine_km, detour_km

def test_haversine_km_same_point():
    point = (51.5072, -0.1276)
    assert haversine_km(point, point) == 0.0

def test_haversine_km_known_distance():
    london = (51.5072, -0.1276)
    paris = (48.8566, 2.3522)
    # The actual haversine distance is around 343 km
    assert abs(haversine_km(london, paris) - 343.5) < 1.0

def test_haversine_km_ny_la():
    ny = (40.7128, -74.0060)
    la = (34.0522, -118.2437)
    # Expected distance around 3935 km
    assert abs(haversine_km(ny, la) - 3935.7) < 1.0

def test_haversine_km_antipodes():
    p1 = (90.0, 0.0) # North pole
    p2 = (-90.0, 0.0) # South pole
    # Expected distance is exactly pi * R
    import math
    from aviapars.domain.geo import EARTH_RADIUS_KM
    expected = math.pi * EARTH_RADIUS_KM
    assert abs(haversine_km(p1, p2) - expected) < 1e-5

def test_detour_km_no_detour():
    london = (51.5072, -0.1276)
    paris = (48.8566, 2.3522)
    assert detour_km(london, london, paris) == 0.0
    assert detour_km(london, paris, paris) == 0.0

def test_detour_km_detour():
    london = (51.5072, -0.1276)
    paris = (48.8566, 2.3522)
    ny = (40.7128, -74.0060)
    # Detour London -> NY -> Paris
    detour = detour_km(london, ny, paris)
    assert detour > 0

    # Detour should be exact diff
    expected_detour = haversine_km(london, ny) + haversine_km(ny, paris) - haversine_km(london, paris)
    assert abs(detour - expected_detour) < 1e-9
