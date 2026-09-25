# Mario Party Horse Racing

A Discord bot and OBS overlay for running a fake-currency betting broadcast
around Mario Party CPU races. The bot keeps everyone's points, takes their
guesses, runs the rundown, counts ? tiles and minigames live, and pays out.
The overlay puts the scorebug, the board, bonus questions, the crew's slips
and the fake ads on stream. Both read the same SQLite file, so what's on
screen never disagrees with what the bot just said in chat.

Nothing here touches real money.

## Setup

**SETUP.md walks through everything from zero:** creating the Discord bot,
putting the code on GitHub, deploying on Railway with a volume so the season
survives redeploys, adding the overlay to OBS, backups, and what to check
when something's wrong.

The settings, all set as Railway variables:

| Variable | Needed | What it does |
|---|---|---|
| `DISCORD_TOKEN` | yes | The bot's token from the Discord developer portal |
| `MPR_GUILD_ID` | yes | Your server's ID. Commands appear instantly, and the bot ignores other servers |
| `MPR_OVERLAY_KEY` | recommended | Overlay pages need `?key=` this value, so only your OBS can read them |
| `MPR_STAFF_ROLE` | no | Crew role name, default `Race Staff` |
| `MPR_DB_PATH` | no | Override where the season is stored. On Railway, an attached volume is used automatically |

The overlay pages, each loaded in OBS as
`https://<your railway domain>/<page>?key=<your key>`:

| Page | Size | What it is |
|---|---|---|
| `bug` | 520x440 | Corner scorebug: segment clock, turn count, live tallies |
| `bonus` | 1280x460 | Mid-race bonus question, appears only while one runs |
| `tote` | 1920x400 | Lower-third board, cycles through each finishing place and the props |
| `casters` | 1100x460 | The crew's own slips |
| `ads` | 1280x260 | Rotating fake ad slot |
| `standings` | 900x700 | Season leaderboard, for the wrap-up |

## How it pays

Everyone starts with 100 points and can bet as much of it as they like.

**Finishing order.** `/bet` takes a guess at the whole order, 1st to last,
and one stake. The stake comes back multiplied by how many places were right:

| Places right | Comes back as | A random guess does this |
|---|---|---|
| 4 | stake × 4 | 4% |
| 2 | stake × 2 | 25% |
| 1 | stake back | 33% |
| 0 | nothing | 38% |

Spend 100 and get all four right, you have 400. Spend 50, you have 200.
Three right can't happen with four runners: if three are right, the fourth
is forced.

**Props.** `/prop` on most minigames won, most coins at the end, or most ?
tiles. Pays 2x. Minigames goes to whoever won the most minigames, counted
live on the control panel. Coins goes to whoever holds the most coins when
the race ends. The game's own bonus stars don't settle either one, even
where the game names a star the same thing. A tie refunds the bet.

**Bonus questions** run during the race; see below.

### What to expect from these numbers

A random guess at the order comes out exactly even on average, at any field
size. The placement market doesn't create or destroy points by itself; the
leaderboard moves on who reads the race better and who sizes their bets
well.

Props at 2x on a one-in-four pick return half the stake on average. They're
where points leak out of the economy. That fits "fun and risky"; if they
start to feel like a tax, raise `PROP_MULTIPLIER` to 4 for even odds.

Going all in is allowed, and 38% of guesses get nothing right, so expect
roughly a third of anyone who shoves everything to be broke after a week.
`/railmoney` tops anyone under 100 back up to 100 and pays nobody else. It
only runs when the crew runs it, so it's your call whether busted players
sit out until the next reset or come back next week.

## Seasons and /reset

The server host runs `/reset name:"Season 2"` to start over: every wallet
goes back to 100, and the old table is saved first so `/season hall` shows
past winners. It asks for a second click before doing anything, only the
person who ran it can confirm, and it's hidden from everyone without Manage
Server. It won't run mid-race. Use it between seasons, or when switching
games.

The first `/race create` opens "Season 1" on its own.

## Run of show

    /race create week:"Week 4" runners:"Mario, Luigi, Peach, Yoshi"
    /show start template:standard
    /panel

`/show start` puts the scorebug on its first segment and opens betting,
because the first segment in `config/show.json` says to. From there the
producer's whole job is `/show next` at each break. The race segment locks
betting on its own and switches the scorebug from segment clock to turn
count. `premiere` is the week-one format with the rules block.

Post `/panel` in a crew-only channel. It's the control panel for the race:
one button per character for ? tiles, one for minigame wins, next and
previous turn, and undo. The message updates in place with the running
count. In a 2v2 or 1v3 minigame, tap every winner. A misclick is one tap on
Undo.

After the game:

    /race autograde                                  # ? tiles and minigames, from the tally
    /race call market:"Most coins at the end" winner:Peach
    /race result first:Mario second:Luigi third:Peach fourth:Yoshi
    /race finish

`/race autograde` settles both tallied props from the live count and voids a
tie rather than guessing. `/race result` pays every order guess the moment
it's entered. `/race finish` refuses to run until everything is settled,
then posts the finish, who came out ahead, and the standings.

If something goes wrong, `/race void` refunds a market, the finishing order
included. Voiding is always safer than guessing.

## Mario Kart and other games

`/race create` takes 2 to 12 runners:

    /race create week:"MK 1" game:"Mario Kart" turns:3 props:false
        runners:"Mario, Luigi, Peach, Toad, Yoshi, DK, Wario, Bowser, Daisy, Rosalina, Koopa, Shy Guy"

