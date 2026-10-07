import re

with open("src/aviapars/adapters/s7_direct.py", "r") as f:
    content = f.read()

search = """    def _parse_offers(
        self, payload: dict[str, Any], q: SearchQuery, depart_date: date, deep_link: str | None = None
    ) -> list[Offer]:
        offers: list[Offer] = []
        now = datetime.now(timezone.utc)
        tz_cache: dict[str, timezone | ZoneInfo] = {}

        for option_set in payload.get("optionSets") or []:
            for flight_option in option_set.get("flightOptions") or []:
                route = flight_option.get("route") or {}
                segments = route.get("segments") or []
                if not segments:
                    continue

                solutions = flight_option.get("solutions") or {}
                available_solutions = [s for s in solutions.values() if s.get("available")]
                if not available_solutions:
                    continue
                cheapest = min(
                    available_solutions,
                    key=lambda s: s["pricing"]["total"]["salePrice"]["amount"],
                )
                price = cheapest["pricing"]["total"]["salePrice"]

                legs: list[Leg] = []
                for seg in segments:
                    # departureDate/arrivalDate are naive local clock time; the response
                    # gives the real IANA zone per endpoint (verified live: not always the
                    # same zone at both ends), so each side gets localized separately before
                    # conversion — treating either as UTC directly crashed on comparison
                    # against any tz-aware datetime elsewhere in the pipeline (freshness_gate).
                    dep_tz_name = seg.get("departureTimeZone")
                    arr_tz_name = seg.get("arrivalTimeZone")
                    dep_local = datetime.fromisoformat(seg["departureDate"])

                    dep_tz: timezone | ZoneInfo = timezone.utc
                    if dep_tz_name:
                        if dep_tz_name not in tz_cache:
                            tz_cache[dep_tz_name] = ZoneInfo(dep_tz_name)
                        dep_tz = tz_cache[dep_tz_name]
                    dep_utc = dep_local.replace(tzinfo=dep_tz).astimezone(timezone.utc)

                    arr_utc = None
                    if seg.get("arrivalDate"):
                        arr_local = datetime.fromisoformat(seg["arrivalDate"])
                        arr_tz: timezone | ZoneInfo = timezone.utc
                        if arr_tz_name:
                            if arr_tz_name not in tz_cache:
                                tz_cache[arr_tz_name] = ZoneInfo(arr_tz_name)
                            arr_tz = tz_cache[arr_tz_name]
                        arr_utc = arr_local.replace(tzinfo=arr_tz).astimezone(timezone.utc)
                    airline = seg.get("operatingAirline") or {}
                    legs.append(
                        Leg(
                            mode=Mode.PLANE,
                            from_place=q.origin,
                            to_place=q.dest,
                            depart_utc=dep_utc,
                            depart_local=dep_local,
                            carrier=airline.get("name"),
                            number=airline.get("flightNumber"),
                            arrive_utc=arr_utc,
                            tz=dep_tz_name,
                            time_precision="exact",
                        )
                    )

                duration = (route.get("duration") or {}).get("amountInMinutes")
                itinerary_hash = hashlib.sha256(
                    f"{self.id}|{q.origin.iata}|{q.dest.iata}|{depart_date.isoformat()}|"
                    f"{legs[0].number}|{cheapest.get('fareFamily')}".encode()
                ).hexdigest()[:16]

                offers.append(
                    Offer(
                        legs=tuple(legs),
                        price_minor=round(price["amount"] * 100),
                        currency=price.get("currency", q.currency).upper(),
                        deep_link=deep_link,
                        source_id=self.id,
                        tier=Tier.CONFIRMATION,
                        observed_at=now,
                        source_cached_at=now,
                        expires_at=None,
                        confidence=1.0,
                        itinerary_hash=itinerary_hash,
                        duration_minutes=duration,
                    )
                )
        return offers"""

