# Setup, step by step

Set aside about an hour for the first time through. The steps are numbered
straight through, so if something doesn't match what you see, tell me the
step number.

Keep a notepad open. Along the way you'll collect four things to paste into
Railway and OBS later:

| Label it | What it is | You get it in |
|---|---|---|
| `DISCORD_TOKEN` | The bot's password | Step 6 |
| `MPR_GUILD_ID` | Your server's ID number | Step 15 |
| `MPR_OVERLAY_KEY` | A random string you make up | Step 20 |
| Railway domain | The web address of your overlay | Step 45 |

Treat the token like a password. Never paste it into Discord or anywhere
public.


## Part 1: Create the bot in Discord's developer portal

1. In a web browser, go to https://discord.com/developers/applications and
   log in with your Discord account.

2. Click "New Application" in the top right.

3. Type a name. Viewers will see this name, so pick something like "Horse
   Racing". Tick the box agreeing to Discord's terms, then click "Create".

4. In the left sidebar, click "Bot".

5. Under the Token heading, click "Reset Token". Confirm, and enter your
   two-factor code if Discord asks for one.

6. Click "Copy" and paste the token into your notepad as `DISCORD_TOKEN`.
   Discord only shows it once. If you lose it, come back and reset it again.

7. Scroll down to "Privileged Gateway Intents" and leave all three switched
   off. The bot doesn't need them.

8. In the left sidebar, click "Installation".

9. Under "Installation Contexts", tick "Guild Install" and untick "User
   Install".

10. Under "Install Link", choose "Discord Provided Link" from the dropdown.

11. Under "Default Install Settings", in the Guild Install box:
    - In Scopes, add `applications.commands` and `bot`.
    - A Permissions box appears once `bot` is added. Add View Channels, Send
      Messages, Embed Links, and Read Message History.

12. Click "Save Changes" at the bottom.

13. Copy the link under "Install Link", paste it into a new browser tab, pick
    your server from the dropdown, click "Continue", then "Authorize".

    Check: the bot now appears in your server's member list, shown as
    offline. That's correct. It comes online once Railway runs it.


## Part 2: Set up your Discord server

14. Turn on Developer Mode. In the Discord app, click the gear icon next to
    your name at the bottom left. Go to "Advanced" and switch on "Developer
    Mode". Close settings.

15. Right-click your server's icon in the far-left column and click "Copy
    Server ID". Paste it into your notepad as `MPR_GUILD_ID`. It's a long
    number, around 18 or 19 digits.

16. Create the crew role. Click your server's name at the top left, then
    "Server Settings", then "Roles", then "Create Role". Name it exactly
    `Race Staff`, with a capital R, a capital S and one space. Open its
    "Permissions" tab and switch on "Manage Events". Click "Save Changes".

    Manage Events is what lets the crew see the crew commands. Discord hides
    them from everyone without it, so players only see the commands meant for
    them. On its own it just lets someone edit the server's scheduled events.

17. Give the role to the crew. Right-click each person in the member list,
    choose "Roles", and tick Race Staff. Do this for yourself, JaeAIK, and
    anyone who will run the control panel.

18. Make a private crew channel. Click the "+" next to a channel category,
    choose "Text", and name it something like `race-control`. Switch on
    "Private Channel", click "Next", add the Race Staff role and the bot,
    then click "Create Channel".

19. Make a public channel for viewers, something like `place-your-bets`.


## Part 3: Make your overlay key

20. Make up a long random string of letters and numbers, with no symbols or
    spaces. If you have Python, this makes one:

    ```
    python -c "import secrets; print(secrets.token_hex(16))"
    ```

    A password manager also works, if you switch symbols off and use at least
    24 characters. Paste it into your notepad as `MPR_OVERLAY_KEY`.


## Part 4: Put the code on GitHub

Install Git (one time only)

21. Download Git from https://git-scm.com/downloads and run the installer.
    Clicking "Next" through every screen is fine.

22. Open a new terminal. On Windows, click Start and type "Terminal" or
    "PowerShell". Type `git --version` and press Enter. It should print a
    version number.

23. Tell Git who you are. Use the email on your GitHub account:

    ```
    git config --global user.name "Jon"
    git config --global user.email "you@example.com"
    ```

