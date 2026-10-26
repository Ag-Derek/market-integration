"""The live pages' shared rendering helpers (#22): served, and loaded by
every page that takes live updates before the page's own script."""

import pytest


def test_render_helpers_are_served(client):
    script = client.get("/static/render.js")
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]
    for helper in ("batch", "text", "cls", "flash", "canvas"):
        assert f"{helper}: {helper}" in script.text


def test_the_ticker_shows_the_watchlist_not_a_fixed_symbol_list(client):
    # #27: the cards and sidebar come from /watchlists/me, with an empty
    # state pointing at the search bar.
    page = client.get("/ticker").text
    assert 'id="watchList"' in page and 'id="pickerList"' not in page
    assert "Search for a ticker to pin it." in page
    assert "MarketSearch.loadWatchlist()" in page and "MarketSearch.saveWatchlist(" in page


def test_the_market_strip_is_served(client):
    script = client.get("/static/strip.js")
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]
    assert "window.MarketStrip = { mount: mount }" in script.text


@pytest.mark.parametrize("path, crawl", [
    ("/ticker", "equity"),
    ("/fixed-income", "fixed_income"),
    ("/yield-curve", "fixed_income"),
    ("/bond/GHGGOG069931", "fixed_income"),
    ("/commodities", "commodity"),
    ("/commodity/GEJWM2", "commodity"),
])
def test_pages_mount_the_market_strip_after_the_render_helpers(client, path, crawl):
    page = client.get(path).text
    render = '<script src="/static/render.js"></script>'
    strip = '<script src="/static/strip.js"></script>'
    assert 'id="strip"' in page
    assert page.index(render) < page.index(strip)
    assert f'{{ crawl: "{crawl}" }}' in page


@pytest.mark.parametrize("path", ["/ticker", "/stock/MTNGH", "/fixed-income", "/bond/GHGGOG069931", "/commodity/GEJWM2"])
def test_live_pages_load_the_render_helpers_first(client, path):
    page = client.get(path).text
    tag = '<script src="/static/render.js"></script>'
    assert tag in page
    # Before the page's own inline script, which uses window.Render.
    assert page.index(tag) < page.index("<script>")
