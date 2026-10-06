"""The live pages' shared rendering helpers (#22): served, and loaded by
every page that takes live updates before the page's own script."""

import pytest


def test_render_helpers_are_served(client):
    script = client.get("/static/render.js")
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]
    for helper in ("batch", "text", "cls", "flash", "canvas"):
        assert f"{helper}: {helper}" in script.text


@pytest.mark.parametrize("path", ["/ticker", "/stock/MTNGH", "/fixed-income", "/bond/GHGGOG069931"])
def test_live_pages_load_the_render_helpers_first(client, path):
    page = client.get(path).text
    tag = '<script src="/static/render.js"></script>'
    assert tag in page
    # Before the page's own inline script, which uses window.Render.
    assert page.index(tag) < page.index("<script>")