Unzip the project

24. Right-click `mario-party-horse-racing.zip`, choose "Extract All", then
    "Extract".

25. Open folders until you see `railway.json`, `README.md`, `SETUP.md` and
    the `mpr` folder side by side. Windows often creates a folder inside a
    folder with the same name. The inner one is the one you want.

26. Open a terminal in that folder. On Windows 11, right-click an empty spot
    inside it and choose "Open in Terminal". On Windows 10, hold Shift,
    right-click, and choose "Open PowerShell window here". Type `dir` and
    press Enter. If `railway.json` is in the list, you're in the right place.

Create the repository on GitHub

27. Go to github.com and sign in. Click the "+" in the top right, then "New
    repository".

28. Name it something like `mario-party-horse-racing` and choose "Private".
    Leave "Add a README file" unticked, and leave .gitignore and license on
    None. Click "Create repository".

29. GitHub shows a setup page. Copy the HTTPS address near the top. It ends
    in `.git`.

Send the code up

30. Back in your terminal, run these one line at a time. Paste your address
    from step 29 where it says `PASTE-URL-HERE`:

    ```
    git init
    git add .
    git commit -m "Mario Party Horse Racing"
    git branch -M main
    git remote add origin PASTE-URL-HERE
    git push -u origin main
    ```

31. On your first push, a window asks you to sign in to GitHub. Choose "Sign
    in with your browser" and approve it.

    On a Mac, the terminal may ask for a password that GitHub won't accept.
    If that happens, install GitHub Desktop instead. Use File, then "Add
    Local Repository", pick the folder from step 25, let it create a
    repository, then click "Publish repository" with "Keep this code
    private" ticked.

32. Refresh your repository page on GitHub.

    Check: you should see `.gitignore`, `.python-version`, `railway.json`,
    `requirements.txt`, `README.md` and `SETUP.md`, plus the folders
    `config`, `mpr`, `previews`, `tests` and `tools`. There should be no
    `data` folder.

    If instead you see one folder called `mario-party-horse-racing`, you ran
    step 30 one folder too high. Delete the repository on GitHub (its
    Settings page, then "Delete this repository" at the very bottom), then
    redo steps 26 to 32 from the inner folder.


## Part 5: Deploy on Railway

33. Go to railway.com, log in, and click "New Project".

34. Choose "Deploy from GitHub repo".

35. The first time, Railway asks to connect to GitHub. Choose "Only select
    repositories", pick your repository, and approve. Back in Railway, pick
    the repository from the list.

36. Railway creates a service, a box on the canvas named after your
    repository, and starts building. If it offers to add variables before
    deploying, skip that for now.

37. Wait a couple of minutes for the build. The first deploy will fail or
    crash, because the bot has no token yet. That's expected.

Add the volume. Don't skip this: it's where the season is stored.

38. Right-click an empty part of the canvas, around the service box, and
    choose the option to add a volume. It may be labeled "Volume" or "New
    Volume". You can also press Ctrl+K (⌘K on a Mac) and type "volume".

39. When asked which service, pick your bot's service.

40. Set the mount path to exactly `/app/data` and confirm. The volume now
    shows as attached to the service.

Add the three values from your notepad

41. Click the service box, then the "Variables" tab.

42. Click "New Variable" and add each of these. Alternatively, open the "Raw
    Editor" and paste all three lines at once:

    ```
    DISCORD_TOKEN=your token
    MPR_GUILD_ID=your server id
    MPR_OVERLAY_KEY=your overlay key
    ```

    Don't use quotes, and don't put spaces around the `=`.

43. Railway shows a bar saying there are changes to apply. Click "Deploy".

Give the overlay a web address

44. Still on the service, open the "Settings" tab. Scroll to "Networking",
    and under Public Networking click "Generate Domain".

45. If it asks for a port, type `8080` and confirm. Copy the address it
    gives you, something like
    `mario-party-horse-racing-production.up.railway.app`, into your notepad.

Read the logs

46. Open the "Deployments" tab, click the newest deployment, and choose
    "Deploy Logs" (not Build Logs).

