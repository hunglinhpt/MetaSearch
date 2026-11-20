# Public API Sources - Free Data Integration

## Overview

This document lists **public APIs that require no API key or provide instant free API keys** for integration into MetaSearch. These APIs can be used for research, testing, and production without registration barriers.

---

## 🎯 Currently Integrated Public APIs (No Key Required)

### 1. **IP-API (Geolocation)**
- **Adapter**: `ipapi.py`
- **Endpoint**: `http://ip-api.com/json/{ip}`
- **Rate Limit**: 45 requests/minute (non-commercial)
- **Features**: IP geolocation, ISP info, ASN, timezone
- **Status**: ✅ Integrated
- **Category**: geolocation, ip, research

### 2. **Open Trivia Database**
- **Adapter**: `opentdb.py`
- **Endpoint**: `https://opentdb.com/api.php`
- **Rate Limit**: Unlimited (fair use)
- **Features**: Trivia questions, multiple categories, difficulties
- **Status**: ✅ Integrated
- **Category**: trivia, quiz, education

### 3. **Dog CEO API**
- **Adapter**: `dogceo.py`
- **Endpoint**: `https://dog.ceo/api/`
- **Rate Limit**: Generous (no strict limit)
- **Features**: Random dog images, breed listing, breed-specific images
- **Status**: ✅ Integrated
- **Category**: images, animals, testing

---

## 📋 Public APIs to Integrate Next (No Key Required)

### 4. **Cat API**
- **URL**: `https://api.thecatapi.com/v1/`
- **Features**: Random cat images, breed info
- **Rate Limit**: 10 requests/sec (fair use)
- **Note**: Can use without key (lower priority)
- **Category**: images, animals

### 5. **HTTPbin**
- **URL**: `https://httpbin.org/`
- **Features**: HTTP request/response testing
- **Rate Limit**: Unlimited
- **Use Case**: Debugging, testing
- **Category**: testing, debugging

### 6. **JSONPlaceholder**
- **URL**: `https://jsonplaceholder.typicode.com/`
- **Features**: Fake REST API for testing
- **Rate Limit**: Unlimited
- **Use Case**: Testing, development
- **Category**: testing, mock

### 7. **MockAPI**
- **URL**: `https://mockapi.io/`
- **Features**: Custom mock APIs
- **Rate Limit**: Generous
- **Use Case**: Testing
- **Category**: testing, mock

### 8. **Rest Countries**
- **URL**: `https://restcountries.com/`
- **Features**: Country information, flags, currencies
- **Rate Limit**: Generous
- **Category**: geography, reference

### 9. **Open Street Map (Nominatim)**
- **URL**: `https://nominatim.openstreetmap.org/`
- **Features**: Geocoding, reverse geocoding
- **Rate Limit**: 1 request/sec
- **Note**: Requires email in User-Agent
- **Category**: geolocation, maps

### 10. **GitHub API** (Public Repos)
- **URL**: `https://api.github.com/`
- **Features**: Public repositories, issues, users
- **Rate Limit**: 60 requests/hour (unauthenticated)
- **Note**: Already partially integrated, can expand
- **Category**: code, development

### 11. **NPM Registry API**
- **URL**: `https://registry.npmjs.org/`
- **Features**: Package information, versions, dependencies
- **Rate Limit**: Generous
- **Category**: development, packages

### 12. **PyPI API**
- **URL**: `https://pypi.org/pypi/`
- **Features**: Python package information
- **Rate Limit**: Generous
- **Category**: development, packages

### 13. **Wikipedia API**
- **URL**: `https://en.wikipedia.org/w/api.php`
- **Features**: Article content, search, metadata
- **Rate Limit**: Generous (follow bot guidelines)
- **Note**: Already integrated via wikipedia.py
- **Category**: reference, encyclopedia

---

## 🔑 APIs with Instant Free Keys (Quick Registration)

### Financial & Market Data

| Service | URL | Free Tier | Instant Key | Notes |
|---------|-----|-----------|-------------|-------|
| **Alpha Vantage** | `https://www.alpha-vantage.co` | 5 req/min, 500/day | ✅ Yes | Stocks, forex, crypto |
| **Twelve Data** | `https://twelvedata.com` | 8 req/sec | ✅ Yes | Stocks, forex, crypto |
| **Financial Modeling Prep** | `https://financialmodelingprep.com` | 250 req/day | ✅ Yes | Financial statements |
| **Yahoo Finance** | Already integrated | Unlimited | ❌ No | Via yahoo_finance.py |
| **CoinGecko** | `https://www.coingecko.com` | 10-30 req/min | ✅ Optional | Crypto prices |
| **CoinCap** | `https://coincap.io` | Free | ❌ No | Crypto data |

### News & Media

| Service | URL | Free Tier | Instant Key | Notes |
|---------|-----|-----------|-------------|-------|
| **NewsAPI** | `https://newsapi.org` | 100 req/day | ✅ Yes | Global news |
| **GNews** | `https://gnews.io` | 100 req/day | ✅ Yes | Google News |
| **ContextualWeb News** | `https://contextualweb.io` | 500 req/day | ✅ Yes | News search |
| **TheNewsAPI** | `https://thenewsapi.com` | 100 req/day | ✅ Yes | News aggregator |

### Weather & Environment

| Service | URL | Free Tier | Instant Key | Notes |
|---------|-----|-----------|-------------|-------|
| **OpenWeatherMap** | `https://openweathermap.org` | 1000/day | ✅ Yes | Weather data |
| **WeatherAPI** | `https://www.weatherapi.com` | 1M req/month | ✅ Yes | Weather + forecasts |
| **WeatherStack** | `https://weatherstack.com` | 1000 req/month | ✅ Yes | Real-time weather |
| **NOAA** | `https://www.noaa.gov` | Unlimited | ❌ No | US weather data |

