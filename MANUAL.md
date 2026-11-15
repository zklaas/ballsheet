# BallSheet

A PyQt6 desk for Major League batter, pitcher and team numbers. Pick a team,
then step through its history one season at a time.

## Install

```
./install.sh            # puts `ballsheet` on ~/.local/bin + a desktop entry
```

Needs Python 3 and PyQt6 (`pip install --user PyQt6`). Everything else is the
standard library. Run with `ballsheet`, or `python3 ballsheet.py`.

## Using it

| Control | What it does |
|---|---|
| **Team** | The dropdown is **the league as it stood in the selected season**, listed alphabetically by that year's names. Step back to 1953 and you get the 16 teams of 1953 — *St. Louis Browns*, *Brooklyn Dodgers*, *Milwaukee Braves*…; step back to 1876 and you get the eight-team National League — *Boston Red Caps*, *Hartford Dark Blues*, and the rest. Your current pick stays selected as its name and league-mates change around it (it's tracked by franchise, not by name). The season range snaps to that franchise's debut, so you can only walk back as far as the club actually existed. |
| **◀ / ▶** | Previous / next season. Also `Ctrl+Left` / `Ctrl+Right`. Hold-friendly: the fetch is debounced. |
| **Season** | Type a year directly. |
| **View** | *Batting* and *Pitching* give one sortable row per player. *Fielding* gives one row **per player per position** (a utility man appears once for each spot he played), sorted by innings; base-running lives in the *Batting* record, but fielding is its own thing. *Team totals* gives the franchise's aggregate batting, pitching and fielding lines for that year. |
| **Refresh** | Re-pull this view, ignoring the cache. |
| Click a column header | Sort. Numeric columns sort numerically. |

### Window and text size

BallSheet opens no bigger than the screen. If its usual size won't fit (on a short
screen like 1280×624), it fills the screen instead, so the bottom of each tab (notes,
status bar, the ends of tables) stays in view. Text is 2 points smaller than the
desktop's (9 pt against Ubuntu Sans 11), and table rows are only as tall as their
text. **View ▸ Smaller text / Larger text** changes the size a point at a time, and
**View ▸ Desktop text size** goes back to the desktop's size. The choice is saved in
`~/.config/ballsheet/settings.json` and takes effect the next time BallSheet starts.

### Exports

* **Export CSV…** — the view currently on screen.
* **Export workbook…** — *every* season the franchise has played, as one `.xlsx`:
  * Batting / Pitching / Fielding view → one sheet per year of per-player rows.
  * Team totals view → a single sheet, one row per year, batting then pitching
    then fielding across the columns, with a **Team** column giving that year's
    name (so a relocation shows up as the name changing partway down). Good for
    charting a franchise over time.
  Cached seasons are instant; the rest are one request each, with a progress
  bar you can cancel.

### Leaders tab

