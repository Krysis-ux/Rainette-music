"""Phone sort controls stay with their list heading without crowding playlist titles."""

import os
from pathlib import Path

import pytest

from test_pwa_client_behaviour import FakeComputer, browser, open_phone, static_server


class LibraryComputer(FakeComputer):
    def handle_command(self, payload):
        result = super().handle_command(payload)
        if payload.get("type") == "music_playlist_list":
            result["playlists"][0]["name"] = "A long late-night playlist name that still needs its controls"
        return result


def snapshot(page, name):
    if directory := os.environ.get("RAINETTE_QA_DIR"):
        output = Path(directory)
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / f"{name}.png"))


@pytest.mark.parametrize("width", [402, 432], ids=["iphone", "pixel"])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_library_sort_shares_the_heading_and_keeps_playlists_readable(browser, static_server, width, theme):
    page, errors = open_phone(browser, static_server, LibraryComputer())
    try:
        page.set_viewport_size({"width": width, "height": 874})
        page.evaluate("async theme => (await import('./src/prefs.js')).applyTheme(theme)", theme)
        page.locator("[data-tab='library']").click()
        page.locator("#libraryList .track-shell").first.wait_for(state="visible")

        page.locator("#libraryModePlaylists").click()
        page.locator(".playlist-row").first.click()
        title = page.locator("#libraryList .collection-title")
        title.wait_for(state="visible")
        snapshot(page, f"playlist-{theme}-{width}")

        head = page.locator("#libraryList .collection-head")
        sort = head.get_by_role("button", name="Sort: Default order", exact=True)
        assert sort.count() == 1, "Playlist sorting belongs beside its title and actions"
        assert sort.inner_text() == "", "The sort control must use an icon, not a wide text pill"
        assert head.get_by_role("button", name="Edit playlist", exact=True).count() == 1
        assert head.get_by_role("button", name="Download all", exact=True).count() == 1

        geometry = title.evaluate("""title => {
            const head = title.closest('.collection-head');
            const button = head.querySelector('[aria-label^="Sort:"]');
            const back = document.querySelector('.collection-back');
            const box = element => element.getBoundingClientRect().toJSON();
            return { title: box(title), sort: box(button), head: box(head), back: box(back),
                titleStyle: { transform: getComputedStyle(title).textTransform,
                    whiteSpace: getComputedStyle(title).whiteSpace },
                backAlign: getComputedStyle(back).textAlign,
                overflow: document.documentElement.scrollWidth > innerWidth };
        }""")
        assert geometry["titleStyle"] == {"transform": "none", "whiteSpace": "nowrap"}
        assert geometry["title"]["width"] >= 160
        assert geometry["sort"]["width"] == geometry["sort"]["height"]
        assert geometry["sort"]["x"] > geometry["title"]["x"]
        assert abs(geometry["sort"]["y"] - geometry["head"]["y"]) <= 4
        assert geometry["backAlign"] in ("left", "start")
        assert not geometry["overflow"]

        sort.click()
        page.get_by_role("button", name="Title", exact=True).click()
        selected = head.get_by_role("button", name="Sort: Title", exact=True)
        selected.wait_for(state="visible")
        assert selected.evaluate("button => button.classList.contains('on')")
        assert page.evaluate("localStorage.getItem('rainette.pwa.sort.library')") == "title"

        page.get_by_role("button", name="‹ All playlists", exact=True).click()
        page.locator(".playlist-row").first.wait_for(state="visible")
        assert page.locator("#librarySort").is_hidden()

        for tab, label in (("#libraryModeSongs", "Songs"), ("#libraryModeArtists", "Artists")):
            page.locator(tab).click()
            heading = page.locator("#libraryListHead")
            heading.wait_for(state="visible")
            assert label in heading.inner_text()
            assert heading.get_by_role("button", name="Sort:").count() == 1
            assert heading.evaluate("head => head.contains(document.querySelector('#librarySort'))")
            snapshot(page, f"{label.lower()}-{theme}-{width}")
        assert not errors, errors
    finally:
        page.close()
