# Name That Movie - Discord Bot

A single-channel Discord game where the winner of each round sets a new movie by uploading
3 screenshots. Screenshots are released automatically on a timer. Players guess using slash
commands, and wins, points and streaks are tracked on an all-time leaderboard.

Screenshots are posted via a webhook so they appear to come from the winner — showing
their profile picture and username — rather than from the bot.

---

## Setup

### 1. Create a Discord Application & Bot

1. Go to https://discord.com/developers/applications and create a new application.
2. Under **Bot**, click **Add Bot** and copy the **Token**.
3. Under **Bot → Privileged Gateway Intents**, enable:
   - **Server Members Intent**
   - **Message Content Intent**

#### Generating the invite URL

4. Go to **OAuth2 → URL Generator**
5. Under **Scopes**, tick **both**:
   - `bot`
   - `applications.commands`

   > Both scopes are required. `bot` adds the bot to your server's member list;
   > `applications.commands` registers the slash commands. Using only one of them
   > will result in the bot either not appearing in the member list, or its commands
   > not working.

6. Under **Bot Permissions** (appears once `bot` is ticked), select:
   - **Text Permissions:** Send Messages, Attach Files, Read Message History, Add Reactions, Use Slash Commands, Manage Webhooks
   - **Role Management:** Manage Roles

   > **Manage Webhooks** is required for screenshots to appear as if sent by the winner.
   > Without it the bot falls back to posting screenshots under its own name.

7. Copy the generated URL at the bottom and open it in your browser to invite the bot to your server.

#### After inviting

8. In your server go to **Settings → Roles** and drag the bot's role so it sits **above**
   the `Winner` role in the list. Without this the bot cannot assign or remove the Winner role.

