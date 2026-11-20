"""Tests for Last30Days-inspired adapters: Careers, Polymarket, StockTwits, USPTO."""

from __future__ import annotations

from app.config import Settings, get_settings
from app.retrieval.adapters.careers import CareersAdapter
from app.retrieval.adapters.polymarket import PolymarketAdapter, PolymarketTrendingAdapter
from app.retrieval.adapters.stocktwits import StockTwitsAdapter, StockTwitsTrendingAdapter
from app.retrieval.adapters.uspto import USPTOAdapter, USPTOCompanyAdapter
from app.retrieval.registry import build_adapters


class TestAdapterInstantiation:
    """Test that all adapters can be instantiated."""

    def test_careers_adapter_instantiation(self):
        adapter = CareersAdapter()
        assert adapter.id == "careers"

    def test_polymarket_adapter_instantiation(self):
        adapter = PolymarketAdapter(api_key="test_key")
        assert adapter.id == "polymarket"

    def test_polymarket_trending_adapter_instantiation(self):
        adapter = PolymarketTrendingAdapter(api_key="test_key")
        assert adapter.id == "polymarket_trending"

    def test_stocktwits_adapter_instantiation(self):
        adapter = StockTwitsAdapter(api_key="test_key")
        assert adapter.id == "stocktwits"

    def test_stocktwits_trending_adapter_instantiation(self):
        adapter = StockTwitsTrendingAdapter(api_key="test_key")
        assert adapter.id == "stocktwits_trending"

    def test_uspto_adapter_instantiation(self):
        adapter = USPTOAdapter(api_key="test_key")
        assert adapter.id == "uspto"

    def test_uspto_company_adapter_instantiation(self):
        adapter = USPTOCompanyAdapter(api_key="test_key")
        assert adapter.id == "uspto_company"


class TestAdapterRegistry:
    """Test that adapters are properly registered."""

    def test_adapters_in_registry(self):
        settings = get_settings()
        adapters = build_adapters(settings)

        # Check that new adapters are in the registry
        assert "careers" in adapters
        assert "polymarket" in adapters
        assert "polymarket_trending" in adapters
        assert "stocktwits" in adapters
        assert "stocktwits_trending" in adapters
        assert "uspto" in adapters
        assert "uspto_company" in adapters

    def test_adapter_types_in_registry(self):
        settings = get_settings()
        adapters = build_adapters(settings)

        # Check adapter types
        assert isinstance(adapters["careers"], CareersAdapter)
        assert isinstance(adapters["polymarket"], PolymarketAdapter)
        assert isinstance(adapters["polymarket_trending"], PolymarketTrendingAdapter)
        assert isinstance(adapters["stocktwits"], StockTwitsAdapter)
        assert isinstance(adapters["stocktwits_trending"], StockTwitsTrendingAdapter)
        assert isinstance(adapters["uspto"], USPTOAdapter)
        assert isinstance(adapters["uspto_company"], USPTOCompanyAdapter)


class TestConfigSettings:
    """Test that config settings are available."""

    def test_config_has_new_api_keys(self):
        settings = get_settings()

        # Check that new config keys exist
        assert hasattr(settings, "polymarket_api_key")
        assert hasattr(settings, "stocktwits_api_key")
        assert hasattr(settings, "uspto_api_key")
        assert hasattr(settings, "careers_enabled")

    def test_config_defaults(self):
        settings = Settings()

        # Check default values
        assert settings.polymarket_api_key == ""
        assert settings.stocktwits_api_key == ""
        assert settings.uspto_api_key == ""
        assert settings.careers_enabled is True


class TestCareersAdapterHasSearchMethod:
    """Test that CareersAdapter has search method."""

    def test_careers_has_search(self):
        adapter = CareersAdapter()
        assert hasattr(adapter, "search")
        assert callable(adapter.search)


class TestPolymarketAdapterHasSearchMethod:
    """Test that PolymarketAdapter has search method."""

    def test_polymarket_has_search(self):
        adapter = PolymarketAdapter(api_key="test_key")
        assert hasattr(adapter, "search")
        assert callable(adapter.search)


class TestStockTwitsAdapterHasSearchMethod:
    """Test that StockTwitsAdapter has search method."""

    def test_stocktwits_has_search(self):
        adapter = StockTwitsAdapter(api_key="test_key")
        assert hasattr(adapter, "search")
        assert callable(adapter.search)


class TestUSPTOAdapterHasSearchMethod:
    """Test that USPTOAdapter has search method."""

    def test_uspto_has_search(self):
        adapter = USPTOAdapter(api_key="test_key")
        assert hasattr(adapter, "search")
        assert callable(adapter.search)
