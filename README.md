# Mario Party Horse Racing

A Discord bot and OBS overlay for running a fake-currency betting broadcast
around Mario Party CPU races. The bot keeps everyone's points, takes their
guesses, runs the rundown, counts ? tiles and minigames live, and pays out.
The overlay puts the scorebug, the board, bonus questions, the crew's slips
and the fake ads on stream. Both read the same SQLite file, so what's on
screen never disagrees with what the bot just said in chat.

Nothing here touches real money.

## Setup

**Bringing in a moderator?** Nothing to install: give them the Race Staff
role and send them MODERATOR.md. If they'll also run the stream, `/overlay-links`
gives you the OBS addresses to send them privately.

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
| `winners` | 1920x1080 | Results reveal for the wrap-up: final order, biggest winners counted up, perfect cards |

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

## Gifting points

`/gift` lets the server host give someone points, for a contest prize or a
make-good. It's announced in the channel, so the leaderboard stays
trustworthy, and every gift is logged in the database with who gave it and
why. A negative amount takes points back, to fix a mistake; a balance never
goes below zero. `/status` shows gifts on their own line, so they're never
mistaken for winnings, and `/reset` clears them with everything else.

## Seasons and /reset

The server host runs `/reset name:"Season 2"` to start over: every wallet
goes back to 100, and the old table is saved first so `/season hall` shows
past winners. It asks for a second click before doing anything, only the
person who ran it can confirm, and it's hidden from everyone without Manage
Server. It won't run mid-race. Use it between seasons, or when switching
games.

The first `/race create` opens "Season 1" on its own.

## Run of show

Show night is four commands. Crew can type `/help` in Discord for this
checklist, and MODERATOR.md is a one-page guide to hand a new moderator.

    /race create mode:"Mario Party" week:"Week 4" runners:"Mario, Luigi, Peach, Yoshi"
    /show start template:standard
    /show next                        (at each break)
    /race result first:Mario second:Luigi third:Peach fourth:Yoshi coins:Peach

`/race create` can happen any time before the show. `/show start` puts the
scorebug on its first segment and opens betting. From there, `/show next`
moves through the rundown in `config/show.json`. When it reaches the race
segment, it locks betting, switches the scorebug to the turn count, and posts
the control panel in the channel: one button per character for ? tiles and
minigame wins, next and previous turn, and undo. In a 2 v 2 or 1 v 3
minigame, tap every winner.

`/race result` is the whole post-game in one command. It settles the ? tiles
and minigames bets from the panel counts (a tie refunds), the coins bet from
the `coins` option (pick Tie for a tie), and every order guess. It then closes
the night, posts one card with the results and standings, and saves a backup
on the volume. It checks everything before paying anything, so a refusal
(usually an unsettled bonus question) leaves the night untouched: fix it and
run it again. Mario Kart races have no props, so no `coins` option.

The step-by-step commands are still there as manual overrides: `/panel` to
repost the control panel, `/race call` and `/race autograde` for props,
`/race void` to refund a market, `/race finish` to close a night settled by
hand, and `/race open` or `/race lock` for the betting window.

## Game modes: Mario Party and Mario Kart

`/race create` starts by asking for the mode, and the mode decides what
everyone sees for the rest of the night:

| | Mario Party | Mario Kart |
|---|---|---|
| Runners | exactly 4 | 2 to 12 |
| `/bet` and `/race result` show | 4 places, all required | 12 places, first two required |
| Props (minigames, coins, ? tiles) | yes | no |
| Counted in | turns, 35 by default | laps, 3 by default |
| Control panel | tally buttons, next and previous turn, undo | next and previous lap, undo |

Discord fixes a command's options when it's registered, so the bot keeps a
four-place and a twelve-place version of `/bet` and `/race result` and swaps
which one your server sees when the mode changes. The swap reaches Discord in
about a second. Anyone whose Discord hasn't caught up gets told to press
Ctrl+R. If the bot restarts mid-race, it comes back in that race's mode.

    /race create mode:"Mario Kart" week:"MK 1"
        runners:"Mario, Luigi, Peach, Toad, Yoshi, DK, Wario, Bowser, Daisy, Rosalina, Koopa, Shy Guy"

For an eight-racer game like Mario Kart 64, list eight runners; a perfect
guess then pays 8x. Use `game:` to put a specific title on the board, like
"Mario Kart 64" or "Mario Party 3".

The payout rule is the same in both: stake times places right. The tote
board shows the six runners with the most money on each place so it stays
readable with a full field.

## On-stream extras

**Form guide.** Every runner's last five finishes in the same mode, latest
first, show beside their name on the tote board, with wins lit. `/form` and
`/board` show the full record: finishes, wins, starts and average. Names match
ignoring capitals, Mario Kart form never mixes with Mario Party form, and a
runner with no history shows as "debut".

**Name callouts.** An all-in, or a bet of at least half someone's stack, gets
announced publicly in the channel and pops up on the scorebug for six seconds:
"ana just went ALL IN: 100 on Mario to win!". The bet slip itself stays
private. Bets under 20 points are never called out, and a cancelled bet takes
its callout with it. The thresholds are `CALLOUT_FRACTION` and `CALLOUT_MIN`
in `mpr/economy.py`.

