import os

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.skipif(
    os.getenv("RUN_PLAYWRIGHT") != "1",
    reason="Set RUN_PLAYWRIGHT=1 with backend and frontend servers running.",
)
def test_dashboard_loads_value_board(page: Page) -> None:
    page.goto(os.getenv("APP_URL", "http://127.0.0.1:5174"))
    expect(page.get_by_role("heading", name="Prop Value Board")).to_be_visible()
    expect(page.get_by_text("Pregame Props")).to_be_visible()
    expect(page.get_by_role("row", name="A'ja Wilson LV | DraftKings")).to_be_visible()
    page.get_by_role("button", name="Matchups").click()
    expect(page.get_by_role("heading", name="Matchup Board")).to_be_visible()
    expect(page.get_by_text("LV at IND")).to_be_visible()
