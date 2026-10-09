import json
import sqlite3
from datetime import datetime, timezone

import pytest

from backend.app.rotowire_scouting import FIELDS, TEAM_SLUGS, parse_scouting_table, pull_scouting_reports


HTML = """<table class="scoutreport"><thead><tr><th>Scouting Report</th><th>Offense</th><th>Defense</th></tr></thead><tbody>
<tr><td>Efficiency</td><td><span>98.3</span> (14th)</td><td><span>85.1</span> (9th)</td></tr>
<tr><td>Pace</td><td colspan="2"><span>99.6</span> (5th)</td></tr>
<tr><td>Effective FG%</td><td>48.2 (14th)</td><td>50.0 (3rd)</td></tr>
<tr><td>Turnover %</td><td>15.4 (12th)</td><td>13.8 (9th)</td></tr>
<tr><td>FTA/FGA</td><td>26.9 (13th)</td><td>0.3 (12th)</td></tr>
<tr><td>3P%</td><td>31.7 (13th)</td><td>32.5 (1st)</td></tr>
<tr><td>2P%</td><td>48.7 (14th)</td><td>50.8 (6th)</td></tr>
<tr><td>FT%</td><td>79.4 (7th)</td><td>82.7 (15th)</td></tr>
<tr><td>3PA/FGA</td><td>38.1 (8th)</td><td>38.7 (10th)</td></tr>
</tbody></table>"""


def test_parse_matches_exact_rotowire_wnba_fields():
    report = parse_scouting_table(HTML)
    assert tuple(report) == FIELDS
    assert report["Pace"]["defense"] is None
    assert report["Efficiency"]["offense"] == {"display": "98.3 (14th)", "value": 98.3, "rank": 14}
    assert report["FTA/FGA"]["defense"] == {"display": "0.3 (12th)", "value": 0.3, "rank": 12}
    with pytest.raises(ValueError):
        parse_scouting_table(HTML.replace("3PA/FGA", "Other"))


def test_each_real_pull_creates_distinct_observations():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE teams(id INTEGER PRIMARY KEY, abbreviation TEXT)")
    conn.executemany("INSERT INTO teams(id, abbreviation) VALUES (?, ?)",
                     [(index, abbreviation) for index, abbreviation in enumerate(TEAM_SLUGS, 1)])
    clock = lambda: datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc)
    first = pull_scouting_reports(conn, fetch_html=lambda _url: HTML, now=clock)
    second = pull_scouting_reports(conn, fetch_html=lambda _url: HTML, now=clock)
    assert first["complete"] and second["complete"]
    assert first["pull_id"] != second["pull_id"]
    assert conn.execute("SELECT COUNT(*) FROM rotowire_scouting_snapshots").fetchone()[0] == 2 * len(TEAM_SLUGS)
    saved = conn.execute("SELECT report_json FROM rotowire_scouting_snapshots LIMIT 1").fetchone()[0]
    assert json.loads(saved)["FTA/FGA"]["defense"]["display"] == "0.3 (12th)"
