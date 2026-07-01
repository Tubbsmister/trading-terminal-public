"""Tests for the keyless X watchlist collector."""

from __future__ import annotations

import pipeline.signals.x_collector as xc


NITTER_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>WhiteHouse / @WhiteHouse</title>
  <item>
    <title>President signs order rescinding the June 12 export directive.</title>
    <link>https://nitter.net/WhiteHouse/status/1810000000000000001#m</link>
    <guid>https://nitter.net/WhiteHouse/status/1810000000000000001#m</guid>
    <pubDate>Wed, 01 Jul 2026 03:39:44 GMT</pubDate>
  </item>
  <item>
    <title>Short</title>
    <link>https://nitter.net/WhiteHouse/status/1810000000000000002#m</link>
    <guid>https://nitter.net/WhiteHouse/status/1810000000000000002#m</guid>
    <pubDate>Wed, 01 Jul 2026 04:00:00 GMT</pubDate>
  </item>
</channel></rss>"""


def test_parse_nitter_rss_extracts_id_url_and_source():
    sigs = xc.parse_nitter_rss(NITTER_RSS, "WhiteHouse")
    assert len(sigs) == 2
    s = sigs[0]
    assert s.source == "x:@WhiteHouse"
    assert s.source_tier == 3                      # SOCIAL
    assert s.external_id == "x:1810000000000000001"
    assert s.url == "https://x.com/WhiteHouse/status/1810000000000000001"
    assert "export directive" in s.title
    assert s.published_at and s.published_at.startswith("2026-07-01T03:39")


def test_parse_nitter_rss_bad_xml_is_empty_not_raise():
    assert xc.parse_nitter_rss(b"not xml at all", "x") == []


def test_mk_signal_strips_html_and_drops_empty():
    assert xc._mk_signal("h", "  <b></b>  ", tweet_id=None, url=None,
                         published_at=None) is None
    s = xc._mk_signal("h", "<p>hello world</p>", tweet_id="9", url="u",
                      published_at=None)
    assert s is not None and s.title == "hello world"


def test_env_list_parsing(monkeypatch):
    monkeypatch.setenv("X_WATCHLIST", " @Foo , Bar ,")
    assert xc._env_list("X_WATCHLIST", ["default"]) == ["Foo", "Bar"]
    monkeypatch.delenv("X_WATCHLIST", raising=False)
    assert xc._env_list("X_WATCHLIST", ["default"]) == ["default"]


def test_fetch_watchlist_degrades_to_empty_when_all_routes_fail(monkeypatch):
    """Every keyless route down must yield [] — never an exception."""
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    monkeypatch.setattr(xc, "_fetch_nitter", lambda h: [])
    monkeypatch.setattr(xc, "_fetch_reader", lambda h: [])
    assert xc.fetch_x_watchlist(handles=["WhiteHouse", "AnthropicAI"]) == []


def test_fetch_watchlist_uses_official_when_token_present(monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", "tok")
    called = {}

    def fake_official(handles, query, token):
        called["args"] = (handles, query, token)
        return ["sentinel"]

    monkeypatch.setattr(xc, "_fetch_official", fake_official)
    out = xc.fetch_x_watchlist(handles=["A"], query="q")
    assert out == ["sentinel"]
    assert called["args"] == (["A"], "q", "tok")


def test_nitter_falls_through_to_reader(monkeypatch):
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    monkeypatch.setattr(xc, "_fetch_nitter", lambda h: [])
    sentinel = xc._mk_signal("A", "from reader proxy line", tweet_id=None,
                             url=None, published_at=None)
    monkeypatch.setattr(xc, "_fetch_reader", lambda h: [sentinel])
    out = xc.fetch_x_watchlist(handles=["A"])
    assert out == [sentinel]
