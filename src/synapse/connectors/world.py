"""Earth, weather, and finance connectors: USGS, MET Norway, Frankfurter (ADR 0008).

All three verified live 2026-10-25. Weather is a two-host flow with both hosts
robots-permissive: place names resolve through Open-Meteo's geocoding host
(its robots.txt is absent → default allow) and the forecast comes from
``api.met.no`` (its ``User-agent: *`` block has an empty ``Disallow:``, i.e.
allow-all — verified against Python's robotparser). Open-Meteo's *forecast*
host was rejected as a provider: its robots.txt disallows every path.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from urllib.parse import urlencode

from synapse.connectors.base import (
    Connector,
    ConnectorError,
    RateLimit,
    ResultItem,
    SearchResponse,
    float_of,
    int_of,
    na,
    rate_from_headers,
    text_of,
)

# USGS FDSN event parameters: the documented filter set, only the operators
# a search should expose. Values are passed through; the server validates dates.
_USGS_NUMERIC_PARAMS = frozenset(
    {
        "minlatitude",
        "maxlatitude",
        "minlongitude",
        "maxlongitude",
        "mindepth",
        "maxdepth",
        "minmagnitude",
        "maxmagnitude",
    }
)
_USGS_PARAMS = _USGS_NUMERIC_PARAMS | {"starttime", "endtime"}

_LATLON_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)")


def _title_or_placeholder(value: object) -> str:
    return text_of(value) or "(no title reported)"


def _iso_from_millis(millis: int) -> str:
    return datetime.fromtimestamp(millis / 1000, tz=UTC).isoformat(
        timespec="seconds"
    )


class UsgsConnector(Connector):
    """Recent earthquakes from the USGS event feed.

    The query holds ``key=value`` filters (``minmagnitude=5 starttime=2026-10-01``);
    an empty query means "latest earthquakes worldwide". The feed reports no
    total count, so ``total`` is honestly ``None``.
    """

    name = "usgs"
    domain = "earth"
    description = (
        "USGS earthquakes (query: key=value filters like minmagnitude=5; date = event time)"
    )
    max_results = 100
    allow_empty_query = True

    def build_url(self, query: str, page_size: int) -> str:
        params: dict[str, str] = {
            "format": "geojson",
            "orderby": "time",
            "limit": str(page_size),
        }
        for token in query.split():
            key, sep, value = token.partition("=")
            key = key.strip().lower()
            value = value.strip()
            if not sep or not value:
                raise ConnectorError(
                    "invalid_query",
                    f"unexpected filter {token!r} — usgs expects key=value "
                    "(e.g. minmagnitude=5)",
                )
            if key not in _USGS_PARAMS:
                raise ConnectorError(
                    "invalid_query",
                    f"unsupported filter {key!r}; supported: {', '.join(sorted(_USGS_PARAMS))}",
                )
            if key in _USGS_NUMERIC_PARAMS:
                try:
                    float(value)
                except ValueError:
                    raise ConnectorError(
                        "invalid_query", f"filter {key} must be a number, got {value!r}"
                    ) from None
            params[key] = value
        return "https://earthquake.usgs.gov/fdsnws/event/1/query?" + urlencode(params)

    def parse(
        self, query: str, page_size: int, envelope: object
    ) -> tuple[list[ResultItem], int | None, RateLimit | None]:
        data = self._obj(envelope, "the response")
        rows = self._arr(data.get("features"), "features")

        items: list[ResultItem] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            props = row.get("properties")
            props = props if isinstance(props, dict) else {}
            geometry = row.get("geometry")
            coordinates = geometry.get("coordinates") if isinstance(geometry, dict) else None
            if not isinstance(coordinates, list):
                coordinates = []
            lon = float_of(coordinates[0]) if len(coordinates) > 0 else None
            lat = float_of(coordinates[1]) if len(coordinates) > 1 else None
            depth = float_of(coordinates[2]) if len(coordinates) > 2 else None
            millis = int_of(props.get("time"))
            facts: list[tuple[str, str]] = [
                ("magnitude", na(float_of(props.get("mag")))),
                ("depth km", na(depth)),
                ("lat,lon", f"{lat},{lon}" if lat is not None and lon is not None else "n/a"),
                ("tsunami", "yes" if props.get("tsunami") == 1 else "no"),
            ]
            items.append(
                ResultItem(
                    title=_title_or_placeholder(props.get("title")),
                    url=text_of(props.get("url")),
                    date=_iso_from_millis(millis) if millis is not None else None,
                    snippet=text_of(props.get("place")),
                    facts=tuple(facts),
                )
            )
        # Verified: USGS metadata carries limit/offset/status but no total count.
        return items, None, None


class WeatherConnector(Connector):
    """MET Norway location forecast for a place name (geocoded) or ``lat,lon``.

    Two verified hosts, one request each: Open-Meteo geocoding for place names,
    MET Norway ``locationforecast/2.0/compact`` for the forecast itself.
    """

    name = "weather"
    domain = "weather"
    description = "MET Norway forecast by place name or 'lat,lon' (date = forecast hour)"
    max_results = 1  # one location, one forecast — the API has no pages

    def build_url(self, query: str, page_size: int) -> str:
        raise NotImplementedError("weather orchestrates its own two-host flow")

    def parse(
        self, query: str, page_size: int, envelope: object
    ) -> tuple[list[ResultItem], int | None, RateLimit | None]:
        raise NotImplementedError("weather parses in _run")

    def _run(self, query: str, limit: int, page_size: int) -> SearchResponse:
        coords = _parse_latlon(query)
        place: str | None = None
        if coords is None:
            geo_url = "https://geocoding-api.open-meteo.com/v1/search?" + urlencode(
                {"name": query, "count": 1}
            )
            text, _geo_headers = self._get(geo_url)
            data = self._obj(self._json(text), "the geocoding response")
            results = data.get("results")
            if not isinstance(results, list) or not results:
                # A measured zero (no place matched), not a failure.
                return self._assemble(
                    query=query,
                    limit=limit,
                    page_size=page_size,
                    items=[],
                    total=0,
                    rate=None,
                )
            first = self._obj(results[0], "the first geocoding result")
            lat = float_of(first.get("latitude"))
            lon = float_of(first.get("longitude"))
            if lat is None or lon is None:
                raise ConnectorError(
                    "invalid_response",
                    "weather geocoder returned a result without coordinates",
                )
            place = ", ".join(
                part
                for part in (text_of(first.get("name")), text_of(first.get("country")))
                if part
            ) or None
            coords = (lat, lon)

        lat, lon = coords
        forecast_url = "https://api.met.no/weatherapi/locationforecast/2.0/compact?" + urlencode(
            {"lat": lat, "lon": lon}
        )
        text, headers = self._get(forecast_url)
        items = self._forecast_items(self._json(text), place, lat, lon)
        return self._assemble(
            query=query,
            limit=limit,
            page_size=page_size,
            items=items,
            total=len(items),
            rate=rate_from_headers(headers),
        )

    def _forecast_items(
        self, envelope: object, place: str | None, lat: float, lon: float
    ) -> list[ResultItem]:
        data = self._obj(envelope, "the forecast response")
        properties = data.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        series = properties.get("timeseries")
        if not isinstance(series, list) or not series:
            raise ConnectorError(
                "invalid_response", "MET Norway returned no timeseries for this location"
            )
        first = self._obj(series[0], "the first forecast entry")
        node = first.get("data")
        node = node if isinstance(node, dict) else {}
        instant = node.get("instant")
        instant = instant if isinstance(instant, dict) else {}
        details = instant.get("details")
        details = details if isinstance(details, dict) else {}
        next_hour = node.get("next_1_hours")
        next_hour = next_hour if isinstance(next_hour, dict) else {}
        summary = next_hour.get("summary")
        summary = summary if isinstance(summary, dict) else {}
        hour_details = next_hour.get("details")
        hour_details = hour_details if isinstance(hour_details, dict) else {}
        meta = properties.get("meta")
        meta = meta if isinstance(meta, dict) else {}
        units = meta.get("units")
        units = units if isinstance(units, dict) else {}

        def with_unit(value: float | None, unit_key: str) -> str:
            if value is None:
                return "n/a"  # reported null (seen live) must not become 0
            unit = text_of(units.get(unit_key))
            return f"{value} ({unit})" if unit else str(value)

        facts: list[tuple[str, str]] = [
            ("temperature", with_unit(float_of(details.get("air_temperature")), "air_temperature")),
            ("wind", with_unit(float_of(details.get("wind_speed")), "wind_speed")),
            ("humidity", with_unit(float_of(details.get("rel_humidity")), "rel_humidity")),
            (
                "precipitation next hour",
                with_unit(
                    float_of(hour_details.get("precipitation_amount")), "precipitation_amount"
                ),
            ),
            ("model updated", na(text_of(meta.get("updated_at")))),
        ]
        label = place or f"{lat}, {lon}"
        return [
            ResultItem(
                title=f"Weather — {label}",
                url=None,  # the API exposes no per-location page we could verify
                date=text_of(first.get("time")),
                snippet=text_of(summary.get("symbol_code")),  # server text, e.g. "cloudy"
                facts=tuple(facts),
            )
        ]


def _parse_latlon(query: str) -> tuple[float, float] | None:
    """``"59.91, 10.75"`` → coordinates; anything else → None (treated as a place)."""
    match = _LATLON_RE.fullmatch(query)
    if match is None:
        return None
    lat = float(match.group(1))
    lon = float(match.group(2))
    if not -90 <= lat <= 90:
        raise ConnectorError("invalid_query", f"latitude {lat} is outside -90..90")
    if not -180 <= lon <= 180:
        raise ConnectorError("invalid_query", f"longitude {lon} is outside -180..180")
    return lat, lon


class FrankfurterConnector(Connector):
    """Frankfurter exchange rates (European Central Bank reference rates, keyless).

    The query is a base currency plus optional symbols (``USD`` or ``USD EUR GBP``).
    The API has no pagination: ``_assemble`` slices to the requested limit while
    ``total`` keeps the provider's full rate count.
    """

    name = "frankfurter"
    domain = "finance"
    description = "ECB exchange rates via Frankfurter (query: base currency, e.g. 'USD')"
    max_results = 100

    def build_url(self, query: str, page_size: int) -> str:
        tokens = [token.upper() for token in query.split()]
        base = tokens[0]
        if len(base) != 3 or not base.isalpha():
            raise ConnectorError(
                "invalid_query",
                f"expected a 3-letter base currency like 'USD', got {query.split()[0]!r}",
            )
        params: dict[str, str] = {"base": base}
        symbols: list[str] = []
        for token in tokens[1:]:
            if len(token) != 3 or not token.isalpha():
                raise ConnectorError(
                    "invalid_query",
                    f"{token!r} is not a 3-letter currency code "
                    "(query: base [symbol ...], e.g. 'USD EUR')",
                )
            symbols.append(token)
        if symbols:
            params["symbols"] = ",".join(symbols)
        return "https://api.frankfurter.dev/v1/latest?" + urlencode(params)

    def parse(
        self, query: str, page_size: int, envelope: object
    ) -> tuple[list[ResultItem], int | None, RateLimit | None]:
        data = self._obj(envelope, "the response")
        rates = data.get("rates")
        if not isinstance(rates, dict):
            raise ConnectorError("invalid_response", "frankfurter returned no rates object")
        base = text_of(data.get("base")) or query.split()[0].upper()
        rate_date = text_of(data.get("date"))
        amount = float_of(data.get("amount"))

        items: list[ResultItem] = []
        for symbol, value in rates.items():
            rate = float_of(value)
            if rate is None or not isinstance(symbol, str):
                continue
            quantity = f"{amount:g} " if amount is not None else ""
            items.append(
                ResultItem(
                    title=f"{base} → {symbol}",
                    url=None,  # rates have no per-pair page; none invented
                    date=rate_date,
                    snippet=f"{rate} {symbol} per {quantity}{base}",
                    facts=(("rate", str(rate)), ("date", na(rate_date))),
                )
            )
        return items, len(items), None
