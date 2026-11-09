"""数据源与离线夹具的测试。"""

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from config import Settings
from data.providers import FallbackProvider, MockProvider, build_provider
from data.stake_client import StakeError

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "mock_events.json"


def test_mock_provider_loads_shipped_fixture():
    provider = MockProvider(FIXTURE)
    events = provider.fetch_events()

    assert len(events) == 10
    assert {event.status for event in events} == {"upcoming", "live", "ended"}
    assert len([event for event in events if event.status == "ended"]) == 4
    assert all(event.market("1x2") is not None for event in events)
    # 每场都有 1X2，且赔率大于 1
    for event in events:
        for outcome in event.market("1x2").outcomes:
            assert outcome.odds > Decimal("1")

    results = provider.fetch_results()
    assert len(results) == 4
    assert all(result.home_score >= 0 for result in results)
    # 离线数据必须带来源标记，切线上后才能被精确清理
    assert all(event.source == "mock" for event in events)


def test_mock_provider_relative_times():
    fixed = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    provider = MockProvider(FIXTURE, now=fixed)
    events = provider.fetch_events()

    upcoming = next(event for event in events if event.status == "upcoming")
    live = next(event for event in events if event.status == "live")
    ended = next(event for event in events if event.status == "ended")
    assert upcoming.start_time > fixed
    assert live.start_time <= fixed
    assert ended.score is not None


def test_fallback_provider_degrades_to_mock():
    class Broken:
        name = "stake"
        label = "Stake 线上"

        def fetch_events(self):
            raise StakeError("boom")

        def fetch_results(self):
            raise StakeError("boom")

    provider = FallbackProvider(Broken(), MockProvider(FIXTURE))
    events = provider.fetch_events()

    assert len(events) == 10
    assert provider.active == "mock"
    assert "boom" in provider.last_warning
    assert provider.label == "离线演示"


def test_build_provider_honours_config():
    settings = Settings(data_source="mock", mock_dir=FIXTURE.parent)
    provider = build_provider(settings)
    assert provider.name == "mock"
    assert len(provider.fetch_events()) == 10