### Geolocation & IP

| Service | URL | Free Tier | Instant Key | Notes |
|---------|-----|-----------|-------------|-------|
| **IPinfo** | `https://ipinfo.io` | 50K req/month | ✅ Yes | IP geolocation |
| **IPGeolocation** | `https://ipgeolocation.io` | 1K req/day | ✅ Yes | IP + weather |
| **IP2Location** | `https://www.ip2location.com` | Demo | ✅ Yes | IP geolocation |

### Social Media

| Service | URL | Free Tier | Instant Key | Notes |
|---------|-----|-----------|-------------|-------|
| **Pushshift Reddit** | `https://api.pushshift.io` | Free | ❌ No | Reddit data |
| **Social Media APIs** | Various | Varies | Varies | Limited free access |

### Development & Tools

| Service | URL | Free Tier | Instant Key | Notes |
|---------|-----|-----------|-------------|-------|
| **ExchangeRate-API** | `https://www.exchangerate-api.com` | 1500/month | ✅ Yes | Currency conversion |
| **Free Currency API** | `https://freecurrencyapi.com` | Unlimited | ✅ Yes | Currency data |
| **Abstract API** | `https://www.abstractapi.com` | 100 req/day | ✅ Yes | Multiple APIs |

### AI & Machine Learning

| Service | URL | Free Tier | Instant Key | Notes |
|---------|-----|-----------|-------------|-------|
| **Hugging Face** | `https://huggingface.co` | Free | ✅ Yes | ML models |
| **OpenAI Public Datasets** | Various | Free | ❌ No | Research datasets |

### Government & Public Data

| Service | URL | Free Tier | Instant Key | Notes |
|---------|-----|-----------|-------------|-------|
| **Data.gov** | `https://data.gov` | Unlimited | ❌ No | US government data |
| **World Bank** | `https://data.worldbank.org` | Unlimited | ❌ No | Economic data |
| **IMF** | `https://data.imf.org` | Unlimited | ❌ No | Financial data |
| **UN Data** | `http://data.un.org` | Unlimited | ❌ No | UN datasets |
| **NASA Open Data** | `https://data.nasa.gov` | Unlimited | ❌ No | Space/science |
| **US Census** | `https://data.census.gov` | Unlimited | ❌ No | Demographic data |

---

## 🚀 Integration Priority

### Priority 1: No API Key Required (Immediate)
1. ✅ **IP-API** - Integrated
2. ✅ **Open Trivia Database** - Integrated
3. ✅ **Dog CEO API** - Integrated
4. **Rest Countries** - High value for geography
5. **Open Street Map** - Useful for geocoding
6. **JSONPlaceholder** - Testing utility

### Priority 2: Instant Free Keys (Quick Wins)
1. **Alpha Vantage** - Financial data (high value)
2. **NewsAPI** - Global news (high value)
3. **OpenWeatherMap** - Weather data (high value)
4. **CoinGecko** - Cryptocurrency (high value)
5. **IPinfo** - IP geolocation (high value)

### Priority 3: Requires Approval or Limited
1. **Twitter API** - Requires approval (limited free)
2. **Google Maps** - Requires billing setup
3. **AWS Free Tier** - Requires credit card

---

## 📊 Current Status

### Integrated Adapters: **38 Total**
- **Original**: 31 adapters
- **Last30Days**: 7 adapters (careers, polymarket, stocktwits, uspto, etc.)
- **Public APIs**: 3 adapters (ipapi, opentdb, dogceo)

### Next Steps
1. Integrate **Rest Countries** for geography data
2. Integrate **Open Street Map** for geocoding
3. Integrate **Alpha Vantage** for financial data
4. Integrate **NewsAPI** for global news
5. Add more public APIs from this list

---

## 💡 Recommendations

### For Immediate Use (No Registration)
```python
# These work immediately without any setup:
from app.retrieval.adapters.ipapi import IPAPIGeolocationAdapter
from app.retrieval.adapters.opentdb import OpenTriviaAdapter
from app.retrieval.adapters.dogceo import DogCEOAdapter
```

### For Financial Data
```bash
# Get free API key from Alpha Vantage (instant approval)
# https://www.alpha-vantage.co/support/#api-key
ALPHA_VANTAGE_API_KEY=your_free_key
```

### For News Data
```bash
# Get free API key from NewsAPI (instant approval)
# https://newsapi.org/register
NEWSAPI_KEY=your_free_key
```

### For Weather Data
```bash
# Get free API key from OpenWeatherMap (instant approval)
# https://home.openweathermap.org/users/sign_up
OPENWEATHERMAP_API_KEY=your_free_key
```

---

## 📝 Notes

1. **Rate Limits**: Always respect API rate limits. Implement backoff/retry logic.
2. **Caching**: Cache responses to minimize API calls and stay within limits.
3. **Error Handling**: Handle rate limiting gracefully (HTTP 429).
4. **Attribution**: Some APIs require attribution in your application.
5. **Commercial Use**: Check license terms for commercial use restrictions.

---

## 🔗 Quick Links

- [IP-API](http://ip-api.com/docs) - Geolocation
- [Open Trivia DB](https://opentdb.com/api_config.php) - Trivia Questions
- [Dog CEO API](https://dog.ceo/dog-api/) - Dog Images
- [Rest Countries](https://restcountries.com/) - Country Data
- [Alpha Vantage](https://www.alpha-vantage.co/) - Financial Data
- [NewsAPI](https://newsapi.org/) - News Aggregator
- [OpenWeatherMap](https://openweathermap.org/api) - Weather Data

---

**Last Updated**: 2026-11-20
**Total Public APIs**: 3 Integrated + 20+ Available for Integration
