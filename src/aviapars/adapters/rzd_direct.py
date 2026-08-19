from __future__ import annotations

import asyncio
import hashlib
import json
import ssl
from datetime import date, datetime, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp_socks import ProxyConnector

from aviapars.adapters.base import (
    MAX_EGRESS_ATTEMPTS_PER_CALL,
    EgressPool,
    HealthStatus,
    Leg,
    Mode,
    Offer,
    RateLimitPolicy,
    ResultStatus,
    SearchQuery,
    SearchResult,
    Tier,
    build_search_result,
    probe_dates_total_timeout_s,
)

BASE_URL = "https://pass.rzd.ru/timetable/public/ru"
TRAIN_LAYER_ID = 5827
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; aviapars/1.0)",
    "Referer": "https://pass.rzd.ru/timetable/public/ru",
}
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
_POLL_TRIES = 14
_POLL_INTERVAL_S = 1.5

# pass.rzd.ru's leaf cert chains through "Russian Trusted Sub CA" (2024 generation, AIA
# points at nuc-cdp.digital.gov.ru/cdp/subca_ssl_rsa2024.crt) up to "Russian Trusted Root
# CA" — Minsvyaz's national CA, not in the standard Mozilla/OS trust store this VDS uses.
# Live-verified (2026-08-19): every request was failing at the TLS handshake itself
# (`SSLCertVerificationError: unable to get local issuer certificate`, both via plain
# `curl` and aiohttp), landing as a generic UPSTREAM_ERROR — not the 403 network ban
# ARCHITECTURE.md documented earlier, and masking it: the egress pool can't route around
# a client-side trust-store gap, so every pool entry failed identically before ever
# reaching the point of testing the ban. The server itself sends leaf + a stray copy of
# the *root* cert, never the sub CA — so the client has to carry both explicitly, not
# just the root. If RZD rotates to a newer Sub CA generation, this needs a fresh cert
# fetched from the leaf's own AIA URL (`openssl x509 -in <leaf> -noout -text | grep -A3
# 'Authority Information'`), not a wildcard trust relaxation.
_RU_TRUSTED_CA_PEM = """\
-----BEGIN CERTIFICATE-----
MIIFwjCCA6qgAwIBAgICEAAwDQYJKoZIhvcNAQELBQAwcDELMAkGA1UEBhMCUlUx
PzA9BgNVBAoMNlRoZSBNaW5pc3RyeSBvZiBEaWdpdGFsIERldmVsb3BtZW50IGFu
ZCBDb21tdW5pY2F0aW9uczEgMB4GA1UEAwwXUnVzc2lhbiBUcnVzdGVkIFJvb3Qg
Q0EwHhcNMjIwMzAxMjEwNDE1WhcNMzIwMjI3MjEwNDE1WjBwMQswCQYDVQQGEwJS
VTE/MD0GA1UECgw2VGhlIE1pbmlzdHJ5IG9mIERpZ2l0YWwgRGV2ZWxvcG1lbnQg
YW5kIENvbW11bmljYXRpb25zMSAwHgYDVQQDDBdSdXNzaWFuIFRydXN0ZWQgUm9v
dCBDQTCCAiIwDQYJKoZIhvcNAQEBBQADggIPADCCAgoCggIBAMfFOZ8pUAL3+r2n
qqE0Zp52selXsKGFYoG0GM5bwz1bSFtCt+AZQMhkWQheI3poZAToYJu69pHLKS6Q
XBiwBC1cvzYmUYKMYZC7jE5YhEU2bSL0mX7NaMxMDmH2/NwuOVRj8OImVa5s1F4U
zn4Kv3PFlDBjjSjXKVY9kmjUBsXQrIHeaqmUIsPIlNWUnimXS0I0abExqkbdrXbX
YwCOXhOO2pDUx3ckmJlCMUGacUTnylyQW2VsJIyIGA8V0xzdaeUXg0VZ6ZmNUr5Y
Ber/EAOLPb8NYpsAhJe2mXjMB/J9HNsoFMBFJ0lLOT/+dQvjbdRZoOT8eqJpWnVD
U+QL/qEZnz57N88OWM3rabJkRNdU/Z7x5SFIM9FrqtN8xewsiBWBI0K6XFuOBOTD
4V08o4TzJ8+Ccq5XlCUW2L48pZNCYuBDfBh7FxkB7qDgGDiaftEkZZfApRg2E+M9
G8wkNKTPLDc4wH0FDTijhgxR3Y4PiS1HL2Zhw7bD3CbslmEGgfnnZojNkJtcLeBH
BLa52/dSwNU4WWLubaYSiAmA9IUMX1/RpfpxOxd4Ykmhz97oFbUaDJFipIggx5sX
ePAlkTdWnv+RWBxlJwMQ25oEHmRguNYf4Zr/Rxr9cS93Y+mdXIZaBEE0KS2iLRqa
OiWBki9IMQU4phqPOBAaG7A+eP8PAgMBAAGjZjBkMB0GA1UdDgQWBBTh0YHlzlpf
BKrS6badZrHF+qwshzAfBgNVHSMEGDAWgBTh0YHlzlpfBKrS6badZrHF+qwshzAS
BgNVHRMBAf8ECDAGAQH/AgEEMA4GA1UdDwEB/wQEAwIBhjANBgkqhkiG9w0BAQsF
AAOCAgEAALIY1wkilt/urfEVM5vKzr6utOeDWCUczmWX/RX4ljpRdgF+5fAIS4vH
tmXkqpSCOVeWUrJV9QvZn6L227ZwuE15cWi8DCDal3Ue90WgAJJZMfTshN4OI8cq
W9E4EG9wglbEtMnObHlms8F3CHmrw3k6KmUkWGoa+/ENmcVl68u/cMRl1JbW2bM+
/3A+SAg2c6iPDlehczKx2oa95QW0SkPPWGuNA/CE8CpyANIhu9XFrj3RQ3EqeRcS
AQQod1RNuHpfETLU/A2gMmvn/w/sx7TB3W5BPs6rprOA37tutPq9u6FTZOcG1Oqj
C/B7yTqgI7rbyvox7DEXoX7rIiEqyNNUguTk/u3SZ4VXE2kmxdmSh3TQvybfbnXV
4JbCZVaqiZraqc7oZMnRoWrXRG3ztbnbes/9qhRGI7PqXqeKJBztxRTEVj8ONs1d
WN5szTwaPIvhkhO3CO5ErU2rVdUr89wKpNXbBODFKRtgxUT70YpmJ46VVaqdAhOZ
D9EUUn4YaeLaS8AjSF/h7UkjOibNc4qVDiPP+rkehFWM66PVnP1Msh93tc+taIfC
EYVMxjh8zNbFuoc7fzvvrFILLe7ifvEIUqSVIC/AzplM/Jxw7buXFeGP1qVCBEHq
391d/9RAfaZ12zkwFsl+IKwE/OZxW8AHa9i1p4GO0YSNuczzEm4=
-----END CERTIFICATE-----
-----BEGIN CERTIFICATE-----
MIIG6DCCBNCgAwIBAgICEAUwDQYJKoZIhvcNAQELBQAwcDELMAkGA1UEBhMCUlUx
PzA9BgNVBAoMNlRoZSBNaW5pc3RyeSBvZiBEaWdpdGFsIERldmVsb3BtZW50IGFu
ZCBDb21tdW5pY2F0aW9uczEgMB4GA1UEAwwXUnVzc2lhbiBUcnVzdGVkIFJvb3Qg
Q0EwHhcNMjQwNzE1MTI1MDQxWhcNMjkwNzE5MTI1MDQxWjBvMQswCQYDVQQGEwJS
VTE/MD0GA1UECgw2VGhlIE1pbmlzdHJ5IG9mIERpZ2l0YWwgRGV2ZWxvcG1lbnQg
YW5kIENvbW11bmljYXRpb25zMR8wHQYDVQQDDBZSdXNzaWFuIFRydXN0ZWQgU3Vi
IENBMIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEA1j0rkZECOt1S8o7I
JY+4YKAxuEa5xaHKHXT2EpkuC/0krqMOjUy2oPIRNgR5g8X0Jl6jamxeGLc4Q1tf
ju6or9oSRYThIUhRsFDQNBiBBEXoBgWxTfiKB2eyT97+pz5TBtBiRCPaLGRHYLRb
9Jz2HkJlxbtNPjtDrF5DPHym+mZ1M1z3hIQYAqJwLpsEBnsw/VxWMlxqHoeewd0h
uJMd71KQ5vOKlz7KrIZ6EobNNa6wItuvsfj3kYCK7O78uLHGXXFxdr8Hae9lMUmC
8F7AFwa+bO1LRlTlqW7rE3rLf+jj70N01N8T3o22v14YBaFBWQWncAVYD2JuL3tH
252+kdNOERf1fLbLRigJAbd+hOhWYlNf963TFDgnNPliHNIW72SygVBnI2V3JwO1
dp1hVKpK/zt8ziGdHW4gmOLTsH50YKdR4jNqUgQv4wASlKn9OpN6zHYc5G8h86fY
BM+zxE5ikGI+I/vIqBuI0eaDU92AWN/YjFLpu8tMu9kLRSCf1vug6FIfDPWVo7iP
ac/SI2v8jnnpaW7ph/Pz3WkzaG7ZZJsfFs+8dploWc6LOoDtbFBhMdGMxu024msC
0PSjZb5ODXPIaO2NsA7fMiAtZcoK6anTUJh4zOP/stA9qsJGNxdrEmiPXSmBZY/N
Y0wkZgZ6JTDhw7038bPvctkblJkCAwEAAaOCAYswggGHMB0GA1UdDgQWBBR3Pdk5
r0K93FvKduru/c4+YSkwXzAfBgNVHSMEGDAWgBTh0YHlzlpfBKrS6badZrHF+qws
hzAOBgNVHQ8BAf8EBAMCAYYwEgYDVR0TAQH/BAgwBgEB/wIBADCBmAYIKwYBBQUH
AQEEgYswgYgwQAYIKwYBBQUHMAKGNGh0dHA6Ly9udWMtY2RwLnZvc2tob2QucnUv
Y2RwL3Jvb3RjYV9zc2xfcnNhMjAyMi5jcnQwRAYIKwYBBQUHMAKGOGh0dHA6Ly9u
dWMtY2RwLmRpZ2l0YWwuZ292LnJ1L2NkcC9yb290Y2Ffc3NsX3JzYTIwMjIuY3J0
MIGFBgNVHR8EfjB8MDqgOKA2hjRodHRwOi8vbnVjLWNkcC52b3NraG9kLnJ1L2Nk
cC9yb290Y2Ffc3NsX3JzYTIwMjIuY3JsMD6gPKA6hjhodHRwOi8vbnVjLWNkcC5k
aWdpdGFsLmdvdi5ydS9jZHAvcm9vdGNhX3NzbF9yc2EyMDIyLmNybDANBgkqhkiG
9w0BAQsFAAOCAgEAmsINXtQ7wwUWvIeOr80MdJS/5G4xhyZOVEmeUorThquT672y
cCg3XCxc4fwbiZqSSbBqntQ7RtiTAKMYMvBageKoVHbzz+R4jX01tKcTx8cDePrz
dJ73bLNUorE7RU9QsW4KyiUeRmjMDV23AUlEvuQFTwgkHXvbac1BBdPn9CrssQuF
5EGohZKcQPFiAAc4SHbRNhlr7uAwgpc/erzI9EAcvA6BVAXcVKoeGpV01uexUgZ6
St5RP9UmDWNA7T4yVXWJ233N0Q8bl+6AswINQ3PosPu6yQQHQjr65YS06epK+AeI
6j+oGR4xI7EhTQhQvaobnGmX/8QQ7XDRYCP2HXYxiffnn/CfZ/BVyKLYeY1ZipjE
nzqdQIC2+Q3WtY8jsVRQMP38WFRmtsIt5snehnPTs5bKGVIcYzj3o3Ex/K7agEz0
zAJ0JR5ivXZOvNkT0g9x1v+S1IkU3e/nX1a+tpRquMtnHX0L2lXArNHUbaOO9EJt
d57WaIpofV5cVhhwShOgAuBc9UMJF3/n4t4RKiPxtsK8P67gcmphMhslj7AMYrYM
ej2NvQZY4m3ub3CPC/PrTjDONvb+8g5xrKtxBjYqC74HSB4dg9G3WimSDUuP2Su6
G2y2TUeyJuCvCLz289VoO0vg7cNdMobE3KCqAiiNhN2VBFxHAUKmUoRcRdw=
-----END CERTIFICATE-----
"""