---

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your token, guild ID, and channel ID
```

To get IDs: Enable Developer Mode in Discord (User Settings → Advanced),
then right-click a server or channel and choose **Copy ID**.

Game settings (timings, lightning rounds, weekly summary, etc.) are in `config.py`.

---

### 3. Deploy

```bash
cd /path/to/namethatmovie-bot
docker compose up -d --build
```

All game state (SQLite DB + screenshots) is stored in a persistent Docker volume and
survives restarts and rebuilds. The scheduler picks up exactly where it left off within
a minute of the bot coming back online.

> The only thing that destroys saved data is running `docker compose down -v` — the `-v`
> flag explicitly deletes volumes. Avoid this unless you want to reset everything.
> For an in-game reset, use `/ntm reset` instead.

To monitor logs:
```bash
docker logs namethatmovie-bot --tail 50
docker logs namethatmovie-bot --follow   # live stream
```

---

## How a Round Works

1. The current `Winner` runs `/ntm movie The Dark Knight` with **screenshot 1 attached**.
   - Screenshot 1 is posted immediately via webhook, appearing as if sent by the winner.
   - The winner receives an ephemeral confirmation with timing details.
2. The winner runs `/ntm screenshot` twice to upload screenshots 2 and 3.
   - A public message is posted once all 3 are uploaded, showing when each will drop.
   - Each screenshot is released automatically by the bot on its scheduled time.
3. Players type `/ntm guess <title>` to submit guesses (case-insensitive).
   - Wrong guesses are tracked and shown in the recap at the end of the round.
4. The first correct guess wins — the guesser earns points and gets the `Winner` role.
5. If nobody guesses in time, the bot auto-reveals the answer, posts a recap, and opens
   a **free game** — anyone except the person who set the movie can start the next round.
6. The winner can also use `/ntm skip` to give up early and reveal the answer themselves,
   also triggering a free game.

---

## Scoring

Points are awarded based on which screenshot the movie was guessed on:

| Screenshot | Points |
|---|---|
| First | 3 pts |
| Second | 2 pts |
| Third | 1 pt |

If nobody guesses the movie, the person who **set** the movie earns **1 point** for
stumping everyone. Points are not awarded on skipped rounds.

The leaderboard is sorted by total points, with wins as a tiebreaker.

---

## Streaks

A streak counts consecutive rounds where you guessed correctly. Rounds where you are
the **Winner** (and therefore cannot guess) are skipped — your streak is frozen for
those rounds and neither increments nor resets. This means a streak reflects pure
guessing performance, regardless of how often you set movies.

When a player hits the configured streak threshold, the bot publicly calls it out with
a random announcement message. Toggle this with `HOT_STREAK_ENABLED` and set the
minimum streak with `HOT_STREAK_THRESHOLD`.

---

## ⚡ Lightning Rounds

Each round has a **5% chance** of being a lightning round (configurable, or disable entirely):
- Screenshots release every **30 minutes** instead of 8 hours
- Auto-reveal after **2 hours** instead of 48
- The bot announces it at the start and screenshots are marked with ⚡
- The probability is completely random — there's no fixed interval

---

## Round Recap

At the end of every round — whether solved, timed out, skipped, or admin-ended — the bot
posts a recap embed showing:
- The movie title and who guessed it (or a random funny message if nobody did)
- Which screenshot it was solved on and points awarded
- Total guesses made
- Up to `MAX_WRONG_GUESSES_SHOWN` unique players who guessed incorrectly
- All available screenshots

If nobody guessed the movie, one of 22 randomly chosen funny messages is shown and the
setter earns +1 point.

---

## Free Game

When a round ends without anyone guessing (auto-reveal or skip), the bot announces a
**free game**: anyone except the person who just set the movie can start the next round
with `/ntm movie` — no Winner role required. This prevents the same person from setting
back-to-back movies and keeps the game open when nobody holds the Winner role.

Admins can always start a round regardless of free game rules.

---

## 📊 Weekly Summary

Every Monday at 09:00 UTC (configurable), the bot posts a weekly summary in the game
channel covering the previous week's activity:
- Rounds played
- Top guesser (most wins that week)
- Top stumper (most unsolved movies set)
- Hardest movie (most total guesses)
- Fastest guess (solved on earliest screenshot)

Toggle with `WEEKLY_SUMMARY_ENABLED`. Change the day and time with `WEEKLY_SUMMARY_DAY`
and `WEEKLY_SUMMARY_HOUR`.

---

## 🗓️ Monthly Leaderboard

A separate leaderboard resets on the 1st of each month, giving everyone a fresh shot at
the top regardless of all-time history. View it with `/ntm monthly`. Toggle with
`MONTHLY_LEADERBOARD_ENABLED`.

---

## Command Reference

| Command | Who | Description |
|---|---|---|
| `/ntm guess [title]` | Anyone | Submit a guess |
| `/ntm movie [title]` + image | Winner / Anyone in free game / Admin | Start a round with the first screenshot |
| `/ntm screenshot` + image | Winner / Admin | Add screenshot 2 or 3 |
| `/ntm skip` | Winner / Admin | Give up and reveal the answer early |
| `/ntm currentcheck` | Winner / Admin | Privately see the current movie title and guess count |
| `/ntm usagecheck [title]` | Anyone | Check how many times a movie has been used and by whom |
| `/ntm last` | Anyone | Post all screenshots from the previous round |
| `/ntm repost` | Anyone | Repost currently revealed screenshots |
| `/ntm leaders` | Anyone | All-time leaderboard with points, streaks and toughest movie |
| `/ntm monthly` | Anyone | This month's leaderboard |
| `/ntm stats [@user]` | Anyone | Personal stats for yourself or another player |
| `/ntm help` | Anyone | How to play |
| `/ntm winner @user` | Admin | Force-end the current round and assign a new winner |
| `/ntm reset` | Admin | Wipe all stats, history and screenshots (with confirmation) |

---

## Configuration Files

**`.env`** — secrets and Discord IDs (never commit this):

| Variable | Description |
|---|---|
| `DISCORD_TOKEN` | Bot token |
| `GUILD_ID` | Server ID |
| `GAME_CHANNEL_ID` | Channel for the game |
| `WINNER_ROLE_NAME` | Role name for the current winner (default: `Winner`) |

**`config.py`** — game settings (safe to commit):

| Variable | Default | Description |
|---|---|---|
| `SCREENSHOT_INTERVAL_HOURS` | `8` | Hours between screenshot releases in a normal round |
| `REVEAL_AFTER_HOURS` | `48` | Hours after last screenshot before auto-reveal in a normal round |
| `LIGHTNING_ROUNDS_ENABLED` | `True` | Set to `False` to disable lightning rounds entirely |
| `LIGHTNING_ROUND_PROBABILITY` | `0.05` | Chance per round of it being a lightning round (0.05 = 5%) |
| `LIGHTNING_INTERVAL_HOURS` | `0.5` | Hours between screenshots in a lightning round (0.5 = 30 min) |
| `LIGHTNING_REVEAL_HOURS` | `2` | Hours after last screenshot before auto-reveal in a lightning round |
| `HOT_STREAK_ENABLED` | `True` | Publicly announce when a player hits a hot streak |
| `HOT_STREAK_THRESHOLD` | `3` | Minimum streak length before a public announcement is made |
| `WEEKLY_SUMMARY_ENABLED` | `True` | Post a weekly summary in the game channel |
| `WEEKLY_SUMMARY_DAY` | `0` | Day to post the summary (0 = Monday, 6 = Sunday) |
| `WEEKLY_SUMMARY_HOUR` | `9` | Hour to post the summary (24h UTC) |
| `MONTHLY_LEADERBOARD_ENABLED` | `True` | Track a separate monthly leaderboard |
| `MAX_WRONG_GUESSES_SHOWN` | `5` | Max unique wrong guessers shown in the recap (0 to disable) |
| `SCREENSHOT_RETENTION_ROUNDS` | `2` | How many rounds of screenshots to keep on disk |
| `LEADERBOARD_SIZE` | `10` | Number of players shown on leaderboards |
