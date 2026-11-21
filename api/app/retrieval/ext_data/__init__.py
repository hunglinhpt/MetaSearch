"""External Data Integrations (docs/32).

Free-key / keyless market, weather, FX, crypto, news and IP-geo sources
behind one package. Design law (docs/32 §1):

- **Keyless-first**: every capability MUST work without registration using a
  keyless public source (CoinGecko, Frankfurter, ExchangeRate-API open
  endpoint, Open-Meteo, Yahoo Finance chart, ipwho.is). Registered free keys
  (OpenWeatherMap, NewsAPI, Alpha Vantage, Twelve Data, FMP, IPinfo) are
  optional upgrades wired from env vars.
- **Zero fabrication**: a missing key / dead source / missing field is
  reported as `null` + provenance — never a placeholder number.
- **Free-tier defense**: every source gets a bounded TTL cache + single-flight
  coalescing so a burst of requests cannot exhaust upstream quotas.

All fetches run on the shared ``search`` HTTP pool (`app/http.py`) with the
5MB response cap and browser headers shared with the FA sources.
"""

from app.retrieval.ext_data._base import TtlCache, ext_settings_keys, json_get
from app.retrieval.ext_data.crypto import (
    fetch_crypto_global,
    fetch_crypto_prices,
)
from app.retrieval.ext_data.fx import fetch_fx_rates
from app.retrieval.ext_data.ipgeo import fetch_ipgeo
from app.retrieval.ext_data.markets_global import (
    fetch_fundamentals,
    fetch_global_quote,
)
from app.retrieval.ext_data.news_ext import fetch_external_news
from app.retrieval.ext_data.weather import fetch_weather

__all__ = [
    "TtlCache",
    "ext_settings_keys",
    "fetch_crypto_global",
    "fetch_crypto_prices",
    "fetch_external_news",
    "fetch_fundamentals",
    "fetch_fx_rates",
    "fetch_global_quote",
    "fetch_ipgeo",
    "fetch_weather",
    "json_get",
]