replace = """    def _parse_offers(
        self, payload: dict[str, Any], q: SearchQuery, depart_date: date, deep_link: str | None = None
    ) -> list[Offer]:
        offers: list[Offer] = []
        now = datetime.now(timezone.utc)
        tz_cache: dict[str, timezone | ZoneInfo] = {}

        for option_set in payload.get("optionSets") or []:
            for flight_option in option_set.get("flightOptions") or []:
                offer = self._parse_flight_option(flight_option, q, depart_date, deep_link, now, tz_cache)
                if offer:
                    offers.append(offer)
        return offers

    def _parse_flight_option(
        self,
        flight_option: dict[str, Any],
        q: SearchQuery,
        depart_date: date,
        deep_link: str | None,
        now: datetime,
        tz_cache: dict[str, timezone | ZoneInfo],
    ) -> Offer | None:
        route = flight_option.get("route") or {}
        segments = route.get("segments") or []
        if not segments:
            return None

        solutions = flight_option.get("solutions") or {}
        available_solutions = [s for s in solutions.values() if s.get("available")]
        if not available_solutions:
            return None
        cheapest = min(
            available_solutions,
            key=lambda s: s["pricing"]["total"]["salePrice"]["amount"],
        )
        price = cheapest["pricing"]["total"]["salePrice"]

        legs: list[Leg] = [self._parse_segment(seg, q, tz_cache) for seg in segments]

        duration = (route.get("duration") or {}).get("amountInMinutes")
        itinerary_hash = hashlib.sha256(
            f"{self.id}|{q.origin.iata}|{q.dest.iata}|{depart_date.isoformat()}|"
            f"{legs[0].number}|{cheapest.get('fareFamily')}".encode()
        ).hexdigest()[:16]

        return Offer(
            legs=tuple(legs),
            price_minor=round(price["amount"] * 100),
            currency=price.get("currency", q.currency).upper(),
            deep_link=deep_link,
            source_id=self.id,
            tier=Tier.CONFIRMATION,
            observed_at=now,
            source_cached_at=now,
            expires_at=None,
            confidence=1.0,
            itinerary_hash=itinerary_hash,
            duration_minutes=duration,
        )

    def _parse_segment(
        self, seg: dict[str, Any], q: SearchQuery, tz_cache: dict[str, timezone | ZoneInfo]
    ) -> Leg:
        # departureDate/arrivalDate are naive local clock time; the response
        # gives the real IANA zone per endpoint (verified live: not always the
        # same zone at both ends), so each side gets localized separately before
        # conversion — treating either as UTC directly crashed on comparison
        # against any tz-aware datetime elsewhere in the pipeline (freshness_gate).
        dep_tz_name = seg.get("departureTimeZone")
        dep_local = datetime.fromisoformat(seg["departureDate"])
        dep_tz = self._get_tz(dep_tz_name, tz_cache)
        dep_utc = dep_local.replace(tzinfo=dep_tz).astimezone(timezone.utc)

        arr_utc = None
        if seg.get("arrivalDate"):
            arr_local = datetime.fromisoformat(seg["arrivalDate"])
            arr_tz_name = seg.get("arrivalTimeZone")
            arr_tz = self._get_tz(arr_tz_name, tz_cache)
            arr_utc = arr_local.replace(tzinfo=arr_tz).astimezone(timezone.utc)

        airline = seg.get("operatingAirline") or {}
        return Leg(
            mode=Mode.PLANE,
            from_place=q.origin,
            to_place=q.dest,
            depart_utc=dep_utc,
            depart_local=dep_local,
            carrier=airline.get("name"),
            number=airline.get("flightNumber"),
            arrive_utc=arr_utc,
            tz=dep_tz_name,
            time_precision="exact",
        )

    @staticmethod
    def _get_tz(tz_name: str | None, tz_cache: dict[str, timezone | ZoneInfo]) -> timezone | ZoneInfo:
        if not tz_name:
            return timezone.utc
        if tz_name not in tz_cache:
            tz_cache[tz_name] = ZoneInfo(tz_name)
        return tz_cache[tz_name]"""

if search in content:
    content = content.replace(search, replace)
    with open("src/aviapars/adapters/s7_direct.py", "w") as f:
        f.write(content)
    print("Patch applied")
else:
    print("Search string not found")