MLB's leaderboards for the statistics the Stats tab shows (no custom stats), next to
Stats. **Yearly** shows one season's leaders, or — with **Best single seasons ever** —
the best seasons from any year; **Career** shows career leaders. Choose the **Group**
(Batting, Pitching, Fielding), **Statistic**, **Position** (fielding only), **Leagues**
and **Era**, and how many players (**Top**, up to 100, MLB's limit). Tied players share a
rank.

* **Leagues** is a drop-down of every league MLB counts as major, by abbreviation (AL, NL;
  AA, UA, PL; FL; the Negro leagues NNL I, ECL, ANL, EWL, NSL, NNL II, NAL), each ticked
  to count or not, with **All**, **AL and NL only** and **None**. A one-year board counts
  only the ticked leagues that played that year.
* **Era**: *All years*, *1901 Onwards* (the 19th-century leagues and the NL's seasons
  before 1901 left out) or *Before 1901* (only those).
* A few statistics are one-year boards only, as MLB's all-time boards for them are wrong
  or missing: extra-base hits, fielding innings and range factor. Fielding percentage
  needs a position.
* **Catchers' caught stealing (CS%)** (Fielding, all positions or C) ranks catchers by
  the runners they threw out per steal tried against them, CS / (SB + CS), with SB, CS and
  attempts (Att) alongside. MLB has no board for it, so it's put together here from MLB's
  fielding records, which have steals against catchers from the 1950s on (so *Before
  1901* is always empty). **At least … attempts** sets the minimum: 40 for a season, 60
  for the best single seasons, 500 for a career by default. Equal percentages share a
  rank, the catcher with more attempts listed first.

Most boards are MLB's own, with its playing-time minimums for the averages and rates.
MLB has no board for an era, or for a career counting only some leagues (its career
boards rank each league's careers separately), so those are put together here: the best
single seasons from MLB's own season lists, and careers added up from each player's
league careers or seasons, with the averages and rates worked out again for careers of
3,000 plate appearances or 1,000 innings. The first such board can take up to a minute;
after that it's cached. On a career board the team is the player's last — within the
leagues and era counted — filled in from his season-by-season record where MLB leaves
it out.

### Finding a player by name

The Personal and Dailies tabs search MLB's player list by name. MLB's search misses some
players it has records for (Turkey Stearnes, Oscar Charleston, "Tris" Speaker), so
BallSheet also looks up the other names **Wikidata** knows a player by — nicknames like
*Pete Alexander*, *Shoeless Joe*, *The Big Train*, *The Splendid Splinter* — from
`wikidata_names.json`; and when nothing has turned up, it searches everyone who has
played in an MLB-recognised major league, given and middle names included (*Grover
Cleveland Alexander*, *Three Finger Brown*). That list takes a few seconds to gather
the first time it's needed.

### Teamstats tab

The Stats tab's views — Batting, Pitching, Fielding, and the Advanced, Sabermetrics
and Expected views of batting and pitching — with **one row per team-season** in place
of one per player. **Teams:** picks which team-seasons:

* **This season, across all teams** — every club in the chosen season (in 1920–48 that
  includes the Negro league clubs, as the Team list does);
* **across the team's league** or **across the team's division** — just the chosen
  team's league or division that season (divisions began in 1969);
* **This team, across all its seasons** — the chosen franchise year by year, with each
  year's name (the 1950 Browns, the 2026 Orioles).

The chosen team's row is in bold. Click a heading to sort; **Export…** saves the table,
in the order on screen, as `.xlsx` or CSV.

Batting, Pitching and Fielding and their Advanced views are MLB's own team figures. MLB
has no team figure for Sabermetrics or Expected, so those are rolled up from the
team's players: WAR, RAR and the other run and count stats added up, and rates
weighted by playing time — plate appearances for batters (wOBA, wRC+, xAVG …), innings
for pitchers' FIP family (so team FIP and xFIP come out exact), batters faced for
pitchers' expected stats. Expected stats begin in 2015. A franchise's whole history is
one request per season the first time (about 5–20 seconds), cached after that.

### Dailies tab

One player's season **game by game** (next to Personal, and laid out the same way).
Find the player by name, or browse a team's roster for a season; then choose one of
his seasons from **Season** on the name line (a roster pick opens on that roster's
season, a name search on his latest). **Include postseason** adds his postseason
games after the regular season, with their own total.

Four sub-tabs, as on Personal — whichever apply to that season (the others say so):

* **Batting** — PA, AB, R, H, 2B, 3B, HR, RBI, BB, IBB, SO, HBP, SH, SF, GIDP, LOB,
  then AVG / OBP / SLG / OPS season to date, and MLB's one-line summary.
* **Running** — times on base (H + BB + HBP), SB, CS, runs, GIDP, then SB / CS /
  SB% season to date.
* **Pitching** — decision (W, L, SV, H, BS), GS, IP, H, R, ER, HR, BB, SO, HBP, WP,
  batters faced, pitches, strikes, then ERA / WHIP season to date.
* **Fielding** — one row per game per position (a game at two spots is two rows):
  GS, innings, chances, PO, A, E, DP, that game's FLD% and the season-to-date FLD%
  at that position.

Each row leads with the date (a doubleheader's second game is marked "(2)", a
postseason game WC / DS / LCS / WS), his team, the opponent ("@" away, "vs" home)
and the result with the score. A starred column (*) is season to date through that
game; the postseason runs its own count. Bold lines are totals — one per position
for fielding.

**Level** (on the same line) switches between the **Player** and his **Team**. At
the Team level the four sub-tabs are the club's own line for each game, for the
**Team** and **Season** in *Or browse a team's roster* (the player's Season box
hides), and the name line gives the regular-season record. The columns are the
player's less the ones that only make sense for one man (his team, position, games
started, MLB's one-line summary); the pitching decision is W or L plus SV / BS.
Choosing a player — by name or from the roster — switches back to Player.

Game logs exist from **1901** on; earlier seasons are listed but say so.

### Denied tab

Right after Standings: the teams that "ought to have won" in the postseason but
didn't, one row per team per season, 1876 to now. Each season names its best teams:

- **MLB** — the best record in the American and National Leagues (and the other
  19th-century majors), every season, with all of the tied clubs if there is a tie.
  It ought to win it all.
- **AL / NL** — from 1969, the best record in each league. It ought to win the
  championship series.
- **Divisions and wild cards** — from 1995, the best in each division (the
  division winner) and the best of each league's non-division-winners (the "wild
  card division"). They ought to get through the wild card and division rounds.

A team beaten by an **underdog** (a club with a worse record) in a round it ought to
have won is **Denied** (shaded red). Losing to a club with an equal or better record
is no upset, so it isn't a denial. The Postseason column says "not an upset: a better
record" (or "an equal record"). The 2025 Cubs, the NL's best wild card, lost their
division series to the Brewers, who had MLB's best record, so they aren't denied.

| Label | When |
|---|---|
| Denied:  No postseason | no postseason that year (or none in its league: the 1884 Union Association) |
| Denied:  Defeated by underdog in World Series | lost the World Series (1903+) |
| Denied:  Defeated by underdog in final series (…) | lost the last series before 1903 (World's Series, Temple Cup), or a Negro league final, named |
| Denied:  Defeated by underdog in championship series | lost the LCS |
| Denied:  Defeated by underdog in division series | lost the division series |
| Denied:  Defeated by underdog in wild card game / series | lost the wild card round (a game 2012–2019, a series from 2020) |
| Denied:  Excluded by split-season format | 1981: best record, but won neither half |

The **Negro leagues** (1920–1948) are judged on their own: the best record across
them, against their official postseason (`negro_postseason.json`), meaning the
Colored/Negro World Series, or a league's championship series in a year without one.
Unofficial series don't count. Untick *Negro leagues* to hide those rows. MLB's
standings include some very short Negro league seasons, so a club like the 1928
Baltimore Black Sox (5–2) can come out "best".

*Why "MLB" and "Negro leagues" are kept apart.* The labels describe baseball as it
was then: the American and National Leagues barred Black players, and segregated
baseball existed until integration. The Negro leagues ran their own pennant races
and their own World Series, so this tab judges their clubs by their own standings
and postseason, not against AL and NL clubs they were kept from playing. No
implication is meant that Negro league statistics are anything less than part of
baseball's history. MLB has recognized the Negro leagues of 1920–1948 as major
leagues since 2020, and their records belong to the history of today's integrated
game. BallSheet includes them wherever MLB's records do.

**Tiebreakers.** Every club that played a tiebreaker (a pennant playoff, or a
division or wild card "Game 163"; 1908–2018, from MLB's schedule) gets a row,
best team or not. The *Tiebreaker (regular-season games)* column says who won, and
what was at stake. The two clubs were level, so nobody "ought to have won" a
tiebreaker, and losing one is never a denial. A club put out by one is shaded
orange, and its Denied cell says *Better record than* instead: the clubs with worse records that went on to
win postseason series that year (1962 Dodgers: the World Series champion Yankees).
Tiebreaker games counted in the regular season, in the W–L here and in the
players' statistics. MLB's standings include them, except for 1946 and 1962, where
the API still shows both clubs level. There they are added back (marked \*), so
the 1962 Giants are 103–62 and alone at the top.

*Seasons … to …* narrows the range. *Got as far as* keeps the teams whose deepest
round was the chosen one: *Final series*, *Championship series*, *Division series*
or *Wild card round* (tick *or further* to include teams that went past it). It also
offers *Won it all* and *Didn't reach the postseason*, which includes tiebreaker
losers. *Denied/Better Record teams only* keeps the denied teams (red) and the clubs put
out by a tiebreaker that had a better record than a later series winner (orange).
*Tiebreaker teams only* narrows the rows further. Untick *19th century (1876–1900)* to
start at 1901, the usual dividing line for "modern" baseball (1900 counts as 19th
century). Click a column
header to sort by it, and click again to reverse (W, L and Pct sort as numbers). The
sort is kept when you change the filters, and sorting by Season puts the original
order back. If the filters leave no rows, a message takes the table's place and
says why. "Won it all" with "Denied/Better Record teams only" can never
match, since a champion can't have been denied or put out by a tiebreaker. The
first visit loads every season's standings and postseason (cached for good after
that), which takes a while the first time.

### Stadiums tab

A club's home park against the Naturalzone tab's uniform park (the Retrosheet location
diagram's, 330 ft down the lines and 405 ft to centre). Pick a season and a team: the
park is drawn over the uniform one, **green** where it has ground the uniform park
hasn't (deeper fences, longer foul strips) and **red** where the uniform park has
ground this one hasn't, with the uniform fence dotted and the park's posted distances
at their marks (green deeper, red shorter). Hovering a spot names its zone, whose ground
it is, and the two fences' distances that way. **Distances** tables each posted mark
against the uniform fence at the same angle; **Usual zones** gives each fielder's usual
ground in both parks, what's gained and lost, and the whole field's.

The dimensions come from two places, and the line under the park's name says which:

* **Wikipedia** first: `stadium_dimensions.json` holds fence distances transcribed by
  hand from the park articles on English Wikipedia, for 82 of the 92 parks MLB names as
  home parks since 1901, era by era wherever an article dates its changes (Braves Field
  year by year, Yankee Stadium, the Polo Grounds, Ebbets Field, Shibe Park, Fenway,
  Kauffman, San Diego, Olympic Stadium, Petco...). Where an article gives only one set
  (the current or final one), that set is used for every season, and the source line
  says so. The source line links the article, and the exact revision transcribed: for
  further sourcing, consult the article and its references. Wikipedia's text is
  licensed CC BY-SA 4.0, and so is the file.
* **MLB's posted distances** otherwise (the Stats API's venue field info): one set per
  park, today's or its last, whatever the season.

A few early parks have neither (Bennett Park, South Side Park, the two Washington
American League Parks, Sportsman's Park 1902-08, Lloyd Street Grounds, Cincinnati's
League Park), nor do the Dodgers' Coliseum years or the Marlins' first park: the tab
says so. The lines are put at 45°, the alleys at 22.5°, other marks where their names
place them (straightaway left or right 33.75°, deep left- or right-centre 15°...), and the
fence between marks is joined straight in angle and distance, as the uniform park's own
fence is, so it's the outline of the posted distances rather than the wall's every corner.

On the Naturalzone tab, **Play them in … (from the Stadiums tab)** fits the natural
zones to the park shown here instead of the uniform one (see below).

### Postseason tab

A season's postseason drawn as a bracket, in the manner of MLB.com's *Postseason
Picture*: American League rounds, National League rounds and the World Series,
with lines showing who advanced to which series. Only the rounds played that year
appear: the World Series alone (1903–1968), then the championship series (from
1969), division series (1981, and from 1995) and wild card round (a single game
2012–2019, a series from 2020).

Before 1903 it shows the series of the day: the **World's Series** between the
National League and American Association champions (1884–1890), the **1892
split-season championship**, the **Temple Cup** (1894–1897) and the **1900
Chronicle-Telegraph Cup**. The MLB Stats API doesn't have these games, so they come
from `early_postseason.json` in the app folder. They were transcribed from Wikipedia's
article on each series. The header shows how each
series was decided (*15-game tour*, *first to 6 wins*…), and a note under the box
gives anything unusual. For example, 1885 and 1890 ended level at 3–3–1, and
1887 and 1888 went on after the winner had clinched. The other years before 1903
say there was no postseason series.

* Each series is a box with a row per club and a column per game. **✓ 6** = won,
  scoring 6; **✗ 3** = lost, scoring 3; **– 6** = a tied game (1907, 1912, 1922).
  The number after a club's name is its games won in the series; the winner is bold.
* A season under way shows the matchups known so far. Undecided slots show as
  *NYY/BOS* or *AL Higher Seed*, and games still to come show their date
  (*italic* = only if necessary). **Refresh** re-pulls the data.
* **Negro leagues**: below the MLB bracket, any Negro league postseason that year
  (1903–1949). League playoffs and the Colored World Series (1924–1927) or Negro
  World Series (1942–1948) form a bracket, with the Negro National League on the left.
  One-off series, such as championship challenges and disputed or abandoned series,
  appear on their own. MLB has recognized the Negro leagues of 1920–1948 as major
  leagues since 2020, and a series outside that is marked **unofficial** in orange,
  with the reason under it: before 1920 or after 1948, between independent clubs, or
  disputed or abandoned. Games shown with runs in brackets, like the two 1942 World Series games,
  were played but not counted. The games come from Retrosheet, and titles, results and status
  from Wikipedia's list of Negro league postseason games. `make_negro_postseason.py`
  rebuilds `negro_postseason.json` from Retrosheet's files. *The information used here
  was obtained free of charge from and is copyrighted by Retrosheet. Interested parties
  may contact Retrosheet at "www.retrosheet.org".*
* Hover over a game for its date, the full score, the ballpark and any note. A * marks
  a game with a note: shortened, forfeited or called.
* **Layout**: *Side by side* (as on MLB.com) or *Stacked* (AL above NL, World
  Series at the right: half the width). *Auto* picks side by side when it fits the
  window at a readable size. **Fit to window** shrinks the bracket instead of scrolling.
* 1904 and 1994 had no postseason.
* **Tiebreakers (regular season)**: under the MLB bracket, each tiebreaker of the
  season gets a box of its own, headed by what it decided (*NL pennant tiebreaker*,
  *AL East tiebreaker*, *NL wild card tiebreaker*), from the 1908 Cubs–Giants game
  through the 2018 Game 163s. These were played to break a tie before the
  postseason, and their games count in the regular-season standings and statistics,
  not the postseason's; the section says so. The National League's pennant playoffs
  of 1946–1962 were best of three, the rest single games.
* Clubs appear under their
  names of the time (*Boston Americans*, *California Angels*), without logos.

### Poststats tab

Player statistics for the **postseason only**, laid out like the Stats tab. Pick a
**Team** (or *All teams*, which adds a Team column), a **Season** (1884 on) and a
**View**: *Batting*, *Pitching*, *Fielding* (one row per player per position) or *Team
totals* (a club's lines; for *All teams*, one row per club). Tick the kinds of series to
include:

* **American and National Leagues** (1903 on):
  * **World Series**.
  * **Championship series**: 1969 on.
  * **Division series**: 1981, and 1995 on.
  * **Wild card**: 2012 on.
  * **Tiebreakers**: games and playoffs for a first place (or, from 1995, a wild card
    spot) left tied at the end of the season. Before 1969 they decided the pennant
    (1946, 1948, 1951, 1959, 1962), from 1969 to 1994 a division (1978, 1980), and from
    1995 they were one-game "Game 163"s. Traditionally their statistics count with the
    regular season, so leave the box unticked for conventional postseason figures.
    In a season that had one, the line above the table says so: that its games are
    left out, or, with the box ticked, that they're included even though they count
    in the regular-season statistics.
* **Other series, grouped by the leagues involved**: *NL v AA World's Series*
  (1884–1890), *NL postseason series* (1892, the Temple Cup, the Chronicle-Telegraph
  Cup), and for the Negro leagues *NNL v ECL* / *NNL v NAL World Series*, *NNL / NAL
  / NSL postseason series*, *NNL v independent clubs* and *Independent clubs*. A group
  whose series are all unofficial says so and starts unticked, like *Tiebreakers*, so
  the default figures are the official ones. Hover over a box for the series it takes in.

A kind a season didn't have isn't shown. A kind with no statistics is greyed out and
the line under the boxes says why. For example, no box scores exist for any postseason
series before 1900, so 1884–1897 have none.

The figures are summed from box scores, and rates (AVG, OBP, SLG, OPS, ERA, WHIP, K/9,
BB/9, FLD%) are worked out from the sums:

* **American and National Leagues:** MLB's box scores, fetched once per game and cached.
  They match the Stats API's own round-by-round splits. MLB's box scores don't give
  fielders' innings or double plays, and a fielder's game counts at the position he
  started.
* **Negro leagues and the 1900 Cup:** Retrosheet's box scores, bundled with the app by
  `make_negro_postseason.py`. Only games that counted are included, so the two
  uncounted 1942 World Series games are left out. Pitching decisions are Retrosheet's,
  which don't always agree with other sources. For example, Satchel Paige gets three
  1942 wins where Wikipedia gives one.
* **Blank cells:** a blank means the source doesn't record that figure, and the line
  above the table says which ones.

### Gameday tab

One game's play-by-play from MLB's game feed, the same feed MLB's own Gameday uses.
Pick a **Team** and a **Season**. The **Game** list then shows that team's regular-season
and postseason games (for example *Oct 25   WS 1   @ Los Angeles Dodgers   L 3–6*), and
the ◀ ▶ buttons step through them. The tab opens on the latest game played.

* **Header:** the score, the date, the round (in the postseason), the ballpark and the
  winning, losing and saving pitchers, with the line score (runs by inning, plus R, H
  and E) underneath.
* **Diamond (top right):** who is on which base, and the outs. It follows the row you
  pick: after a play, after a grey row such as a stolen base, or as a half-inning
  begins (empty, apart from extra innings' automatic runner on 2nd). Under the "after"
  line, in bold, are the runs scored on that play or event (a grey row counts only its
  own, such as a run on a wild pitch), followed by the score after it. On a home run, the hitter is named in bold beside
  home plate. Hover for the
  runners' full names. In a game in progress with no play picked, it shows the game
  right now, with the count. Click the diamond to go back to that.
* **Plays:** every plate appearance, grouped by half-inning. Each half-inning's row
  gives its runs, hits, errors and runners left on base. Scoring plays are shown in
  bold. Substitutions, stolen bases, balks and similar events appear as grey rows just
  before the plate appearance they happened in. Tick **Scoring plays only** to hide the
  rest.
* **Detail pane:** pick a play to see who batted and pitched (with handedness), the
  batted ball, where each runner went, and the pitches. The pitch table gives the count
  after each pitch, the result (balls green, strikes red, balls in play blue), and, where
  the feed has them, the pitch type, speed and spin, plus:
  * **Vertical break (induced):** inches the spin moves the pitch up or down compared with
    a spinless ball, with gravity left out. A four-seamer's backspin holds it up (about
    15–18 in "up"); a curveball's topspin pulls it "down".
  * **Horizontal break:** inches of sideways movement from spin, to the pitcher's *arm
    side* (a right-hander's fastball, sinker, changeup) or *glove side* (sliders,
    sweepers).
  * **Location:** where the pitch crossed the plate in the batter's terms: up or down,
    and in (toward him) or away, from the side he batted on that time. "Out:" means
    outside the strike zone. Hover for MLB's Gameday zone number.

  Columns the game has nothing for are hidden. Hover over a column heading for its
  explanation.
* **Refresh:** re-pulls the schedule and the game on screen, ignoring the cache. A game
  in progress updates itself every 20 seconds while the tab is on screen.
* **Export…:** saves an `.xlsx` workbook (Plays, Pitches and Line score sheets), or the
  plays alone as CSV.

How much a game holds depends on its era, measured on sampled games:

| Era | What the feed has |
|---|---|
| before about 1950 | no plays: only the line score and the decisions |
| about 1950 to the late 1980s | one line per plate appearance; the ball-strike sequence for only some plate appearances (the others show no count and say so) |
| late 1980s to 2007 | the full ball-strike sequence, without pitch tracking |
| 2008 to 2014 | each pitch's speed and location (PITCHf/x) |
| 2015 on | also spin, break, exit velocity, launch angle and distance (Statcast) |

A finished game's feed is about 0.5–0.8 MB. It's fetched once and then cached for good.

### Boxscore tab

MLB's box score of the game picked on the Gameday tab. It comes in the same feed, so
there is nothing extra to download, and it follows Gameday as you change games.

* Each team's batters (substitutes indented under the player they replaced, with
  positions such as *PH-LF*) and pitchers (with *W*, *L*, *S*, *H* and *BS* notes), with
  a totals row. AVG, OPS and ERA are the player's season to date, as MLB prints them.
* Under each team are MLB's batting, baserunning and fielding notes (2B, HR, RBI,
  runners left in scoring position, errors, double plays). At the bottom are the game
  notes: pitches-strikes, batters faced, umpires, weather, attendance.
* **A game in progress** shows its box score so far, marked *In progress*, with ▶ next
  to the batter up and the pitcher in the game. It updates with Gameday every 20 seconds
  while the tab is on screen. A game not started shows the lineups once MLB posts them.
* Box scores go back to 1901. Before play-by-play (about 1950), MLB has the batting and
  pitching lines but no left-on-base or pitch counts, so those columns are left out.

### WPA tab

Win Probability Added for the game picked on the Gameday tab. Win probability is each
team's chance of winning given the score, inning, outs and runners on base. WPA is how
much one play moved it.

* The figures are **MLB's own**, from its `/game/{pk}/winProbability` feed: the same
  numbers MLB's Gameday shows, one per plate appearance. BallSheet does not work them
  out itself. A stolen base or wild pitch is counted in the plate appearance it happened
  during.
* **Chart:** the home team's chance of winning through the game, with 50% across the
  middle. The area above 50% is in the home team's colour (they are favoured), and the
  area below is in the visitors' colour. Each inning is split into top and bottom halves
  of equal width. The three biggest swings are ringed. Hover over the chart for a play's
  batter, pitcher, WPA, the chances after it, the score and the leverage index. Click a
  play to open it on the Gameday tab.
* **A game in progress** is drawn as far as it has got. The innings still to be played
  are greyed out, and a dot marks where the game stands. The plate appearance under way
  is added once it ends. The chart updates with Gameday every 20 seconds while the tab
  is on screen.
* **Every play, biggest swings first:** WPA from the batting team's side (+ helped it),
  and the leading team's chance after the play. Click a heading to re-sort (Inning
  sorts in game order). Double-click a play to open it on Gameday.