47. A healthy start has these three lines near the bottom:

    ```
    Logged in as Horse Racing#1234. In 1 server(s).
    Overlay ready. OBS source for the board: https://...up.railway.app/tote?key=...
    Database: /app/data/horserace.db
    ```

    Check: the bot now shows as online in your server's member list.

48. If you see something else instead:

    | Log says | Fix |
    |---|---|
    | NO VOLUME ATTACHED | Redo steps 38 to 40, then redeploy |
    | Database path isn't `/app/data/...` | The volume's mount path is wrong; set it to `/app/data` |
    | Couldn't register slash commands | `MPR_GUILD_ID` is wrong, or step 13 wasn't done |
    | DISCORD_TOKEN isn't set, or rejected | Recheck the variable; reset the token (step 5) if needed |
    | Overlay address shows 127.0.0.1 | Harmless. The domain was made after this deploy began; the next deploy shows it |

    To redeploy after a fix: Deployments tab, the three-dot menu on the
    newest deployment, then "Redeploy".


## Part 6: Test run, and set up OBS while it's running

How slash commands work: type `/` followed by the command name, and Discord
shows a box for each option. Press Tab to move between boxes. For character
names, pick from the suggestions that pop up.

49. In your crew channel, type `/payouts` and press Enter. The bot replies
    with the rules card. If no commands appear, press Ctrl+R to reload
    Discord and wait a minute.

50. Run `/reset` with name `Test`, and click "Reset everything". This opens a
    test season, so your trial run doesn't end up in the real season's
    history.

51. Run `/race create` with mode "Mario Party", week `Test`, and runners
    `Mario, Luigi, Peach, Yoshi`. The bot confirms "Test is built".

52. Run `/show start` with template `standard`. The bot says Pre-show, and
    that betting is now open.

53. Run `/bet` with amount `10` and first `Mario`, second `Luigi`, third
    `Peach`, fourth `Yoshi`. You get a private reply ending "You have 90
    points left".

54. Run `/sidebet` with market "Most coins at the end", pick `Peach`, amount
    `5`.

Check the overlay in a normal browser first

55. In your browser, open `https://YOUR-DOMAIN/tote?key=YOUR-KEY`, using the
    address from step 45 and the key from step 20. You should see the board
    for "Test". Swap `tote` for `bug` and you should see the scorebug.

    "Missing or wrong overlay key" means the key in the address doesn't match
    Railway's. A blank page means there's no race, so check step 51.

Add the sources in OBS

56. In OBS, in the Sources panel, click "+", choose "Browser", pick "Create
    new", name it something like "MPR Tote", and click OK.

57. Set the URL to `https://YOUR-DOMAIN/tote?key=YOUR-KEY`, width `1920` and
    height `400`. Leave everything else as it is and click OK.

58. Drag it into position. Repeat steps 56 and 57 for the rest:

    | Page | Width | Height | Usually goes |
    |---|---|---|---|
    | `bug` | 520 | 440 | A top corner |
    | `bonus` | 1280 | 460 | Lower middle |
    | `casters` | 1100 | 460 | Beside the casters' cams |
    | `ads` | 1280 | 260 | Wherever you run ad breaks |
    | `standings` | 900 | 700 | A separate wrap-up scene |
    | `winners` | 1920 | 1080 | Full screen in the wrap-up scene. Tick "Refresh browser when scene becomes active" so the reveal replays |

    Do this now, while the test race is live. The pages hide themselves when
    there's no race, so otherwise there'd be nothing to position.

    The Casters source stays empty until someone is featured. To see it, run
    `/feature` with user set to yourself. Your slip from steps 53 and 54
    appears within a few seconds. `/feature` with show set to False takes
    you off again.

Try the race tools

59. Run `/show next` twice. The second one locks betting, switches the
    scorebug to the turn count, and posts the control panel in the channel.

60. On the control panel, tap "? Peach" a few times, "Won: Yoshi" once, and
    "Next turn" once. The panel updates straight away, and the scorebug in
    OBS within a couple of seconds. Tap "Undo last" once.

61. Test that a restart doesn't break anything. In Railway, open the
    Deployments tab, use the three-dot menu on the newest deployment, and
    choose "Restart". Wait for "Logged in" in the logs, then tap "Next turn"
    on the same panel. It should still work.

