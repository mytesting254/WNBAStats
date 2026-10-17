import pytest

from backend.app.covers_import import CoversMetadata, _event_rows


@pytest.mark.parametrize('prefix,side', [('o', 'over'), ('u', 'under')])
def test_current_covers_markup_keeps_single_side_and_comparison_books(prefix, side):
    page = f'''
    <article id="901"><h2>Points Scored</h2>
      <a href="/players/10068/allisha-gray">
        <div class="playerContainer"><span class="category fw-bold">Allisha Gray</span></div>
      </a>
      <table><tbody><tr><td class="oddsCell">
        <span class="oddsSideLineText">{prefix}14.5</span>
        <span class="oddsSideOddsText americanOdds text-smaller">-145</span>
        <img alt="BetMGM logo">
      </td></tr></tbody></table>
      <dialog><div class="compareOddsTableContainer"><table class="compareOddsTable">
        <thead><tr><th>ALL BOOKS</th><th>{side.upper()}</th></tr></thead>
        <tbody><tr><th><img alt="bet365 logo"></th><td class="compareOddsSide">
          <span class="oddsSideLineText">{prefix}15.5</span>
          <span class="oddsSideOddsText americanOdds text-smaller">&#x2B;105</span>
        </td></tr></tbody>
      </table></div></dialog>
    </article>
    '''
    metadata = CoversMetadata('395535', None, '2026-10-17', '2026-10-17T23:00:00Z', 'GS', 'ATL')
    rows = _event_rows(metadata, page, '2026-10-17T12:00:00Z')
    assert {(row[11], row[12], row[13], row[14], row[8]) for row in rows} == {
        ('Allisha Gray', side, 14.5, -145, 'BetMGM'),
        ('Allisha Gray', side, 15.5, 105, 'bet365'),
    }