_ssl_context = ssl.create_default_context()
_ssl_context.load_verify_locations(cadata=_RU_TRUSTED_CA_PEM)


def _proxy_connector(proxy_url: str) -> ProxyConnector:
    """aiohttp_socks/python_socks's URL parser only recognizes socks5/socks4/http
    schemes — 'socks5h' (the curl/requests convention for "resolve via proxy", used
    project-wide in config.yaml since every other proxied adapter is on curl_cffi,
    which does accept it natively) raises ValueError before any request is even
    attempted. Live-verified (2026-08-19): this crashed every rzd_direct call that
    fell back to the relay egress with an uncaught exception, surfacing as a generic
    UPSTREAM_ERROR with the real cause invisible. Normalizing the scheme and asking
    for the same behavior via `rdns=True` explicitly gets the same "resolve through
    the proxy" outcome a `socks5h://` URL was written to mean.
    """
    rdns = proxy_url.startswith("socks5h://")
    normalized = proxy_url.replace("socks5h://", "socks5://", 1) if rdns else proxy_url
    return ProxyConnector.from_url(normalized, rdns=rdns, ssl=_ssl_context)


class RzdDirectAdapter:
    """Unofficial confirmation-tier train prices from pass.rzd.ru's own search
    widget — the two-step RID flow: POST a search, get back
    `{"result": "RID", "RID": N}`, poll the same endpoint with that RID until
    `{"result": "OK", "tp": [...]}` or an error/captcha response. No public
    docs, no auth — this is the same endpoint the site's own JS calls.

    Verified live end-to-end (2026-08-17): a real Moscow->SPb search resolved
    in ~3s (one RID poll), full real fares per car class, no captcha
    triggered. Request/response shapes cross-checked against the reference
    implementation at github.com/devbis/aiorzd, which documents the same flow.

    Exists because Yandex Rasp (the primary train source, `yandex_rasp.py`)
    returns real ticket prices for 0 of the train segments it lists — verified
    across many dates and with/without transport-type filters. This adapter is
    the one place in the roster that actually has RZD's own live prices; the
    plan's §5 "optional, may never be built" framing for `rzd_direct` no
    longer applies now that Yandex Rasp's train pricing gap is confirmed
    structural rather than a query-shape mistake.

    Times: `date0`/`time0`/`date1`/`time1` come back as naive local strings
    with no UTC offset and no per-station tz in the payload. RZD's classic
    (and still dominant) convention is to schedule domestic trains in Moscow
    time — the plan's own timezone note flags exactly this trap — so
    depart/arrive are localized to Europe/Moscow rather than left ambiguous.
    This is a documented simplification, not a verified per-station tz table.
    """

    id = "rzd_direct"
    display_name = "РЖД (pass.rzd.ru)"
    modes = frozenset({Mode.TRAIN})
    tier = Tier.CONFIRMATION
    countries: frozenset[str] | None = frozenset({"RU"})
    supports_batch_dates = False
    request_timeout_s = 15
    # a date here is not one request: the RID flow is a POST plus up to _POLL_TRIES polls
    # paced _POLL_INTERVAL_S apart, so the per-date ceiling is the poll loop's own bound
    # plus one request timeout, and only then multiplied by the dates in a cycle — and by
    # the egress attempts one call may spend replaying that loop through the pool.
    total_timeout_s = probe_dates_total_timeout_s(
        _POLL_TRIES * _POLL_INTERVAL_S + request_timeout_s, egress_attempts=MAX_EGRESS_ATTEMPTS_PER_CALL
    )

    def __init__(
        self,
        *,
        egress_pool: Iterable[str | None] = (),
        rate_limit: RateLimitPolicy | None = None,
        cache_ttl_s: int = 2700,
    ) -> None:
        # §6: RZD gets the most conservative pacing in the roster — the RID flow is the
        # least documented adapter here and the easiest to get rate-limited or captcha'd.
        self.rate_limit = rate_limit or RateLimitPolicy(min_interval_s=20.0)
        self.default_cache_ttl_s = cache_ttl_s
        self._egress = EgressPool(egress_pool)

    async def search(self, q: SearchQuery) -> SearchResult:
        if q.mode != Mode.TRAIN:
            return SearchResult(status=ResultStatus.EMPTY, offers=[], partial=False)
        if not q.origin.rzd_code or not q.dest.rzd_code:
            return SearchResult(status=ResultStatus.EMPTY, offers=[], partial=False, error="missing rzd_code")

        return await self._egress.run(lambda proxy_url: self._search_via(q, proxy_url))

    async def _search_via(self, q: SearchQuery, proxy_url: str | None) -> SearchResult:
        # search() already rejected the query if either rzd_code was missing, so these
        # are never None here — same pattern as travelpayouts._row_to_offer.
        assert q.origin.rzd_code is not None and q.dest.rzd_code is not None
        dates = q.dates or (date.today(),)
        offers: list[Offer] = []
        partial = False
        blocked = False
        connector = _proxy_connector(proxy_url) if proxy_url else aiohttp.TCPConnector(ssl=_ssl_context)
        async with aiohttp.ClientSession(headers=_HEADERS, connector=connector) as session:
            for d in dates:
                trains, outcome = await self._search_day(session, q.origin.rzd_code, q.dest.rzd_code, d)
                if outcome == "blocked":
                    blocked = True
                    continue
                if outcome == "error":
                    partial = True
                    continue
                for train in trains:
                    offer = self._train_to_offer(train, q)
                    if offer is not None:
                        offers.append(offer)

        return build_search_result(offers, blocked=blocked, partial=partial)

    async def _post(
        self, session: aiohttp.ClientSession, src: str, dst: str, day: date, rid: int | None
    ) -> tuple[dict[str, Any] | None, int | None]:
        params = {"layer_id": TRAIN_LAYER_ID}
        data = {
            "checkSeats": "1",
            "dir": "0",
            "tfl": "3",
            "dt0": day.strftime("%d.%m.%Y"),
            "code0": str(src),
            "code1": str(dst),
            "ti0": "0-24",
        }
        if rid is not None:
            data["rid"] = str(rid)
        try:
            async with session.post(
                BASE_URL, params=params, data=data, timeout=aiohttp.ClientTimeout(total=self.request_timeout_s)
            ) as resp:
                if resp.status != 200:
                    return None, resp.status
                text = await resp.text()
        except (aiohttp.ClientError, TimeoutError):
            return None, None
        try:
            result: dict[str, Any] = json.loads(text)
            return result, resp.status
        except ValueError:
            return None, resp.status

    async def _search_day(
        self, session: aiohttp.ClientSession, src: str, dst: str, day: date
    ) -> tuple[list[dict[str, Any]], str]:
        # A 403 is pass.rzd.ru's network-level IP ban (confirmed live — see
        # ARCHITECTURE.md), not a transient upstream hiccup; distinguishing it
        # from a plain non-200/parse failure is what lets /status say "blocked"
        # instead of a generic "error" that looks recoverable on its own.
        response, status = await self._post(session, src, dst, day, rid=None)
        if response is None:
            return [], "blocked" if status == 403 else "error"
        if response.get("error"):
            return [], "blocked"

        rid = response.get("RID")
        for _ in range(_POLL_TRIES):
            if response.get("result") == "OK":
                break
            if rid is None:
                return [], "error"
            await asyncio.sleep(_POLL_INTERVAL_S)
            response, status = await self._post(session, src, dst, day, rid=rid)
            if response is None:
                return [], "blocked" if status == 403 else "error"
            if response.get("error"):
                return [], "blocked"
            rid = response.get("RID", rid)
        else:
            return [], "error"  # never resolved within the poll budget

        tp = response.get("tp") or []
        if not tp:
            return [], "ok"  # legitimate "nothing found" — not a failure
        segment = tp[0]
        msg_list = segment.get("msgList") or []
        if msg_list and msg_list[0].get("errType") == "ERROR":
            return [], "error"
        return segment.get("list") or [], "ok"

    def _train_to_offer(self, train: dict[str, Any], q: SearchQuery) -> Offer | None:
        cars = train.get("cars") or []
        if not cars:
            return None
        cheapest = min(cars, key=lambda c: c["tariff"])

        depart = datetime.strptime(f"{train['date0']} {train['time0']}", "%d.%m.%Y %H:%M").replace(tzinfo=MOSCOW_TZ)
        arrive = None
        if train.get("date1") and train.get("time1"):
            arrive = datetime.strptime(f"{train['date1']} {train['time1']}", "%d.%m.%Y %H:%M").replace(
                tzinfo=MOSCOW_TZ
            )

        now = datetime.now(timezone.utc)
        leg = Leg(
            mode=Mode.TRAIN,
            from_place=q.origin,
            to_place=q.dest,
            depart_utc=depart.astimezone(timezone.utc),
            depart_local=depart,
            carrier=train.get("carrier"),
            number=train.get("number"),
            arrive_utc=arrive.astimezone(timezone.utc) if arrive else None,
            tz="Europe/Moscow",
            fare_class=cheapest.get("typeLoc") or cheapest.get("type"),
            time_precision="exact",
        )
        duration = int((arrive - depart).total_seconds() // 60) if arrive else None
        itinerary_hash = hashlib.sha256(
            f"{self.id}|{q.origin.rzd_code}|{q.dest.rzd_code}|{train.get('number')}|"
            f"{train['date0']}{train['time0']}".encode()
        ).hexdigest()[:16]

        return Offer(
            legs=(leg,),
            price_minor=round(float(cheapest["tariff"]) * 100),
            currency="RUB",
            deep_link=None,
            source_id=self.id,
            tier=Tier.CONFIRMATION,
            observed_at=now,
            source_cached_at=now,
            expires_at=None,
            confidence=1.0,
            itinerary_hash=itinerary_hash,
            duration_minutes=duration,
        )

    async def health_check(self) -> HealthStatus:
        try:
            connector = aiohttp.TCPConnector(ssl=_ssl_context)
            async with aiohttp.ClientSession(headers=_HEADERS, connector=connector) as session:
                _, outcome = await self._search_day(session, "2000000", "2004000", date.today())  # Moscow -> SPb
            return HealthStatus(ok=outcome == "ok")
        except Exception as exc:  # health_check should never crash the caller (§5 rule 1 spirit)
            return HealthStatus(ok=False, detail=str(exc))