The rule doesn't change: the stake times the places you got right, so a
perfect 12-racer guess pays 12x. `props:false` drops the Mario Party side
bets, and the control panel then shows only the turn buttons (use them as
laps). The board shows the six runners with the most money on each place so
it stays readable.

## Bonus questions

The pre-show window is where betting happens, which leaves ninety minutes of
race with nothing for viewers to do. Bonus questions fill that gap.

    /bonus open question:"Who wins the next minigame?" seconds:60

That posts a message with one button per answer. Viewers tap one, type a
stake, done. The bonus band slides onto the stream with a countdown and the
live split, locks itself when the clock runs out, and the Discord post greys
its buttons at the same moment. Then:

    /bonus call market:"Who wins the next minigame?" winner:Luigi

It pays immediately, and the band shows the winner for 25 seconds before
hiding again.

Answers default to tonight's four characters. For anything else, pass them:
`options:"Yes, No"`. Pricing defaults to fair odds, so a four-way pick pays
4x and a yes/no pays 2x. Override with `pays:` to make one juicier.

A few that work well: who wins the next minigame, will anyone steal a star
this turn, who's in last after turn 20, does the leader change in the last
five turns.

## Fake ads

`config/ads.json` holds the rotation. Edit it between shows and the overlay
picks it up on its next poll, no restart. Each slot needs a headline; body, a
corner tag, an accent colour, and a weight are optional. Drop an image in
`config/ads/` and reference it by filename to use artwork instead of text,
which is the path for community-made ads.

## Tuning

Every dial is at the top of `mpr/economy.py`:

| Setting | Default | Effect |
|---|---|---|
| `PLACEMENT_LADDER` | 1 / 2 / 4 | What 1, 2 and 4 places right pay; unlisted counts pay their own number |
| `PROP_MULTIPLIER` | 2 | Props; 4 would be even odds |
| `STARTING_BALANCE` | 100 | What every wallet starts with, and resets to |
| `RAIL_FLOOR` | 100 | What `/railmoney` tops the broke up to |
| `MAX_WAGER` | none | Set a number to stop anyone betting more than that at once |
| `MIN_WAGER` | 1 | Smallest bet |
| `SEASON_LENGTH` | 10 | Broadcasts per season, for the progress readout |

Rerun the tests after changing any of it.

## Commands

| Players | |
|---|---|
| `/bet` | Guess the whole finishing order and stake points on it |
| `/prop` | Side bet: most minigames, most coins, most ? tiles |
| `/payouts` | How it pays, worth pinning |
| `/wallet`, `/mybets` | Balance, and what you have riding tonight |
| `/board`, `/leaderboard` | Where the money is, and the standings |
| `/season status`, `/season hall` | How far into the season, past winners |

| Crew | |
|---|---|
| `/race create`, `/race finish` | Build and close a race night |
| `/show start`, `/show next`, `/show back`, `/show rundown` | Run the segments |
| `/panel` | Tally and turn buttons for the race |
| `/bonus open`, `/bonus call`, `/bonus void` | Mid-race questions |
| `/race result`, `/race autograde`, `/race call`, `/race void` | Settle the markets |
| `/race open`, `/race lock` | Manual override of the betting window |
| `/tally` | Manual override of a count |
| `/railmoney` | Top up anyone who's broke |
| `/feature` | Put someone's slip on the layout |

| Server host | |
|---|---|
| `/reset` | Wipe every wallet back to 100 and start a new season |
| `/backup` | Download a complete copy of the database |

## Checking it works

    pip install -r requirements-dev.txt
    python -m pytest tests -q
    python tools/check_overlays.py

The tests play a complete broadcast night through the real command code,
using discord.py's own permission checks and invocation path: a season, the
rundown, full-order guesses and props, the control panel through all 35 turns
with a misclick and an undo, a bonus question from open to payout, autograde,
the finishing order, a 12-racer Mario Kart night, and a host-only reset. Every wallet is checked against a hand-computed balance and
against the ledger. The overlay check loads every page in a real browser in
each state it will hit on the night and fails on any script error.

What they can't cover is the connection to Discord itself. Before the first
real broadcast, spend five minutes on a test server with a scratch database:

1. Start the bot with `MPR_DB_PATH` pointed at a throwaway file.
2. Confirm the slash commands appear. If not, check `MPR_GUILD_ID`.
3. Run a race through `/show start`, a couple of bets, and `/panel`.
4. Tap some panel buttons and confirm the message updates.
5. Open a 15-second bonus, tap an answer, place a stake, let it close.
6. Restart the bot, then tap the old panel. It should still work.
7. Open every overlay source in OBS once.

## Files

    mpr/economy.py         pricing, grading, ladder, no dependencies
    mpr/db.py              SQLite schema and queries
    mpr/bot.py             slash commands, control panel, bonus buttons
    mpr/overlay.py         FastAPI server for the browser sources
    railway.json           Railway build and start settings
    .python-version        Python 3.12, the version everything is tested on
    mpr/web/               the six overlay pages
    config/show.json       run-of-show templates
    config/ads.json        ad rotation, safe to hand to the community
    config/ads/            ad artwork
    tests/                 unit tests and the broadcast-night run-through
    tools/check_overlays.py  browser check for every overlay page
    data/                  SQLite file, created on first run

Run `/backup` after each broadcast and keep the file. It is the season.