* **Players:** each player's total. A batter gets the WPA of his plate appearances, and
  a pitcher gets his team's side of the plate appearances he pitched to, so a strikeout
  is + for the pitcher and − for the batter.
* MLB has win probability only for games with play-by-play, from about 1950. Older games
  say so.

### Statcast tab

The game picked on the Gameday tab in 3D, one plate appearance at a time. A pitcher and a
batter stand on a regulation field, 60 ft 6 in apart, with the catcher and umpire behind
the plate. Each pitch flies from the pitcher's hand to the plate against the batter's
strike zone, and a ball in play flies off the bat.

* **At-bat:** the game's plate appearances, as "T3 · batter vs pitcher · result". ◀ ▶
  (or ↑ ↓) step through them, and each one opens on its last pitch, the one that ended it.
  **Balls in play only** lists just the plate appearances that ended with the ball in play.
  **Show on Gameday** opens the play there. The two tabs move together: picking a play
  on Gameday shows it here, and stepping here moves Gameday's selection along.
* **Pitch:** one button per pitch, numbered, with its type code (FF, SL, CH…) and
  coloured by result (green ball, red strike, blue in play). Hover over a button for the
  count, speed and call. ◀ ▶ (or ← →) step pitch by pitch, and carry on into the next (or
  previous) plate appearance at either end. **Earlier pitches** keeps the at-bat's earlier
  pitches in view, faded. **Play** (or Space) flies the pitch, then the batted ball, at
  real speed or slowed down.
