# Setting it up

By the end of this you'll have the bot running around the clock on Railway,
the season stored on a Railway volume so redeploys never wipe it, and a
Railway web address your OBS loads the overlay from. First time through takes
about 45 minutes.

You need a Discord account with Manage Server on your server, a free GitHub
account, your Railway account, and Git (git-scm.com). The bot runs all the
time, so check that your Railway plan covers an always-on service.


## 1. Create the Discord bot

1. Go to https://discord.com/developers/applications, click **New
   Application**, name it (the name is what viewers will see), and create it.

2. Open the **Bot** page and click **Reset Token**. Copy the token somewhere
   safe, like a password manager. This is your `DISCORD_TOKEN`. Anyone who
   has it controls the bot, so never paste it into Discord or commit it to
   GitHub. If it ever leaks, reset it and update Railway.

   Leave the three Privileged Gateway Intents switched off. The bot doesn't
   need them.

3. Open the **Installation** page.
   - Under Installation Contexts, tick **Guild Install** and untick **User
     Install**.
   - Under Install Link, choose **Discord Provided Link**.
   - Under Default Install Settings for Guild Install, add the scopes
     `applications.commands` and `bot`, then the permissions View Channels,
     Send Messages, Embed Links and Read Message History. Save.

4. Copy the install link, open it in your browser, pick your server and
   authorize. The bot shows up in the member list, offline until Railway
   starts it.

5. Get your server ID. In Discord, open User Settings, then Advanced, and
   turn on Developer Mode. Right-click your server's icon and choose **Copy
   Server ID**. This is your `MPR_GUILD_ID`.

6. Set up the server:
   - Create a role named exactly `Race Staff` (Server Settings, Roles) and
     give it to JaeAIK and the crew. Crew commands check for this name.
   - Make a private channel for the crew. The control panel lives there.
   - Make a public betting channel for viewers.

   `/reset` and `/backup` are for whoever has Manage Server, and Discord hides
   them from everyone else.


## 2. Put the code on GitHub

1. Unzip the download.

2. On github.com, create a new repository. Make it **Private**, and don't let
   GitHub add a README.

3. In a terminal, from inside the unzipped folder:

   ```
   git init
   git add .
   git commit -m "Mario Party Horse Racing"
   git branch -M main
   git remote add origin https://github.com/YOUR-NAME/YOUR-REPO.git
   git push -u origin main
   ```

   Use Git rather than dragging files into GitHub's web page. The web
   uploader can skip files whose names start with a dot, and Railway needs
   `.python-version`.

4. Check the repository on GitHub. `railway.json`, `requirements.txt` and
   `.python-version` should sit at the top level, next to the `mpr` folder.
   There should be no `data` folder.


## 3. Deploy on Railway

1. On railway.com, create a **New Project**, choose **Deploy from GitHub
   repo**, and pick your repository. Let Railway into your GitHub if it asks.
   It starts building straight away. This first deploy will fail because the
   bot has no token yet. That's expected.

2. **Add the volume before anything else.** Right-click the project canvas
   (or press Ctrl+K / ⌘K) and choose **New Volume**. Attach it to the bot's
   service and set the mount path to `/app/data`.

   Without this step the season is stored inside the container and is wiped
   on every redeploy. The bot logs a loud warning if it starts without one.

3. Open the service's **Variables** tab and add:

   | Variable | Value |
   |---|---|
   | `DISCORD_TOKEN` | The token from step 1.2 |
   | `MPR_GUILD_ID` | Your server ID from step 1.5 |
   | `MPR_OVERLAY_KEY` | A long random string you make up, letters and numbers only |

   The overlay key stops strangers reading your overlay once it's on the
   public internet. You'll add it to each OBS source's address.

   Railway offers to deploy the change. Click **Deploy**.

4. Open the service's **Settings**, find **Networking**, and click **Generate
   Domain**. If it asks which port, enter `8080`. You get an address like
   `something.up.railway.app`. That's where OBS will load the overlay from.

5. Open the latest deployment and view its logs. A healthy start looks like:

   ```
   Logged in as Horse Racing#1234. In 1 server(s).
   Overlay ready. OBS source for the board: https://something.up.railway.app/tote?key=...
   Database: /app/data/horserace.db
   ```

   If the overlay line shows `127.0.0.1`, you generated the domain after that
   deploy began. The overlay still works; the next deploy prints the right
   address.


## 4. Test it in Discord before announcing anything

Type `/` in your server and the bot's commands should appear. If they don't,
press Ctrl+R to reload Discord.

Run a dry show in the crew channel:

1. `/race create week:Test runners:Mario, Luigi, Peach, Yoshi`
2. `/show start template:standard`
3. `/bet amount:10 first:Mario second:Luigi third:Peach fourth:Yoshi`
4. `/prop market:Most coins at the end pick:Peach amount:5`
5. `/panel`, tap a few buttons, then tap **Undo last**.
6. `/bonus open question:Test seconds:20`, tap an answer, enter a stake, and
   wait for it to close. Then `/bonus call` it.
7. `/race autograde`, `/race call` for coins, `/race result` with the order,
   then `/race finish`.
8. In Railway, restart the service. Once it's back, tap a button on the old
   control panel. It should still work.
9. `/backup`. You should get a file.
10. `/reset name:Season 1` and confirm. This wipes the test points so
    everyone starts the real season on 100.


## 5. Add the overlay to OBS

Add each as a **Browser** source. Use your Railway address and your overlay
key:

`https://something.up.railway.app/PAGE?key=YOUR-KEY`

| Page | Width | Height | What it is |
|---|---|---|---|
| `bug` | 520 | 440 | Corner scorebug: segment clock, turn count, live tallies |
| `bonus` | 1280 | 460 | Bonus question band; hides itself when none is running |
| `tote` | 1920 | 400 | Lower-third board |
| `casters` | 1100 | 460 | The crew's slips |
| `ads` | 1280 | 260 | Fake ad rotation |
| `standings` | 900 | 700 | Leaderboard for the wrap-up |

Every page has a transparent background, so it can sit on top of gameplay.
Leave the bonus source switched on all night; it only appears when a
question is open.

If a source stays blank, open its address in a normal browser. "Missing or
wrong overlay key" means the key in the address doesn't match Railway. A
blank page with no message means there's no race yet; the pages stay hidden
until `/race create`.


## Week to week

The README has the run of show. The one rule for Railway: **don't push code
or config changes during a live show.** Every push redeploys, and the bot is
offline for about a minute while it restarts.

To change the rundown or the ads, edit `config/show.json` or
`config/ads.json` on GitHub (the web editor is fine for these) and commit.
Railway redeploys on its own, and the season is untouched because it lives
on the volume. For image ads, add the image to `config/ads/` and commit.

Run `/backup` after each show and keep the file. It's a complete copy:
every wallet, race, bet and past season.

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

**The crew can't use crew commands.** The role must be named exactly `Race
Staff`. To use a different name, set `MPR_STAFF_ROLE` in Railway to match.

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
