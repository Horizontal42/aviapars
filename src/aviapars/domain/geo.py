from __future__ import annotations

from math import asin, cos, radians, sin, sqrt

EARTH_RADIUS_KM = 6371.0

Point = tuple[float, float]  # (lat, lon), degrees


def haversine_km(a: Point, b: Point) -> float:
    lat_a, lon_a = a
    lat_b, lon_b = b
    phi_a, phi_b = radians(lat_a), radians(lat_b)
    d_phi = phi_b - phi_a
    d_lambda = radians(lon_b - lon_a)
    h = sin(d_phi / 2) ** 2 + cos(phi_a) * cos(phi_b) * sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(h))


def detour_km(origin: Point, via: Point, dest: Point) -> float:
    """Extra distance travelled by going origin→via→dest instead of straight.

    Always >= 0 (great-circle distance obeys the triangle inequality), so a hub
    that genuinely sits on the path scores ~0 and one far off to the side scores
    the full excursion — which is exactly the signal hub ranking was missing.
    """
    return haversine_km(origin, via) + haversine_km(via, dest) - haversine_km(origin, dest)
