# Moderating Mario Party Horse Racing

You don't need to install anything. The bot already runs online. All you
need is Discord, and the server host giving you the **Race Staff** role.
Once you have it, press Ctrl+R to reload Discord, and the crew commands
appear when you type `/`. Type `/help` any time for this checklist.

Work in the private crew channel. Players can't see what you do there.


## Show night: four commands

**1. Before the show: `/race create`**

Pick the mode, type the week, and list the runners separated by commas.

- Mario Party: exactly 4, like `Mario, Luigi, Peach, Yoshi`
- Mario Kart: 2 to 12 racers

You can do this hours early. Nothing opens until step 2.

**2. When you go live: `/show start`**

Pick `standard`, or `premiere` for the very first show with the rules
segment. Betting opens and the stream's scorebug starts its clock.

**3. At each break: `/show next`**

When the race segment starts, betting locks and a control panel appears in
the channel. During the race, tap the panel:

- "? Mario", "? Luigi" and so on, every time a character lands on a ? space
- "Won: ..." for each minigame winner (in 2 v 2 or 1 v 3, tap every winner)
- "Next turn" at the start of each turn
- "Undo last" if you tap the wrong one

The tap counts decide the ? tiles and minigames bets, so keep up with them.
The scorebug on stream updates within a couple of seconds.

**Optional, during the race: bonus questions**

`/bonus open` with a question, like "Who wins the next minigame?". Players
tap an answer on the post before the timer runs out. When it's decided,
`/bonus call` with the answer.

**4. After the race: `/race result`**

Enter the finishing order, and in `coins`, whoever had the most coins at the
end (or Tie). That one command settles every bet, closes the night, and posts
the results and standings. Cut to the wrap-up scene and the winners reveal
plays on stream.

If it refuses, it tells you why, and nothing has been paid. Fix it and run
it again. The usual reason is a bonus question you haven't called yet.


## Fixing mistakes

Players can take their own bets back with `/cancel` until betting locks, so
point anyone with a wrong bet there. After betting locks, bets are final.


| Problem | Fix |
|---|---|
| Tapped the wrong button on the panel | "Undo last" |
| A count went badly wrong | `/tally` to adjust it by hand |
| A bet market can't be settled fairly | `/race void` refunds everyone on it |
| A bonus question went wrong | `/bonus void` refunds it |
| The panel scrolled away | `/panel` posts a new one |
| Opened betting too early or late | `/race open` and `/race lock` |
| Someone's out of points | `/railmoney` tops up anyone below 100 |


## Other crew commands

- `/review-ads`: approve or reject fake ads players send in. Anything you
  approve shows on stream, so read it first.
- `/feature`: put a caster's bets on the stream layout.

`/reset`, `/gift`, `/backup` and `/overlay-links` belong to the server host
and won't show for you.