**Winners reveal.** Add the `winners` page full screen in your wrap-up scene.
After `/race result`, it shows the final order, then counts up the night's
biggest winners from fifth to first, across every bet they made, and closes on
a flickering banner if anyone called the whole order. In OBS, tick "Refresh
browser when scene becomes active" and the reveal replays every time you cut
to that scene. It stays hidden while a race is running.

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

The ad rotation is managed from Discord, and changes show on stream within a
few seconds. There's no redeploy, so it's safe mid-show.

| Command | Who | What it does |
|---|---|---|
| `/ad add` | Anyone | A headline, an optional line under it, and optionally an uploaded image |
| `/review-ads` | Crew | Shows the oldest waiting ad, image included, with Approve and Reject buttons |
| `/ad remove` | Anyone | Crew can remove any ad; everyone else can remove their own |
| `/ad list` | Anyone | Crew see the rotation and the queue; everyone else sees their own |

Crew ads go live straight away, and crew can set the corner tag and a weight
from 1 to 10 for how often it comes up. Anyone else's ad waits for approval,
because whatever's approved goes out on your stream. Their corner tag always
reads "made by" and their name, their weight is 1, and each person can have
at most three waiting at once. The bot announces submissions in the channel,
so the crew sees them come in.

Images must be PNG, JPG, GIF or WebP, up to 4 MB, and are checked by their
actual contents rather than the file name. They look best at about 1170 × 170
pixels; other shapes are cropped to fit. Images are saved inside the database,
so `/backup` includes them and they never stop loading.

`config/ads.json` only supplied the starting ads, on the first run. After that
it's ignored, apart from `dwell_seconds`, which sets how long each ad stays up.
Remove the sample ads with `/ad remove` once you have your own.

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

There are three groups. Players see only the player commands; Discord hides
the rest. The crew sees crew commands because the Race Staff role has
"Manage Events" switched on (SETUP.md step 16), and the host sees
everything. Every crew command also checks for the Race Staff role when it
runs, so hiding them is never the only lock.

| Players | |
|---|---|
| `/help` | What you can do; crew also see the show-night checklist |
| `/status` | Your points, your place, and how tonight's bets are doing |
| `/bet` | Guess the whole finishing order and stake points on it |
| `/prop` | Side bet: most minigames, most coins, most ? tiles |
| `/cancel` | Take a bet back, full refund, until betting locks. To change a bet, cancel and bet again |
| `/form` | How tonight's runners have finished in past races |
| `/payouts` | How it pays, worth pinning |
| `/wallet`, `/mybets` | Just the balance, or just tonight's bets |
| `/board`, `/leaderboard` | Where the money is, and the standings |
| `/season status`, `/season hall` | How far into the season, past winners |
| `/ad add`, `/ad list`, `/ad remove` | Send in a fake ad for the stream |

| Crew | |
|---|---|
| `/race create`, `/race result` | Build a race night, and settle and close it in one go |
| `/show start`, `/show next`, `/show back`, `/show rundown` | Run the segments |
| `/panel` | Repost the control panel (it posts itself when the race starts) |
| `/bonus open`, `/bonus call`, `/bonus void` | Mid-race questions |
| `/race autograde`, `/race call`, `/race void`, `/race finish` | Manual overrides for settling |
| `/race open`, `/race lock` | Manual override of the betting window |
| `/tally` | Manual override of a count |
| `/railmoney` | Top up anyone who's broke |
| `/feature` | Put someone's slip on the layout |
| `/review-ads` | Approve or reject ads people send in |

| Server host | |
|---|---|
| `/reset` | Wipe every wallet back to 100 and start a new season |
| `/gift` | Give someone points, or take some back with a negative amount |
| `/overlay-links` | The six OBS addresses and sizes, to send to whoever streams |
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
3. Run a race through `/show start`, a couple of bets, and `/show next` into the race.
4. Tap some panel buttons and confirm the message updates.
5. Open a 15-second bonus, tap an answer, place a stake, let it close.
6. Restart the bot, then tap the old panel. It should still work.
7. Open every overlay source in OBS once.

## Files

    MODERATOR.md           one-page guide for a new moderator
    mpr/economy.py         pricing, grading, ladder, no dependencies
    mpr/db.py              SQLite schema and queries
    mpr/bot.py             slash commands, control panel, bonus buttons
    mpr/overlay.py         FastAPI server for the browser sources
    railway.json           Railway build and start settings
    .python-version        Python 3.12, the version everything is tested on
    mpr/web/               the six overlay pages
    config/show.json       run-of-show templates
    mpr/ads.py             rules for community ads: text limits, image checks
    config/ads.json        the starting ads, imported once on first run
    tests/                 unit tests and the broadcast-night run-through
    tools/check_overlays.py  browser check for every overlay page
    data/                  SQLite file, created on first run

Run `/backup` after each broadcast and keep the file. It is the season.