* **The view:** drag to rotate, right-drag (or Shift+drag) to pan, and use the wheel to
  zoom. Double-click goes back to the camera picked. The cameras are **Broadcast** (TV's
  center-field camera, far out on a long lens), **Catcher** (the umpire's view),
  **Side** (from the first-base side, the pitch's whole flight), **Overhead** and
  **Field** (the whole field, for the batted ball). The camera stays put as you step,
  so every pitch can be seen from one angle. The inset is the zone from the catcher's
  view, with every pitch shown so far.
* **Pitches** are drawn in Baseball Savant's colours. The flight is MLB's own tracking
  (PITCHf/x from 2008, Statcast from 2015): a constant-acceleration fit of each pitch,
  run from the release point (the pitcher's extension in front of the rubber) to the
  plate. The pitcher's throwing hand is at the release point, so a sidearmer looks like
  one. The zone is the batter's own top and bottom.
* **Batted balls** (2015 on) are partly modelled. MLB gives exit velocity, launch angle,
  distance and where the ball was fielded, but not the flight. A ball in the air flies
  with drag, tuned so it comes down at MLB's distance. A ground ball hops and rolls out
  to where it was fielded: MLB's distance for a grounder is only to its first bounce.
  The hang time in the panel is the model's. The ground under each flight carries its
  shadow.
* **The figures** are silhouettes drawn to the players' listed heights. The batter
  stands in the box on his side (a right-handed batter on the third-base side), in his
  stance for a pitch taken and swinging through the zone for one swung at.
* **The park** is the one the game was played in, laid out as on the Stadiums tab:
  Wikipedia's distances for that season where `stadium_dimensions.json` has them, else
  MLB's posted distances for the venue, else a generic 330–400 ft fence. The line under
  the title names the park and the source, linking the Wikipedia article (and the
  revision transcribed) for further sourcing. The fence is drawn 8 ft high except where
  the park's Wikipedia articles give a section's height. So far that's Fenway's left field,
  from the foul pole to the flagpole (379 ft): 25 ft through 1933 (with Duffy's Cliff, a
  10-ft incline, in front of it) and the Green Monster's 37 ft 2 in from 1934, from the
  Fenway Park and Green Monster articles. The Los Angeles Coliseum's 42-ft left-field screen
  (1958–1961) is drawn from the left-field line to where the left fielder's zone ends (16.55°
  from center, the middle of the left-center zone, as on the Naturalzone tab). Wikipedia
  gives its height; its extent, over all of left field, is an approximation from other sources.
* **Select alternate stadium** (at the right end of the View row), just for fun, puts the game in another park:
  search every park era Wikipedia gives (by name, year or a word of its note — "polo",
  "1927", "Monster") and MLB's other parks with posted distances, and pick one. The line
  under the title then says it's **a user-selected stadium**, in place of the park the game
  was played in, with its source as usual; balls off the wall and home runs are judged
  against its fence. The choice stays as you move from game to game until **Back to the
  game's park** (greyed out while the game's own park is on show).
* **Balls off the wall.** A hit that stayed in the park (a single, double or triple)
  can't have gone through the fence, so its flight stops where it met the fence and
  drops back. Where it met the fence higher than the fence there (8 ft, or a known
  section's height), the wall must be taller, so a section of wall that tall is drawn there, outlined in
  yellow and tagged "≈ 16 ft (model estimate)", for that play only; the panel beside the
  view spells it out ("Model estimates double probably hit the wall at the 16 ft mark"). The other way round, a
  home run that crossed the fence line lower than the fence there must have cleared a
  lower fence, so for that play the fence there gives way to a section half a foot or more under where
  the ball crossed, tagged the same way ("Model estimates home run cleared the fence near
  the 4 ft mark" in the panel). And where a home run's distance falls short of the drawn fence,
  the fence data is off there: MLB's distance for a home run is where it would have come
  down at field level, so the fence must be nearer than that. For that play the fence is
  pulled in to the last point where the modelled flight still clears it, tagged
  "fence < 382 ft (MLB distance)", with the panel saying which part is measured (MLB's
  distance) and which estimated (where it's drawn). About 2% of home runs do this, mostly
  where a park's marks are sparse (Yankee Stadium's short porch, Oracle Park's left-centre). Likewise, a
  ball that stayed in the park without being a hit off the wall (a fly out, a lineout, an error,
  a fielder's choice) but came down, or was caught, beyond the drawn fence shows the fence there
  must be farther. The drawn fence runs straight between a park's posted distances, which cuts
  inside a curved wall. For that play the fence is pushed back to 5 ft beyond where the ball was
  caught (MLB's spray point) or came down, with grass out to it, tagged "fence > 403 ft (stayed
  in the park)". About 1 in 60 balls in play that weren't hits does this, almost all deep warning-track catches
  (Tropicana Field, 2026-10-13: Travis Bazzana's 401 ft flyout to right-center, caught about
  403 ft out where the drawn fence is 389 ft). A catch above the fence (a home run robbed)
  isn't a taller wall. The ball's height at the fence is the modelled flight's, so these
  heights are estimates.
* **The panel** beside the view has the pitch's result, speed (out of the hand and at
  the plate), spin, break, location, release height and side, extension, flight time,
  where it crossed the plate, and the batter's zone. For a ball in play it also has
  exit velocity, launch angle, distance, type, direction (pulled or opposite field) and
  hang time.
* Games before 2008 have no pitch tracking: the view says it can't show action, but
  the game's park is still drawn to look round (drag, zoom, or the Field view). From 2008
  to 2014 the pitches fly but a batted ball doesn't (batted-ball tracking began in 2015).
  A game in progress updates with
  Gameday every 20 seconds. Watching its latest pitch, the view moves on to the next
  one as it is thrown.

### Players Batted In tab

The Production tab's companion: whom one batter drove in over a season, most often first.
The **Batter** list starts with the Production tab's nine, in batting order, then the rest
of the team chosen there (most plate appearances first). Each runner's line counts the runs
the batter was credited with driving in (RBI) when that runner scored, split by where the
runner started the play: **From 3rd**, **From 2nd**, **From 1st**, and the batter himself
on his home runs (shown greyed). **Share** is each runner's part of the batter's RBI.

A run that scored on an error, a double play or a wild pitch is no one's RBI and isn't
counted; a pinch runner who scored is the one listed. Regular season only. It reads the same
Retrosheet play-by-play as the Speed and Safe/Out tabs (a season fetched before this tab
existed is downloaded once more); a season Retrosheet hasn't published yet, such as the
current one, can't be shown.

### Bullpen Support tab

The Prevention tab's companion: how its roles work together, for the team and season chosen
there (a line at the top says which). The Prevention tab's **Rotation** is every pitcher who started at
least as often as the team's starters on average, or made at least 40% as many starts as its
busiest starter, or started at least twice as often as he relieved with at least 30% of the
busiest starter's starts — so a starter sharing a turn (Jim Hardin and Tom Phoebus, 1970
Orioles) or filling in all year counts, and a call-up with a few starts doesn't. Closers,
Long Relief and Other are as before, except that a pitcher who never pitched in relief is
never Long Relief: a call-up who only started is Other. Five pages:

* **Rotation** — each starter's starts and innings per start; his own record in them (the
  official scorer's decisions); the starts he left without a decision, split into those the
  team went on to win and to lose; and the team's record in his starts — his wins plus the
  no-decisions won, his losses plus the no-decisions lost. Under the starters, a **Rotation** row adds them up,
  **Other starters** does the same for any starts by pitchers outside the Rotation (hover it
  for their names), and **Total** covers every game. Each of these rows can be clicked too, to
  see the bullpen across those games.
  Clicking a starter (the first is chosen to begin with) or one of these rows chooses whose
  bullpen the next page shows.
* **Bullpen/Starter support** — the bullpen for the starter chosen on the Rotation page (to see
  another starter's, choose him there): every pitcher who followed
  him in relief, with his Prevention role (Long Relief, Closers, Other), most games first: how often one came in **right after him** or **later in the
  game**, his innings in those games, his holds, saves (current rule and MLB's official) and
  blown saves in them, his own record
  in them, his other games split into team won and team lost, and the team's record in
  all of them. Those **No decision, no hold/save** columns count only the games where he
  had neither a decision nor a hold, blown hold, save or blown save. A hold or save
  situation is already counted in its own columns, so it isn't counted again. They are
  the games he pitched in with nothing riding on him by the scoring rules: mostly mop-up
  innings, or innings with a lead too big for a save. With every reliever listed, their
  decisions add up to the starter's no-decisions.
* **Long Relief**, **Closers** and **Other** — each pitcher's starts (**GS**), then his relief appearances and innings
  per appearance; how many began in a **save situation**, his innings in those; the holds and
  the saves that came out of them, and the two together as a share (**Converted**); his blown
  holds and blown saves; and his holds and saves in all, with MLB's official saves beside them.
  Then the record of the games he pitched in relief, all season: his own **W–L**, his
  **No decision, no hold/save** games split into team won and team lost, the **Team W–L when
  he relieved** (every one of those games, his hold and save games included), and the
  **Differential** (team W–L % minus his). These are the same figures the bullpen shows on
  Bullpen/Starter support with the Rotation page's **Total** row chosen. Bullpen/Starter
  support shows them for one starter's games, and these pages show them for every game. In
  a role's total row, a game counts once for each of the role's pitchers who pitched in it.

A save situation is MLB's: the reliever comes in with a lead of three runs or fewer and the
chance to pitch at least an inning, or with the tying run on base, at bat or on deck, or he
pitches three innings or more with the lead. Holds, saves and blown saves are MLB's official
ones, game by game. MLB's records have holds and blown saves only from 1999 (saves always);
for earlier seasons they're counted from the play-by-play by MLB's definitions, applied
retroactively as MLB applies saves: a hold goes to a reliever who comes in to a save
situation, records an out and leaves never having given up the lead, without finishing the
game or getting the save or the win; a blown save to one in a save situation who lets the
tying run score.

Saves are counted by the **current rule** too. Today's rule 9.19 dates from 1975; saves
credited from 1969 to 1974 (and MLB's retroactive saves for earlier years) followed looser
rules — 1969–72, any reliever who finished a win with the lead got one, whatever the score.
So before 1975 saves are counted from the play-by-play by today's rule: he finishes a win,
isn't the winning pitcher, records an out, never gives up the lead, and either came in up by
three or fewer and pitched an inning, or with the tying run on base, at bat or on deck, or
pitched three innings. (From 1975 on this count agrees with the official saves.) The
**Saves (all, current rule)** column says so. Beside it, **MLB official saves (rule of the
time)** gives MLB's own figure for comparison (from 1975, when the two are the same, there is
just one column, **MLB official saves (current rule)**): by the rule in force each season, and before
1969 — when there was no save statistic at all — MLB's retroactive saves, worked out later by
researchers using the 1969 rule (so for those seasons, "rule of the time" means the 1969 rule).

**Blown holds** are a custom stat of this tab, not MLB's (MLB charges any reliever who loses
the lead in a save situation a blown save). Of a pitcher's blown saves, one is a blown hold
when he handed the game on to another reliever (didn't finish it) and his team later took the
lead back. Every other blown save — the lead never won back, or lost by the pitcher who
finished the game — stays a blown save, in the column **Blown saves excluding blown holds**.
The two columns together are MLB's blown saves, shown beside them as **MLB official blown saves**. MLB doesn't publish save situations, so they
come from Retrosheet's game records (the score, runners, inning and outs when each reliever
came in). Regular season only; a season Retrosheet hasn't published yet can't be shown.

### Head-To-Head tab

A batter against a pitcher, each picked by team, season and name. For AVG, OBP and SLG
the **prediction** is (the batter's own number + the pitcher's "against" number) ÷ 2.
Beside it, **Actual, \<season\>** and **Actual, career** are what the batter really
did against that pitcher — in the batter's chosen season, and over every season they
met — and a line above the table says how often they faced each other and what
happened (AB, H, 2B, 3B, HR, BB, HBP, SO). Regular season only; the source has these
from the play-by-play era, about 1950, on. If you pick the pitcher from another season,
the batter's season is the one used for the actual figures, and the line says so.

### Safe/Out tab

The Speed tab's companion at the team level: for every club in a season, how often per
inning its players got **on a base (reached 1st)**, **reached 2nd**, **reached 3rd** and **scored**,
against the 3 outs every inning costs, and three comparisons: on a base to outs, scored
to outs, and scored to on a base (the share of baserunners who scored), each followed by
its **% of MLB** (the club's ratio ÷ MLB's that season, one decimal): offensively green at
or above 100%, defensively (what a club allowed) green at or below it. Click a column
heading to sort by it; MLB's line is always last.

Two tables, on the sub-tabs **Offensive** and **Defensive** (each scrolls sideways for its
later columns), with clubs shown by abbreviation (hover for the full name), and
**About these figures…** opens the explanation in its own window. **Offensive Safe/Out** is what each club's batters and runners
did. **Defensive Safe/Out** is what each club allowed the teams it played: the same
counts for its opponents, credited to the club in the field (its innings are innings in
the field, and its scored are runs allowed). The offensive table lists the most runs per
inning first; the defensive one the fewest allowed. MLB's line is the same in both.

It reads the same Retrosheet play-by-play as the Speed tab. A base counts however the
player got there: hit, walk, hit by pitch, error, fielder's choice or force play (the
batter safe at first on a force has reached first; the runner forced out at second
hasn't reached second). Each trip round the bases counts once at every base up to the
furthest it got, so a double counts at first and second, and a home run at every base.
The extra-innings runner put on second (2020 on) is counted only from third on.
Innings are counted as pitchers' are, outs ÷ 3, so outs per inning is always 3.00 (a
walk-off or rain-shortened inning counts as part of one). Regular season only. Where a
season's play-by-play doesn't account for every run on record, the tab says so.

The Speed tab keeps only a per-runner summary of each season, so the first time Safe/Out
shows a season the Speed tab already had, it downloads the play-by-play once more;
from then on a season's download keeps both summaries.

Every tab that reads Retrosheet finds a club there the same way: by its wins, losses and
runs, which agree with MLB's for the AL and NL, or failing that, by the players the two
rosters share. The Negro Leagues need the second: Retrosheet counts more of their games
than MLB's records do (the 1937 Homestead Grays were 51–15 there, 36–13 in MLB's), and
its play-by-play covers only part of them. A club Retrosheet hasn't got (the 1930 Negro
National League, for one) is named in the message that says so.

Where Retrosheet's play-by-play has less than 98% of a club's innings in MLB's records
(outs made in the field against MLB's innings pitched — every AL and NL club has all of
them, the Negro Leagues clubs a fifth to four-fifths), its figures for that club are a
sample of the season, not the whole of it, and are marked with **\***, with a footnote
giving the share. That goes for Safe/Out (each such club's line), the Speed, Bullpen
Support and Players Batted In tabs, the Custom tab (a stat whose value comes out
differently without its Retrosheet figures), and Player Scores — the Speed Score, an
Overall that counts it, and a Career average that takes in any such season.

### Simulator tab

Plays games between two team-seasons, any two, from any eras: a **Team 1** and a **Team 2**
team and season, how many **Games** (100 to start; a second or two per thousand), **DH** and **Fatigue** (below) and a **Seed** (the same
seed and teams play the same games again). **Play** runs them. The seasons start on the
last completed one, because the runners move as Retrosheet's play-by-play says, and
Retrosheet publishes a season after it ends. The current season plays on the play-by-play
the **Uptodate** tab has made from MLB's games so far, if you've made it (the Result page
says so); otherwise on the previous season's tables, and the Result page says that.

The two teams take turns at home, Team 2 first, so each hosts half the games; with an odd
number of games Team 2 has the extra home game. Each game is played in its home team's
environment: that team's league rates that season and Retrosheet's play tables for it.
A cross-era matchup (Red Sox 1919 against Royals 2015) is played half the time in each
era's game.

**Format.** *Single games* (above), or *best-of-7 series (2-3-2)* or *best-of-5 (2-2-1)*,
when **Games** becomes **Series**: how many series to play. The club with the better real
record (its pitchers' wins and losses) has home field, as the higher seed: games 1, 2, 6 and
7 of a best-of-7 at its park, 1, 2 and 5 of a best-of-5. Each club's four busiest starters
by real starts (or your rotation, in your order) start the games in turn from game 1.
Nobody takes a day off, and tired relievers carry over from game to game within a series,
resting only on a travel day (whenever the series changes parks) and before each series. A
tied game is replayed. The Result page gives each side's share of series won and how many
games they took (won in 4, 5, 6 or 7), then the usual tables over all the games played. In
the **Denied** tab's terms the better record ought to win, so the page gives how often the
underdog put it out, with a link that opens that tab at those seasons; and if the two
team-seasons really met in that postseason, the real series. The 2015 Royals (home field)
against the Blue Jays: Toronto wins 58% of 400 best-of-7 series (really, Kansas City won
the ALCS 4–2). The path analysis plays the same number of games as single games, since a
series' length changes with the part taken away.

**Home parks.** Each game is also played in its home team's park. A park's factor for each
result (strikeout, walk, hit by pitch, single, double, triple, home run, ground out, air
out) is how often it happened in the club's home games (both teams batting) against its
road games that season, from Retrosheet, with 3,000 neutral plate appearances added so a
season's luck counts for less. In a game there the league's rates are multiplied by the
park's factors. A player's own line was made half in his home park, so half of it is taken
out first: a Coors Field hitter isn't credited for Coors twice. In 2015, games at Coors Field
averaged 10.8 runs and at Petco Park 8.2. Where Retrosheet has no play-by-play for the
season, the park is neutral and the notes say so.

**Home-field edge.** Beyond the park, home batters do better than visitors in the same
games: in 2015 they walked 8.5% more often and struck out 4.6% less (1975: 8.2% and 6.0%).
Retrosheet's play-by-play of the season gives each result's rate for the home and for the
visiting batters against all plate appearances, each pulled toward the overall rate with
20,000 plate appearances. In a game the environment is multiplied by the batting side's
figures. A park's own factors compare its home games with the club's road games, both teams
batting, so the edge isn't counted twice; and a player's line is half home, half road, so
his own needs nothing taken out. Two league-average 2015 AL teams: home teams win .538 of
10,000 games (MLB's real home teams .540 in 2015; .504 without the edge), scoring
unchanged. Card Battle includes it for the side batting in its own park.

**Platoon (left/right).** Batters do better against pitchers throwing from the other
side. Retrosheet's play-by-play of the season gives each result's rate in each matchup
of hands (left-handed batter against left-hander, left against right, and so on) against
all plate appearances: in 2015 an average left-handed batter got on base 31.5% of the
time against left-handers and 34.3% against right-handers, an average right-handed
batter 33.2% and 31.2%. In a game the environment is multiplied by its matchup's
ratio. A player's own line already has his matchups in it, so it's first divided by the
ones he had. MLB's splits (from 1950) then move each player toward his own record
against each hand, pulled toward his whole season as if each split had 2,200 more plate
appearances (right-handed and switch hitters), 1,000 (left-handed) or 1,000 batters faced
(pitchers): most of a split is luck (after *The Book*, by Tango, Lichtman and Dolphin). A
switch hitter bats away from the pitcher; a switch pitcher throws with the batter's hand.
Hands come from MLB's player records. Before 1950 only the league's effect plays. Where
Retrosheet has no play-by-play for the season (the current one uses last season's), there
is no platoon effect and the notes say so. The first Play for a season after this change
gets Retrosheet's play-by-play again, once.

**Intentional walks and sacrifice bunts** are decided before the pitch, by the situation.
Retrosheet's play-by-play gives how often each happened in every base-out state, stage of
the game (innings 1–6, 7–8, 9 on) and margin (behind by 3 or more, by 1–2, tied, ahead by
1–2, by 3 or more), pulled toward the base-out state's rate with 300 plate appearances.
That table, scaled to the home league's own rate, is tilted in odds by the batter's
own intentional walks or sacrifices per plate appearance against his league's (with 300
or 200 league-average plate appearances added). In 2015 a batter with a runner on
second and two out was walked intentionally 4.9% of the time, and with a runner on first
and nobody out he sacrificed 4.8% of the time. A sacrifice needs a runner on first or
second and fewer than two out, and moves the runners as that season's sacrifices did
(nine in ten: batter out, runner to second). Pitchers at bat sacrifice as their club's
did (or, in a season when none batted, as MLB's did 2012–2019: about 17 times the
league's odds). Both are taken out of every player's line, and out of the play tables,
first: the plate appearances played out by log5 are the rest. A failed sacrifice or a
bunt for a hit stays in the batter's line as an out or a hit. MLB has intentional walks
from 1955; before that they stay in the walks and the notes say so. The Team pages give
each batter's intentional walks and sacrifices per 162 games beside his real season's,
his card the factors under **Managing**, and the play by play the chance it had. The
first Play for a season after this change gets Retrosheet's play-by-play again, once.

**Each side is set up the way the other tabs would by default:**

* **Batting order**: the Production tab's: whoever batted from each place in the most games.
  Batting-order records start in 1950; before that, the nine with the most plate
  appearances, most first. In a league without the DH that season (AL before 1973; NL
  before 2022, except 2020), the pitcher bats where the club's pitchers batted most, and
  whoever is pitching bats there, unless he's pinch-hit for: a reliever always is, and the
  starter from the 7th inning (he then leaves the game), by the bench's busiest hitter not
  yet used. In the real no-DH game about half the pitcher's spot's plate appearances went
  to pinch hitters (49% in the 2015 NL); the Simulator gives the pitchers 59% (2015) and 60%
  (1975, real 64%). Bench cards say who is next in line to pinch-hit.
* **The DH**: by default each team plays as its league did that season, so a team whose
  pitchers batted can meet one with a designated hitter. **DH** above **Play** can set one
  rule for both instead. With pitchers batting, a DH club's DH sits, the batters below him
  move up and the pitcher bats where the club's pitchers batted (ninth, for a modern AL
  club); its pitchers bat like MLB's pitchers did that season, or, in a season when none
  batted, like MLB's pitchers of 2012–2019 against their league (strikeouts ×1.88, home runs
  ×0.15 and so on). With the DH, a club whose pitchers batted puts the bench's busiest
  hitter in the pitcher's place. Your own lineups are kept for each rule separately.
* **Pitching staff**: the Prevention tab's roles (Rotation, Long Relief, Closers, Other).
* **Defense**: the Range tab's: whoever played each position the most innings.
* **A real lineup card.** The two tabs can disagree: the Production tab's order may have
  a batter who isn't the Range tab's usual fielder anywhere (and so leave a fielder out of
  the order). In a DH season the first such batter is the DH; any other is "bats only",
  and **Play** won't play until it's resolved. It opens that team's lineup dialog
  (below) saying who bats but doesn't field, with a suggested fix filled in: each goes to
  an open position he played most that season, and the DH stays the DH unless a position
  he played is still open. Accept it, change it, or put another player in that place and
  give him a position. Cancel leaves the games unplayed.
* **Two-way players.** A player who pitched counts as a pitcher only if he faced more
  batters than he came to bat *and* played fewer than 20 games without pitching. Anyone
  else who pitched (Babe Ruth 1918–19, Shohei Ohtani, a position player who pitched a
  blowout) is a batter in the order who is also on the staff, and never relieves while
  he's in the order. On his starts he keeps his own place in the order:
  * with the DH from 2022 (the "Ohtani rule"), he stays the DH after he leaves the mound;
  * with the DH before 2022, the club gives up the DH that day, so whoever relieves him
    bats in his place;
  * with no DH, his position goes to the bench's best fielder there (or its busiest
    hitter), who bats in the pitcher's spot; when he leaves the mound he goes back to his
    position and the reliever bats in the pitcher's spot. Ruth 1919: bats third and
    plays left; on his starts Del Gainer plays left and bats ninth.
* **Running**: the Speed tab's Retrosheet counts per batter, plus its Speedy, Smart or
  Slow label.

**How a plate appearance is decided.** Each batter's season becomes his share of nine
results: strikeout, walk, hit by pitch, single, double, triple, home run, ground-ball out,
air out. Each pitcher gets the same from the batters he faced. Every line is pulled toward
its league as if it had 50 more plate appearances at the league average. The batter and
pitcher are then combined by the **odds-ratio (log5) method**: each result's chance is
proportional to (batter's rate ÷ his league's) × (pitcher's rate ÷ his league's) × the
environment's rate, and the nine are scaled to add to 100%. The environment is the home
team's league that season.

Fielding comes next, ball by ball. Each ball in play goes where that kind of ball went that
season, by Retrosheet's hit locations: one fielder's ground or a hole or gap two share (where
no location was recorded, the fielder who picked it up). Only the fielders whose ground it is
count on it. Pitchers' lines already include their club's fielders all season, so each
fielder's range is taken against the club's season at his position: the odds of a hit on his
ground are multiplied by (his range ÷ the club's) to the power −1.27, an exponent measured
for whole teams over 168 team-seasons, 1975–2023. Reaching on an error is the league's share
of the outs on that ground, scaled by the fielder's errors per chance against MLB's at his
position. The first Play for a season after this change gets Retrosheet's play-by-play
again, once, to read the hit locations.

What happens to the runners is drawn from what really happened that season, from
Retrosheet: for each kind of result in each base-out state, every outcome (where the
batter and each runner ended up, outs on the bases included) comes up as often as it did
in the majors. Runners and fielders meet the way batters and pitchers do: each against his
own league, multiplied in odds. Each runner tilts the draw by his own extra bases on hits
(or tag-ups on fly outs) times the arm of the fielder who picked up the ball (or caught
it): how often runners took an extra base on hits he picked up, and tagged up on his
catches, against MLB's at his position. A batter's double plays times how often the
fielder who fielded the grounder turned double-play chances tilt ground-ball outs. Steal
attempts and success are the runner's own times the catcher's (against MLB's catchers)
and the pitcher's (against his own staff, who threw to the same catchers); whether he's
safe is also times the infielders covering the base: the second baseman and shortstop on a
steal of second (an equal share each, since who covers isn't recorded on a successful
steal) and the third baseman on a steal of third, each against his own club's rate on those
steals (Retrosheet: every attempt while he was at the position, and how many were caught;
60 attempts at the club's rate added). Wild pitches,
passed balls, balks and pickoffs happen at the league's rates for that base-out state.

**Managing.** Starts are shared out in proportion to the Rotation's real starts. A starter
pitches until he has faced his usual batters per start; a reliever his usual batters per
relief appearance. The relief role depends on the situation:

* **the closer** (the Closers' most games finished): a lead of 1–3 from the 9th. He comes
  in at the start of the inning if whoever is pitching would run out during it.
* **the other Closers**: the 7th and 8th with the score close (down 1 to up 3).
* **Long Relief**: when the starter goes before the 6th.
* **Other and Long Relief**: everything else.

**Fatigue** (beside DH; on by default). Each regular starts the share of his club's games he
really started (his fielding records' games started, the DH's included, against the club's
games), on a fixed schedule spread through the run and staggered so the regulars don't all
sit together. On his day off the bench player with the most innings at his position plays
it and bats in his place (for the DH, the bench's busiest hitter); with nobody to play it, he
plays. A player traded during the season counts only his games for this club: Jose Reyes
started 43% of the 2015 Blue Jays' games, and Troy Tulowitzki plays shortstop in the
rest. A reliever who pitched in each of his club's last two games sits out the next, and
so does one who faced at least twice his usual relief batters (and at least 8) in the
last. If nobody else is left, a tired reliever pitches. Starters already rest between
starts. The Team pages show the share of games each regular started (simulated / real)
and the bench players' starts; the play by play says who rested and which relievers sat
out tired. **Everyone plays every game, fresh** turns both off.

**Pinch hitters for regulars.** From the 7th inning, with the batting side between 3 runs
behind and 1 ahead, a regular is pinch-hit for when a bench hitter not yet used (with at
least 100 real plate appearances: a manager doesn't trust fewer) is worth at least 0.07 runs
a plate appearance more against the pitcher now in. Each is valued by the games' own chances
for that matchup, platoon, park and fielders included, scored with linear weights (walk
0.69, single 0.89, double 1.27, triple 1.62, home run 2.10, out 0). So a left-handed bench
bat comes up against a right-hander when it's worth it. The pinch hitter takes the
regular's place in the order; when his side takes the field, the best glove at the position
plays it, as after a pinch runner, and with nobody who could play it there's no pinch
hitter. 0.07 was set so that 2015's AL parks get about the real number of such pinch
hitters (Retrosheet: 0.35 a team-game, late and close; the simulated Yankees and Blue Jays
0.35 and 0.32). The 1975 Orioles get 0.65 (real 0.66). The play by play gives both
values.

**Defensive replacements.** From the 8th inning, a side taking the field with a lead of 1–3
sends in a bench player whose fielding record at a position beats the fielder's there by at
least 20 points of MLB's chances (range as on the Range tab, with 50 average innings added;
he needs 100 innings there that season). One change a half-inning, the biggest gap first,
since range factors are noisy and a smaller edge sent in two or three at once. He bats in
the replaced player's place. Bench cards say whom each would replace. It's rarer than it
sounds: in 300 games the 1975 Red Sox and Orioles make 96 such changes, and the 2015 Yankees
and Blue Jays none (no bench player with 100 innings at a position beats their regular there
by 20 points). The Team pages' "On defense" also counts the gloves who take over after a
pinch runner.

**Pinch runners.** From the 8th inning, with the game within two runs either way, a runner
the Speed tab calls a **Slow regular** gives way to the first bench runner available in the
Speed tab's order: Speedy bench, then Speedy regulars, then Smart regulars, each fastest
first (bases gained per opportunity). A pure pinch runner who never batted counts, like
Herb Washington in 1974. The runner takes the Slow regular's place in the batting order.
When his side takes the field, the position goes to whoever has the best fielding record
there: the pinch runner or a bench player who played it that season. The record is range
as a share of MLB's chances, with 50 league-average innings added so a few innings can't
win the job. If nobody could play the position afterwards, there is no pinch runner.
Without the Bases Gained custom stat the Speed groups can't be worked out, so nobody is
pinch-run for.

From 2020 on, extra innings start with a runner on second.

**Pages:**

* **Result**: wins, home and road records, runs, Pythagorean record, final margins (Team 1
  or Team 2 by so many), how many runs a game, and the team's record in each starter's
  games. Above the table, how sure the run is: Team 1's share of the games with a 95%
  interval (Wilson's, ties counted as half), its run margin a game with one (from the
  games' spread), the same for its share of the series in series mode, and about how many
  games it takes to tell the two teams apart at the share found (its interval clear of
  .500): a .600 team needs about 93 games, a .520 team about 2,400. When the run is shorter
  than that, the page says to play more.
* **Team 1** and **Team 2**: **Change lineup, fielders or rotation…** lets you choose the
  side's batting order as a player and a position for each place: a position he played
  for the club that season, or DH in a DH season (in a season without the DH the
  pitcher's spot must be in it once). The fielders are the batters at their positions, so
  every position must be taken once and every batter needs a position. It also sets the
  rotation. The rotation is either the Prevention tab's, where starts are shared like the
  real ones, or yours: up to six pitchers in strict turn, so one pitcher means every game.
  A reliever you put in your rotation starts and goes as long as he does in relief; a
  starter you leave out may relieve once the bullpen is used up. **Defaults** puts back
  the tabs' choices, with the suggested positions for anyone who bats but doesn't field.
  **Play with these** (enabled once every place has a position) plays the games again; **Back to the tabs' defaults** undoes your choices.
  Choices stay with that team-season until you change team or season.
  The pages show the batting order with simulated vs real AVG/OBP/SLG and Runs
  Produced per PA, plus the running factors (odds against the league, 1.00 = average); the
  defense, each fielder with his range against MLB's and the club's season at his position
  and what he does to hits and errors on his ground; the staff by role with each pitcher's
  batters per start and per relief appearance, innings per 162 games, and simulated vs
  real runs per nine.
* **Cards**: one card per player, much like a table-game card. The **How a card is played…**
  button opens a window summing up the rules below, leaving the tab to the cards.

  Every card reads the same way: how often something happens against an average opponent
  (in his own team's league that season, his team's home environment),
  with the league's figure in brackets, and ▲/▼ where the two differ by 5% or more — green
  better and red worse *for the card's owner*, grey where it's neither (how often a runner
  tries to steal). Batting and Running are what he does; Pitching and Fielding are what an
  average batter or runner does against him.

  * **Batting** / **Pitching** (the bar and grid at the top): how plate appearances end, as
    shares of 100%. A batter's is him against an average pitcher and fielders; a pitcher's is
    an average hitter against him, so it shows what he *allows*. In a game the two are
    combined by log5: each result's chance is the batter's ratio to the league × the
    pitcher's ratio to the league × the league's rate, scaled so the results add up to 100%.
  * **Platoon** (batters, bench and pitchers): on base, home run and strikeout against each
    hand on the other side (LHP/RHP for a batter, LHB/RHB for a pitcher), against an average
    opponent of that hand, beside an average player of his hand there. His real splits (MLB,
    from 1950) are under Reference, with how hard they're pulled toward his whole season.
    The Team pages show (L), (R) or (S) after each name: the side he bats from, or for a
    pitcher the hand he throws with. The play by play shows each plate appearance's matchup,
    e.g. "(L vs R)".
  * **Running** (batters and bench runners): against an average battery, arms and
    infielders, how often he tries to steal and is safe, takes an extra base on a single or
    double, tags up and advances on a fly out, and grounds into a double play. Steals are at
    any base, worked out as the games play them: his figure applied to each base's league
    rate, the bases weighed by how often each was a chance (second is most of the tries;
    third and home are seldom tried, so tries per chance at any base read lower than from
    first alone).
  * **Park** (one per team, first): how plate appearances end in its home park, an average
    batter against an average pitcher and fielders, beside the league's, with the arrows in
    grey (a park is neither side's). Its reference gives the Retrosheet counts behind it,
    the factors that differ from neutral by 3% or more, and the fence distances; Simulated
    gives the team's home record. Player cards are in a neutral park.
  * **Fielding** (fielders and pitchers): against him, for an average batter or runner, how
    often balls to his own ground fall for hits (**Range**) and are errors (**Errors**);
    for an outfielder, how often runners take an extra base on hits he picks up and tag up
    on his catches (**Arms**); for an infielder or pitcher, how often double-play grounders he
    fields are turned (**Turning DPs**); for a catcher, and a pitcher with his club's
    catchers, how often runners try to steal and are safe, at any base (**Stopping steals**);
    for a second baseman, shortstop or third baseman, how often runners stealing the base he
    covers are safe, against his club's other infielders (**Stopping steals** too).

  In play a runner and a fielder meet as a batter and pitcher do: each against his own
  league, multiplied in odds. A runner who takes an extra base 52% of the time (league 38%)
  against an outfielder who allows 45% (league 50%) takes it about 47% of the time. Range
  and Errors count only on balls to the fielder's own ground: each ball in play goes where
  that kind of ball went that season, by Retrosheet's hit locations, to one fielder's ground
  or a hole or gap two share, and nobody else, on the field or on the bench, counts on it. A
  fielder's range is taken against his club's whole season at that position, because the
  pitchers' lines were earned in front of those fielders and would otherwise count them
  twice. The play by play names the fielders each ball went to and who picked it up.

  Each table row and line begins with its category in bold, the names the path analysis
  measures. Below the tables:
  * **Also used in play**: **Order** (where he bats), **Usage** (a pitcher's share of starts,
    batters per outing, closer and relief picks), **Pinch running** (a Slow regular is run for
    late in close games; bench runners in their order, and who can take the position
    afterwards), **Managing** (how often he is walked intentionally and sacrifices, as odds
    against the league's, in each situation), and a pitcher's batting in the pitcher's spot.
    Order and Usage decide who plays when; the path analysis doesn't measure them (it counts
    Managing in Batting). Where MLB's lines don't split ground
    and air outs, the card says the league's split is used.
  * **Reference — his real season, and notes**: the real season line and real fielding the
    tables are worked out from, how much of the balls in play his ground gets (alone, and
    shared with whom), for a pitcher the club's season fielding his line is cleared of, the
    Speed tab's label, and fielding at other positions.
  * **Simulated**: what he did in the games.

  **Bench** cards cover pinch runners, in the order they're sent with their bases gained per
  opportunity, and players who could replace a fielder, with their fielding record at each
  position. Each shows how often he went in. The Team 1 and Team 2 pages list the bench
  players who got into the games.

  Fielders who don't bat get a card with just the Fielding table. **Show** filters by
  team, by batters, pitchers or fielders, and by pitching role.
* **Card Battle**: one matchup worked out from the cards of the last run, as the games do
  it, with the chosen cards shown beside the working.
  * **Batter vs. Pitcher**: choose which team bats, a batter (lineup or bench), a pitcher
    from the other team and whose park. A table shows each result's chance for the batter
    and his league, the pitcher and his league, the league and the park's rate there, the
    log5 mix and the final chance after the fielding team's usual fielders. With platoon
    figures the batter's and pitcher's columns are against each other's hand, and a column
    gives the park's rates for that matchup of hands (e.g. "LHB vs RHP here"). Then the
    outcome: the AVG / OBP / SLG this plate appearance comes out to, and its strikeout,
    walk, home run and error chances.
  * **Fielder vs. Runner**: choose a play. **Steal** puts a runner against the catcher and a
    pitcher you choose, with a table for each base and the result at any base. **Extra base
    on a single or double** and **Tag-up on a fly out** put him against an outfielder's arm.
    **Double play on a ground ball** puts the batter against the infielder who fields it.
    Each shows the league's rate in odds, each side's factor multiplied in, and the
    resulting chance: the same factors that tilt the season's play tables in the games.
* **Charts**: each team's runs in a game, and the final margins (who won, by how many), as
  bars of the share of games; hover a group for the counts.
* **One game, play by play**: any game of the run, played again exactly (it says whose park
  it was at and who batted first), with every plate
  appearance's chances, the random number drawn, and how often that outcome happened in
  the majors. Above it, the home team's chance of winning after each plate
  appearance, drawn as on the WPA tab (hover a point for the play; the three biggest
  swings are ringed). The chances come from the run itself: in the games, how many runs
  each team scored from every base-out state to the end of the half-inning (fresh innings
  start with the bases empty, or from 2020 extra innings with a runner on second). From any
  point the rest is a sum over the half-innings left, with a game tied after nine won in
  extra innings by P(home outscores the visitors in an inning) ÷ (1 − P(a tie)). So it
  knows each team's scoring but not who is batting or pitching. Bottoms of the 9th and
  later aren't counted, because a walk-off cuts them short. In 1,000 Yankees–Blue Jays games of
  2015, the Blue Jays at home start at 63% (they won 67% there), a home team tied in the
  bottom of the 9th is 71%, and one up a run going to the 9th 86%. **Show: Box score**
  gives the same game as the Boxscore tab draws one: the line score (runs by inning, hits,
  errors), then each side's batters by their place in the order (each place's pinch hitters,
  pinch runners and defensive replacements indented under it, marked PH, PR or their
  position), AB, R, H, RBI, BB and SO with totals, the extra-base hits, sacrifices,
  intentional walks, double plays and steals under them, and the pitchers in the order they
  pitched with IP, H, R, BB, SO and HR. Errors are the times the other side reached on one,
  since the Simulator doesn't charge errors on throws or advances, and runs aren't split into
  earned and unearned.
* **How it works**: the method, with a worked example for this matchup (Team 1's
  leadoff man against Team 2's ace, at Team 2's park as in game 1, result by result) and how a single with a runner on
  first played out that season.

**Export…** (once a run is played) saves it as an Excel workbook, a sheet per table, or as
CSV files, one per table, named after the file you choose (`…-result.csv`, `…-games.csv`
and so on). The tables are the **Result** (each team's W, L, ties, win share, home and road
records, runs and runs allowed a game, shutouts, Pythagorean record), the **Series** in
series mode (series won and how many games they took), every one of the **Games** (who was
at home, the score, both starters, the winner), and each team's **batting** (every line
in the games: starts, PA, AB, H, 2B, 3B, HR, BB, IBB, HBP, K, SF, SH, reached on errors, R,
RBI, SB, CS, GIDP, times as pinch hitter, pinch runner and defensive replacement,
simulated and real AVG/OBP/SLG) and **pitching** (role, G, GS, BF, H, BB, IBB, K, HR, R, IP,
simulated and real runs per nine).

**Round robin…** opens a window for a league of your own: choose a team and season, **Add**
it to the list (two to twelve; **Remove** takes one off), set **Games per pair** (20 to
start) and press **Play**. Every pair plays its games taking turns at home, each in its
home team's environment, with the tab's DH, Fatigue and Seed. Each pair has its own seed, so
adding a team doesn't change the others' games. Each side is set up by the tabs' defaults
(your lineups for Team 1 and Team 2 aren't used; a lineup with batters who don't field is
played as it is, and the notes say so). Standings come back best first (win share, then run
margin): W, L, Pct, games behind, runs scored and allowed a game and the Pythagorean
record, then a head-to-head grid of each row's wins against each column (green: won the
season series). Three 2015 AL East clubs, 40 games a pair: Blue Jays 50–30, Yankees 38–42,
Red Sox 32–48.

**Path analysis…** opens a diagram (its **How to read this…** button opens the explanation in a
window of its own; it and the progress are at the top) of what each part of each team did,
followed along the chain by which runs, and so wins, are made. The Simulator replays the
same games (same seed) with one part at a time made league-average, or switched off, so the
difference is that part's contribution.

**The chain.** Runs come from batters reaching base and being driven around before the outs
run out (Bill James): a game's 27 outs are the clock. So each part is measured link by link:
**outs at the plate** (the share of plate appearances that end in an out) and **outs on the
bases** (caught stealing, picked off, thrown out on the bases, the second out of a double
play) decide how many **plate appearances** the outs buy; of those batters so many are
**batters reaching** (on base or home), and of those so many are **reachers who score**; that
makes the **runs**, and the runs the **wins**. These are the diagram's columns, left to right,
each head giving the lane's own figures, with arrows from link to link. Every part and every
row has a track running through them: what it changed at each link (pt: percentage points).
In 2,000 Yankees–Blue Jays games of 2015 the Yankees make outs in 66.5% of their plate
appearances and Toronto in 62.9%, so Toronto gets 1.1 more plate appearances a game, 1.8
more batters reaching and a larger share of them scoring (37.1% against 32.7%): 5.32 runs a
game against 4.11. Toronto's pitching staff, against the Yankees: +0.8 points of outs at the
plate, so 0.41 fewer plate appearances, 0.42 fewer reaching, 0.24 fewer runs, +4.3% wins for
Toronto. Its batting order: 2.9 points fewer outs, +1.19 plate appearances, +1.54 reaching,
+3.5 points more of them scoring, +1.02 runs, +8.8% wins, though its runners also make 0.21
more outs on the bases a game (red). A part can work through one link and cost at another,
and the track shows both.

Each value is coloured for the team that owns the part: green helped it, red hurt it. Grey
with a ? is within two standard errors of nothing (measured game by game; for a rate, each
game's part of it). The line from a box to its track takes the colour of its runs.

**The lanes.** Two lanes, one per team at bat: its batting order and baserunning, and the
other team's pitching staff and fielding. A box's bold track is the whole part made
league-average; the tracks under it are each of its rows alone, the cards' categories: Range,
Errors, Arms, Turning DPs, Stopping steals; Steals, Extra bases, Tag-ups, Double plays, Pinch
running. The Batting order and Pitching staff boxes have two each: Batting (or Pitching),
the players made league-average, and Platoon, only their own left/right splits taken away
(each then has his whole season's figures against both hands, with the league's platoon
effect still in play). For fielding, league-average means MLB-average fielders at every
position; range factors count chances, so a club's range is first divided by its staff's
balls in play per out against the league's, leaving the fielders' skill. The parts and rows
aren't exactly additive.

**Managing and home field.** Under the lanes, a band holds what acts on both sides of a
team's games, each switched off for that team: its **Managing** (Sacrifices: its batters
swing away instead; Intentional walks: its pitchers pitch to those batters; Pinch hitting,
for the pitcher and for regulars; Defensive subs; Rest days: its regulars play every game;
Tired relievers: its relievers are always fresh) and its **Home field** (its Park made
neutral, and its Home-field edge taken away). Each row has two tracks: **at bat**, the
team's own chain, and **against**, the other side's chain against it (coloured for the team,
so more outs there are green); its wins come from both. Toronto's rest days work through its
batting: 0.5 points fewer outs, +0.36 plate appearances, +0.23 runs, +3.0% wins (Tulowitzki
and Colabello starting). Its park costs its own batters (+0.7 points of outs, −0.22 runs),
and its home batters' edge gains them (−0.4 points, +0.11 runs). A faint track marked "never
came up" means the replay was identical to the run: the decision never arose (no defensive
replacement ever qualified for these two clubs).

The Result box gives each team's wins and its runs scored and allowed, and how to read the
colours. The window opens as big as the screen allows and scrolls across and down for the
rest. The analysis plays the run's own games (the first 2,000 of a longer run) again 56
times, once per part and once per row, which takes about seven minutes; the window counts
the replays as it goes. Each replay is a game that never happened (the same game with one
thing switched off), so it can't be read off the run itself. The run's own games are the
baseline, reused as they are when the run was 2,000 single games or fewer (a longer run's
first 2,000, or a series run's games played as single games, are played once more for it).

**Checks.** With every player set to exactly league-average, the Simulator scores within
2% of the league's real runs per game: 1927 AL, 1975 NL, 1985 NL, 2019 NL and 2023 AL
(rechecked with ball-by-ball fielding and pinch hitting: 2015 AL 4.42 against 4.40, 2015 NL
4.14 against 4.10, 1975 NL 4.24 against 4.14). "League-average" here means each kind of
batter at his kind's average: position players at the league's position players', pitchers
at MLB's pitchers' batting. (Giving the pitcher's spot an average hitter's line, as an
earlier check did, ran no-DH leagues about 3% high; leaving out pinch hitters ran them 4–5%
low.) With platoon, "league-average" is average for his hand (a left-handed hitter as an
average left-handed hitter does), and turning the platoon effect on moves league-average
scoring by under 1% (6,000 games each, on against off: 2015 AL 4.44 against 4.40, 2015 NL
4.03 against 4.03, 1975 NL 4.28 against 4.25); what's left is the two rosters' mix of hands.
Intentional walks and sacrifices decided by the situation, against left in the lines, also
move it by under 1% (2015 AL 4.42 against 4.43 in 12,000 games, 2015 NL 4.13 against 4.11,
1975 NL 4.05 against 4.06), and real clubs come out near their totals: 2015 Cardinals 42
intentional walks and 36 sacrifices per 162 (real 47 and 39), Yankees 20 and 21 (23 and
24), Blue Jays 16 and 36 (12 and 36); 1927 Yankees 209 sacrifices (204).
In older seasons Retrosheet often records only which outfielder picked up a hit, not where
it went or what kind it was, so such a hit is shared by him and the infielders it may have
got past. The
early-1970s AL runs a little high: 1971 4%, 1972 5%, 1973 3%. Where MLB's lines don't split
outs into ground and air outs (before the mid-1970s), the season's Retrosheet play-by-play
gives the league's split, and every player's outs are split that way.
With fatigue off, a club's regulars playing every game score a few percent more than its
real season did, because that season had bench players and rest days in it (2015 Yankees
against Blue Jays, 2,000 games: Yankees 4.23 runs a game with fatigue off, 4.10 on).

**Left out:** relievers chosen for their hand (the platoon plays, but the opposing manager doesn't answer a
pinch hitter),
bunting for a hit or a squeeze as a choice (they're in the lines and the sacrifices' outcomes), resting players
who don't start in the batting order (a resting regular doesn't pinch-hit), and a starter's fatigue beyond his usual
batters per start.

### Naturalzone tab

How the Range tab's nine fielders might cover for one another. The field is drawn as
Total Zone Rating divides it: the Project Scoresheet / Retrosheet batted-ball location
zones (<https://www.retrosheet.org/location.htm>), to scale for a park 330 ft down the
lines and 405 ft to centre. A zone named for one fielder (`6`, `6S`, `6D`, `6M`) is his;
one named for two (`56`, `78XD`) is split down its middle; foul zones go with the fair
ground beside them. That gives each position a **usual zone** (dashed), and the
**Contiguity** table gives how many feet of border each two share.

Each fielder's **natural zone** (solid outline) is his usual zone grown or shrunk along
its borders with his teammates, never at the wall or the lines, until its area is his
Range-tab ratio (range factor ÷ MLB's at his position) times the usual area: a 120%
third baseman covers a fifth more ground, an 85% shortstop gives some up. The zones
are fitted one at a time, so they needn't meet. Ground a weak fielder gives up that a
neighbour reaches is shaded green, **made up**. Ground nobody reaches any more is red,
a **gap**: batted balls there are likely to fall in. Ground two fielders now reach is
blue, an **overlap**. Arrows show who reaches into whose zone (3% or more of it), and
hovering a spot names its zone and who covers it. **Who covers each zone** tables it:
each usual zone's share inside each fielder's natural zone, and the share nobody covers.

**Play them in … (from the Stadiums tab)** lays the field out in the park chosen on the
Stadiums tab. The zones keep their rays and arcs, but the deepest ones run out to that
park's fence and the foul strips as far as its lines do, so a deep centre field gives the
center fielder more usual ground and a short line takes some from the corner outfielder.
A fielder's natural zone is then his ratio times his usual ground *in that park*, and the
uniform park's fence is drawn dotted for comparison.

The pitcher's and catcher's range factors are mostly strikeouts, comebackers and
pop-ups rather than ground covered, so a checkbox (off by default) keeps their zones as
they are. The tab follows the Range tab: change a player
there and the zones are redrawn.

The model assumes a uniform field size (the diagram's one park, whatever park the
fielder played in) and a uniform spatial correspondence between normalised range factor
(his ÷ MLB's at the position) and normalised field space (the share of his usual zone he
covers). Its areas, reaches, gaps and overlaps are therefore indices under those
assumptions, for comparing fielders and defenses, not literal claims about ground
covered or balls reached.

### Custom tab: Retrosheet variables

Besides MLB's figures, a custom-stat formula can use counts from Retrosheet's play-by-play.
Naming one is all it takes: the season's Retrosheet records are downloaded the first time a
formula needs them (4-10 MB, then kept), and a season Retrosheet hasn't published yet, such as
the current one, leaves those stats blank with a note saying why.

* **Player level**
  * `Running (Retrosheet)`: stolen bases, extra bases on hits, tag-ups and their opportunities
    (the Speed tab's counts).
  * `Relief (Retrosheet)`: each pitcher's starts (outs, his decisions, no-decisions the team won
    or lost, the team's record in them) and relief appearances (outs, decisions, games finished,
    save situations and the outs in them, holds, saves, blown saves, blown holds), from the
    Bullpen Support tab's game records.
  * `Batted In (Retrosheet)`: runs batted in by the base each runner started from (himself,
    1st, 2nd, 3rd), and how many different runners they were (the Players Batted In tab's).
* **Team level**
  * `Safe/Out (Retrosheet)` and `Safe/Out Allowed (Retrosheet)`: outs, times reaching 1st,
    2nd and 3rd, runs scored and placed runners, for a club's batters and for its opponents
    (the Safe/Out tab's).
  * `Relief (Retrosheet)`: the same relief counts added up over the whole staff.

Holds and blown saves under `Relief (Retrosheet)` are counted from the play-by-play by MLB's
definitions, so they exist for every season, but from 1999 on they can differ slightly from
MLB's records. MLB's official figures are under `Pitching`: `HLD`, `BS`, `SVO` (save
opportunities), `IR` and `IRS` (inherited runners and how many of them scored); at Team level
the same five are `Pitching: Holds` and so on.

The figures other tabs work out (Prevention's unearned runs, Production's runs produced per
plate appearance, Speed's bases gained per opportunity, Bullpen Support's innings per start,
records in starts and save situations converted, Safe/Out's per-inning rates, and Players Batted
In's shares by base) come as custom stats in the `*-player.json` / `*-team.json` files in the
app folder, which load at startup.

### Custom tab: Score stats

Five player-level custom stats put the green/red comparisons of the Production,
Prevention, Range and Speed tabs side by side, each only for a player with enough playing
time for his team that season (a "team game" is a game his team played, so short seasons
scale):

| Stat | What it is | Counts with at least |
|---|---|---|
| **Production Score** | his runs produced per plate appearance ÷ MLB's | 3.1 PA per team game (MLB's batting-title rule, 502 in 162) |
| **Prevention Score** | MLB's runs per nine innings ÷ his, as on the tab — above 1.00 is fewer runs allowed | 0.3 innings per team game (about 50, so relievers count) |
| **Range Score** | the chances he made ÷ those an average MLB fielder would have, at the same positions in the same innings | 3 fielding innings per team game (a third of his team's, so platooned fielders count) |
| **Speed Score** | his bases gained per opportunity ÷ MLB's | 1 baserunning opportunity per team game |
| **Overall Score** | the average of the scores he qualifies for (higher is better on all four) | at least one qualifying score |

So 1.00 is MLB-average on every score, and higher is better on all of them; a pitcher's Overall
is in practice his Prevention — a hitter's, the average of Production, Range
and Speed. A score he doesn't qualify for is left out of the Overall rather than counting
against him. Range
needs fielding innings, which MLB has from 1901 on; Speed needs Retrosheet's baserunning
(or the Uptodate tab's, for this season).

They're ordinary custom stats (`production-score-player.json` and so on, loaded at
startup), so their formulas can be read and changed on the Custom tab. Three things in the
formula language make them possible, and work in any player-level custom stat:

* `{Custom: Name}` — another player-level custom stat.
* `qualify(amount, minimum, value)` — the value, or blank if the amount falls short of the
  minimum (rounded to the nearest whole number, as MLB rounds 3.1 × 162 to 502).
* `product(a, b, ...)` and `average(a, b, ...)` — whichever of their arguments aren't blank,
  multiplied or averaged.

And three groups of variables: `{MLB: ...}` (MLB's runs produced per plate appearance,
runs per nine innings and bases gained per opportunity that season — the custom stats of
those names worked out over the whole league), `{Range: ...}` (a fielder's chances,
expected chances and innings, measured as on the Range tab), and `{Team: G}` (his team's
games). They're only for player-level custom stats; the Correlation, Regression and Factor
Analysis tabs don't list them as roster totals.

### Player Scores tab

Next to Personal, and found the same way (by name, or from a team's roster): one player's
five Score stats for every season he played — a line for each team, and for a season split
between teams, a line for the whole season too (in italics), since a traded player often has
the playing time only over the whole season. The scores are the Custom tab's stats of those
names, so the two tabs always agree, and changing a score's formula there changes it here.
1.00 is MLB's figure that season; green (above 1.00) is better and red worse, on every score —
Prevention is MLB's runs per nine ÷ his, so it too is better above 1.00. Overall is in bold.

A score he hasn't the playing time for is shown anyway, in grey parentheses — (0.912) —
so a short stint or a pitcher's few times at bat can still be seen, but it counts in no
average: not the Overall (a line with no qualifying score has its Overall in parentheses
too, the average of those that don't qualify), and not the Career line.

A bold **Career** line at the bottom averages each of the five scores over the seasons
that have it — a plain average of the yearly scores, each season counting once (a split
season by its whole-season line, or, where that's blank, by its team lines' scores
averaged). A score needs at least three such seasons for a career figure; otherwise it's
blank, and its tooltip says how many seasons there were. The Career Overall is the average
of the yearly Overall scores, not of the four career scores.

Each season needs its team's and MLB's figures and Retrosheet's play-by-play (for the Speed
Score; only in the seasons he batted or ran), so the first time a long career can take
several minutes — about five for a 13-season pitcher. After this, the downloaded seasons
are kept, will make this query fast in the future, and may make other queries faster. While it
works, the tab says how many team-seasons there are and how many of his seasons still need
Retrosheet's play-by-play downloaded, with a rough time; a bar shows how far it has got,
opening already part-filled with the team-seasons that are on hand.

### Info and Infograph tabs

**Info** holds *queries*: each one works out a range of values — one per game or per
season, for one or more players or teams — and shows them as a table. **Infograph**
draws a query as a line graph. Both read the same data the other tabs do, and a query's
formulas are written exactly like Custom stats (`{Batting: H} / {Batting: AB}`), so any
Player-level custom stat can be used too, as `{Custom: Runs Produced}`.

On **Info**: pick a query on the left, change it in the middle, and **Run** to fill the
table on the right (hover a value to see its game or season). **Graph ▸** runs it and
switches to Infograph. **New**, **Remove**, **Load…** and **Save…** manage the list;
every `info-*.json` file in the app folder loads at startup.

| Field | What it means |
|---|---|
| **Data** | *Player, game by game* (the Dailies tab's data), *Player, season by season* (the Personal tab's), *Team, game by game* (Dailies at the Team level), *All MLB player seasons, ranked* or *All MLB team-games, ranked* (below) |
| **Subjects** | players or teams, separated by `;`. For teams, a league's or division's name (`National League`, `American League East`, or short: `AL East`, `NL Central`) means every club in it that season — divisions from 1969. If two players share a name, the one who'd debuted by that season is used, and a note says so; write `Name [id 123]` to choose |
| **Season** / **Career** / **Include postseason** | for the game-by-game data (game logs start in 1901). **Career** (player games only) takes every season he played instead of one; a note says if some of his seasons came before 1901 |
| **Only where** | keep only the games (or seasons) where every condition holds, separated by `;`. A condition is two formulas, or a formula and a number, joined by `=` `!=` `<` `<=` `>` `>=` — e.g. `{Pitching: H} = 0; {Pitching: Outs} >= 27` keeps a pitcher's no-hitters. Running totals, game numbers and the graph then count the kept games alone. Blank = every game |
| **X axis** | *Date* or *Game* (each subject's own game number) for games, plus *Age* for a player's games (his age on the day, from his birth date — shown as "Age 26, 104 d"); *Age* or *Season* for seasons. A season's age is the one the source gives for it |
| **Running totals** | work each formula out over the totals so far — so `{Batting: H}` is hits to date and `{Batting: H} / {Batting: AB}` the season-to-date average |
| **Series** | one line per subject for each: a name, a formula, a **panel** (1–4) and a **format** (`auto`, `int`, `rate` for .400, or `1`/`2`/`3` decimals) |
| **Reference lines** | `panel: value`, comma-separated — e.g. `2: .400`. Write `1: .500 = 0` to label a line at 0 ".500" |

**All MLB player seasons, ranked** takes every player's season in the AL and NL from
**Season** **to** the second year. It judges each at his main position (the one he
played most innings at) against the league average that season, keeps the ones that
pass **Only where**, and ranks them by the **Rank by** formula, highest first. The
**Top** few are tabled and graphed, with the x axis naming each one ("Deer 1991").
Besides `{Batting: …}` and `{Fielding: …}` (at his main position, with `{Fielding: RF/9}`
and `{Fielding: RF/G}` worked out from exact innings), its formulas can use
`{MLB: AVG}`, `{MLB: OBP}` and `{MLB: SLG}` (the leagues' figures that season), and
`{MLB: RF/9}` and `{MLB: RF/G}` (all MLB at his position). `{Team: G}` is his team's
games (for a traded player, the average club's), for MLB's qualifying rules: 3.1 PA per
team game for the batting title, and games at the position ≥ ⅔ of team games for a
fielding title. It needs no subjects. In the file, `positions` (default all nine)
limits whose main position counts, `leagues` (default `["AL", "NL"]`) picks the leagues
— the Negro Leagues' records are too incomplete for rates — and `"distinct": true`
keeps each player's best season only.

**All MLB team-games, ranked** takes every AL and NL game from **Season** to the second
year (1901 on — **Include postseason** adds the postseason), once for each team in it, from
MLB's line scores. It keeps the ones that pass **Only where** and ranks them by **Rank by**
(ties in date order; `{Game: Date}` with `"order": "lowest"` lists them by date). Its
variables: `{Team: W}`, `L`, `T`, `Runs`, `Runs allowed`, `Home` (1 at home);
`{Innings: Batted}` (the innings it came to bat — a home team that won without batting in
the ninth had eight), `{Innings: Scored}` (those it scored in), `{Innings: Scoreless}`,
`{Innings: Most runs}`, `{Innings: Fewest runs}`, `{Innings: Opponent batted}`,
`{Innings: Opponent scored}`, `{Innings: Game}` (the game's length); `{Inning: 1}` …
`{Inning: 9}` (its runs in that inning, blank if it didn't bat) and `{Inning: Extra}`;
`{Game: Season}`, `{Game: Date}` (as 19230601) and `{Game: Postseason}`. Hovering a game
shows its score and the team's runs inning by inning. MLB has no line scores for the Negro
League games, nor for a few dozen AL/NL games (forfeits and the like); a note counts them.
In the early 1900s the home club could still choose to bat first, so either side may be the
one that skipped the last half-inning. `distinct` keeps each franchise's first game only.

With `"pitchers": true` (file-only) it ranks every **pitcher's** season instead: his
`{Pitching: …}` line, `{Team: G}` (an inning per team game qualifies for the ERA title),
`{MLB: R/9}` and `{MLB: ERA}` (all MLB that season), and his most-thrown pitch from pitch
tracking (2008 on — PITCHf/x, then Statcast from 2015): `{Pitch: Primary %}` of his
pitches, at `{Pitch: Primary mph}`. `"order": "lowest"` ranks the lowest values first
(for runs allowed), and `"each": "primary pitch"` keeps the **Top** few for each pitch
type separately, the most common main pitch first; the x axis names the pitch over the
pitcher. The pitch mix takes one request per pitcher-season that passes **Only where**
(the first run of a long span takes a few minutes; it's cached after).

Variables: everything a Player-level custom stat can use (`{Batting: …}`, `{Pitching: …}`,
`{Fielding: …}` — innings count as true thirds here), plus `{Team: W}`, `L`, `T`, `G`,
`Runs`, `Runs allowed` for the game-by-game data (the team's result that game), and
`{Player: Age}` / `{Player: Season}` for seasons. A traded player's season counts as one
line (his all-teams total).

**Infograph** draws one y scale per panel: two measures of different size (hits and
batting average) go in panels stacked over the same x axis, never on two axes. Each
player or team keeps its colour; a legend appears when there are two or more lines, and
lines carry their name and last value at the right when there are four or fewer.
Move the mouse over the plot for a crosshair and every line's value there (most
first when there are many); hover a legend entry to bring its line forward. **Save
image…** writes a PNG; **Refresh** fetches again.

#### The query file

A query is a JSON object (a file may also hold a list of them). Everything the Info
form shows, plus a few things only the file can say:

```json
{
  "name": "George Brett's 1980 chase for .400",
  "description": "Shown above the graph.",
  "source": "player-games",
  "subjects": ["George Brett"],
  "season": 1980,
  "x": "date",
  "running": true,
  "series": [
    {"name": "Hits", "formula": "{Batting: H}", "panel": 1, "format": "int"},
    {"name": "Batting average", "formula": "{Batting: H} / {Batting: AB}", "panel": 2, "format": "rate"}
  ],
  "panels": {"2": {"title": "Batting average, season to date", "min": 0.2, "max": 0.45}},
  "reference_lines": [{"panel": 2, "value": 0.4, "label": ".400"}]
}
```

`source` is `player-games`, `player-seasons`, `team-games`, `league-seasons` or `league-games`; `x` is `date`/`game`
(`age` too for player games) or `age`/`season`. `"season": "career"` and
`"where": ["{Pitching: H} = 0", ...]` are the **Career** box and **Only where** field;
the ranked source's are `"season_to"`, `"rank_by"` and `"top"`
(the conditions filter the series that use the query's own source). File-only extras: a `title` for the graph, a `description`, `panels`
(a title and a fixed `min`/`max` per panel), and per-series `source`, `subjects`,
`season`, `postseason` and `running`. `"chart": "bars"` draws the query as bars instead of lines: for each subject, a group of
bars, one per series, each the series' value over all his games or seasons together (what
running totals end at) — so a player-seasons query gives career totals. The groups run in
the order the subjects are listed, named under them; the values sit over the bars where
they fit, and hovering a group shows them all. A ranked list drawn as bars gets a group per rank. Series given the same `"stack"` name are piled
into one bar (in series order, from the bottom), labelled with their total. On Info the table is then a row per subject.
A series with its own source or subjects is how
one graph **combines** data — say a player's game-by-game average and his team's — as
long as every series shares the query's x axis (games with games, seasons with seasons).

Examples that come with the app: `info-best-pitcher-by-primary-pitch.json` (for each kind
of main pitch, 2008 on, the qualifying season with the fewest runs per nine innings), `info-brett-1980.json` (hits and average through
1980, with the .400 line), `info-ruth-aaron-hr-by-age.json` (home runs by age),
`info-runs-produced-career-leaders.json` (the ten most career Runs Produced since 1901, with their
Runs and RBI stacked beside it, as bars), `info-runs-produced-season-leaders.json` (the same for
the 20 best single seasons since 1901, a player's seasons each counting),
`info-1914-nl-pennant-race.json` (every NL club's games over .500, game by game — the
Miracle Braves), and `info-three-no-hitters.json` (every no-hitter by the pitchers with
three or more — Ryan, Koufax, Feller, Verlander, Cy Young — by his age that day, with
his own team's runs as the y value: a career, `where` and age-axis example), and
`info-best-glove-in-infield-with-weakest-bat.json` (the ten infield seasons, one per player,
that best combine range above the league average with batting below it, among seasons
qualified for both), and `info-scored-in-every-inning.json` (every game since 1901 in which
a team scored in each inning it batted, in date order — 26 through 2026, only the 1923
Giants, 1964 Cardinals and 1999 Rockies over nine full innings — with its innings, runs
and biggest inning as bars).

### Uptodate tab

Right after Safe/Out (yellow, with the other Retrosheet tabs).  Retrosheet publishes a season's play-by-play only after the season is over, so
the tabs that read Retrosheet (Players Batted In, Bullpen Support, Speed, Safe/Out, the
Simulator, and the Retrosheet variables of Custom and Regression) have nothing for the current
year.  **Update** builds this calendar year's play-by-play from MLB's own game feeds, in the
layout of the file Retrosheet will publish at the end of the year — the seven CSV files of
`YYYYcsvs.zip` (`plays`, `gameinfo`, `batting`, `pitching`, `fielding`, `teamstats`,
`allplayers`), same names, same columns, same order — and saves it on disk as
`~/.local/share/ballsheet/YYYYcsvs.zip`.

* **The first Update** downloads every finished game of the year so far (regular season,
  postseason and the All-Star Game; about 2,500 games, a few minutes).  A counter shows how
  many games have been downloaded and how many are left; **Stop** stops early and keeps what
  has been downloaded.  The file is saved every 500 games, so nothing is lost if BallSheet
  closes mid-way.
* **Later Updates** download only the finished games the file doesn't have yet and add them.
  Partway through the season the file holds the year up to the most recent finished games.
* **Files on disk** lists every year's file with its games, the last date it covers, and
  whether Retrosheet has published that year's own file yet.  **Open folder**, **Save a
  copy…** (to keep or share the zip) and **Delete…**.
* **Until Retrosheet publishes the year**, the Retrosheet-reading tabs use this file
  instead (they say "Retrosheet has no play-by-play … (yet); the Uptodate tab can make this
  year's" when there is none).  Their saved summaries are remade whenever the file is updated.
  **Once Retrosheet's own file is out**, they switch to it by themselves and the list says the
  Uptodate file can be deleted.

How faithful it is.  The counting columns of `plays` (hits, walks, outs, runners on each
base before and after, who scored and who drove them in, earned runs, putouts and assists
by position, the batting lineup and the fielders) come straight from MLB's structured
play-by-play, and the box-score files are added up from them the way Retrosheet adds up its
own.  Checked against Retrosheet's 2025 file over 200 games: those columns agree 99.5–100%,
`batting`/`pitching`/`fielding`/`teamstats` 99–100%, and BallSheet's Retrosheet summaries
built from it match Retrosheet's (Safe/Out and the Simulator's base-out tables exactly).  Two
columns are reconstructions:

* `event`, the play in Retrosheet's notation (`S8/L8D.2-H;1-3`), is written from MLB's data:
  94% identical, the rest mostly a different hit location in it.
* `loc`, where a ball went on Retrosheet's hit-location diagram, comes from MLB's hit
  coordinates (Retrosheet's are a stringer's click): 93% the same zone, the rest next door.
  The `+`/`-` after a location is MLB's own hard/soft call, as in Retrosheet's file.

Small differences that can't be avoided: Retrosheet adds an `NP` line for a pinch runner
about two times in three (the Uptodate file always does); the official scorer's id is
blank (MLB doesn't give Retrosheet's); attendance is MLB's figure.

Ids are Retrosheet's: players' from MLB's own cross-reference, else Retrosheet's
biographical file (last name and birth date), teams', parks' and umpires' from tables
matched against Retrosheet's 2024–25 files (a new park is looked up in Retrosheet's list).
A rookie Retrosheet hasn't listed yet gets an id made the way Retrosheet makes them, but
numbered from 999 down (`smitj999`) so it can't clash with one of Retrosheet's; they're
counted in the file's `uptodate.json`, which also lists the MLB games the file holds.

MLB publishes no rate limit for its Stats API; to be considerate the tab downloads a few
games at a time, compressed, only the games it doesn't have, and backs off if MLB ever
asks it to.

## Where the data comes from

The official **MLB Stats API** (`statsapi.mlb.com`) — no key, and final within
minutes of a game ending. Responses are cached under
`~/.cache/ballsheet/`:

* finished seasons are cached indefinitely (they never change),
* the current season is refetched if the cached copy is over 10 minutes old,
* if the network is down, a stale cached copy is used rather than failing.
* a finished season's line scores (for *All MLB team-games, ranked*) are kept as a small
  summary, `linescores-YYYY-….json`, in place of MLB's full response.

**File → Clear cache** empties it.

MLB's data is for personal, non-commercial use (see the copyright notice in
every response).

The Stadiums tab's park dimensions come first from `stadium_dimensions.json`, transcribed
from English Wikipedia's park articles (CC BY-SA 4.0; each park records its article and
revision, and the tab links them).

Players' other names and nicknames, for the name search, come from **Wikidata**
(`wikidata_names.json`: the English name, aliases and nicknames of every Wikidata person
with an MLB player ID, collected in October 2026). Wikidata is public domain (CC0).

## Limits

* The dropdown always reflects one season's league, so its length changes as you
  move through time (30 today, 16 in 1953, 8 in 1876, 33 in the three-league
  chaos of 1884). Relocations, renames and long-defunct clubs are all covered —
  the earliest season with data is 1876.
* If you park on a club that later folded and then step *past* its last season,
  the dropdown stays on the last league that had it and the stat view just goes
  empty for those years — your selection is never silently swapped.
* Advanced/rate stats before ~1913 (ERA, caught stealing, etc.) are spotty *at
  the source* — the API returns `0.00` / blanks where the events weren't
  recorded.
* Season-level Statcast metrics (xwOBA and the like) come only from the Personal tab's
  *Expected* set (2015 on). The Gameday and Statcast tabs show the per-pitch and
  per-batted-ball figures for a single game.

## Files

| File | Role |
|---|---|
| `ballsheet.py` | the PyQt6 app |
| `mlbstats.py` | standard-library MLB Stats API client + cache (no Qt) |
| `infoquery.py` | Info queries: reading `info-*.json`, fetching and computing (no Qt) |
| `naturalzone.py` | Naturalzone tab: the Total Zone / Retrosheet location zones on a grid (uniform park or a real one), zones grown or shrunk by range (no Qt) |
| `stadiums.py` | Stadiums tab: clubs' home parks and their distances (Wikipedia, else MLB), as fences (no Qt) |
| `stadium_dimensions.json` | Park fence distances by season, transcribed from Wikipedia (article + revision per park; CC BY-SA 4.0) |
| `wikidata_names.json` | Players' other names and nicknames from Wikidata, by MLB player ID, for the name search (CC0) |
| `uptodate.py` | Uptodate tab: this year's play-by-play from MLB's game feeds, written as Retrosheet's season CSV files (no Qt) |
| `retrosheet.py` | reads Retrosheet's season files (or the Uptodate file, for a year Retrosheet hasn't published) into the tabs' summaries (no Qt) |
| `simulator.py` | Simulator tab: teams set up by the other tabs' rules, log5 plate appearances, Retrosheet base-out tables, managing, path analysis (no Qt) |
| `simpath.py` | the Simulator's path-analysis diagram (QPainter) |
| `simcards.py` | the Simulator's player cards (QPainter) |
| `infochart.py` | the Infograph tab's chart (QPainter) |
| `wpachart.py` | the WPA tab's win-probability chart (QPainter) |
| `statcastview.py` | the Statcast tab's 3D field, figures, pitch and batted-ball flights (QPainter) |
| `info-*.json` | the example Info queries, loaded at startup |
| `*-player.json`, `*-team.json` | custom stat definitions; the ones the tabs use load at startup, others with Load definitions… |
| `xlsxlite.py` | tiny dependency-free `.xlsx` writer |
| `test_core.py` | offline tests — `python3 -m unittest` |
| `make_icons.py` | regenerate `icons/ballsheet*.png` |