62. Run `/bonus bet` with question `Test bonus`, type "Pick a character", and
    timer "60 seconds". A post with answer buttons appears in Discord, and the
    bonus band slides up in OBS. Tap "Mario", type `5`, and submit.

63. Tap "Crew: Pay out" on the post and pick `Mario`. Everyone who backed
    Mario is paid, and the post shows the result.

64. Run `/race result` with first `Mario`, second `Luigi`, third `Peach`,
    fourth `Yoshi`, and coins `Peach`. That one command settles everything:
    ? tiles and minigames from the panel counts, the coins prop, and every
    guess. It closes the night and posts one card with the results and the
    standings. It also saves a backup on the Railway volume.

65. Run `/backup`. You get a file that only you can see. Download it to check
    that backups work.


## Part 7: Start the real season

66. Run `/reset` with name `Season 1`, and click "Reset everything". Everyone
    goes back to 1,000 points, and the test is filed away in `/season hall`.

67. You're live. Show night is four commands: `/race create`, `/show start`,
    `/show next` at each break, and `/race result` at the end. Crew can type
    `/help` in Discord for the checklist. To bring in another moderator, see
    MODERATOR.md.

One rule once you're running: don't push code or config changes during a
live show, because every push restarts the bot for about a minute. Every
finished night is saved automatically on the volume (the newest 20 are
kept). Run `/backup` now and then for a copy that lives off Railway.


## Week to week

The README has the run of show. The one rule for Railway: **don't push code
or config changes during a live show.** Every push redeploys, and the bot is
offline for about a minute while it restarts.

Ads are managed in Discord with `/ad submit`, `/review-ads` and `/ad remove`, and
change on stream within seconds. See the README's "Fake ads" section.

To change the rundown, edit `config/show.json` on GitHub (the web editor is
fine) and commit. Railway redeploys on its own, and the season is untouched
because it lives on the volume.

Every finished night saves a copy of the database in `backups/` on the
volume, keeping the newest 20, which covers a bad night or a mistaken
`/reset`. Run `/backup` now and then for a copy that lives off Railway.

To restore a backup, which you should rarely need: when no show is running,
install the Railway CLI, then run `railway login`, `railway link`, and
`railway volume browse`. Delete `horserace.db`, `horserace.db-wal` and
`horserace.db-shm`, upload your backup as `horserace.db`, and restart the
service.


## When something's wrong

**Commands don't show up in Discord.** Look in the Railway logs for
"Couldn't register slash commands". It almost always means `MPR_GUILD_ID` is
wrong or the bot was never added with the install link. Then reload Discord
with Ctrl+R.

**"The application did not respond."** The bot is offline or restarting.
Check the Railway logs.

**The crew can't see or use crew commands.** The role needs "Manage Events"
switched on (step 16) to see them, and it must be named exactly `Race Staff`
to use them. To use a different name, set `MPR_STAFF_ROLE` in Railway to
match. Server admins always see everything.

**The leaderboard emptied after a deploy.** No volume was attached, so the
season was stored inside the container. Attach one at `/app/data`
(section 3.2). Anything from before is gone unless you have a `/backup`.

**The build fails while installing Python.** Redeploy once; it's usually
temporary. If it keeps failing, change `"RAILPACK"` to `"NIXPACKS"` in
`railway.json` and push.

**The bot logs "Discord rejected DISCORD_TOKEN".** The token was reset or
mistyped. Reset it on the Bot page and paste the new one into Railway.


## Optional: run it on your own PC

This is useful for trying changes before they go live. You need Python 3.12.

```
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # macOS or Linux
pip install -r requirements.txt
```

Set the same three variables in the terminal. In PowerShell:

```
$env:DISCORD_TOKEN="..."; $env:MPR_GUILD_ID="..."; $env:MPR_DB_PATH="scratch.db"
python -m mpr.bot
```

On macOS or Linux, use `export DISCORD_TOKEN=...` and so on. The overlay is
then at `http://127.0.0.1:8730/tote`.

Stop the Railway service first. Two copies of the bot running on the same
token will both try to answer every command.

To run the automated checks:

```
pip install -r requirements-dev.txt
python -m pytest tests -q
python tools/check_overlays.py
```
