import discord
from discord.ext import commands, tasks
from discord import app_commands
import sqlite3
import unicodedata
import re
import httpx
import logging
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import config

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.getLogger("discord.gateway").setLevel(logging.WARNING)
logging.getLogger("discord.client").setLevel(logging.WARNING)
logging.getLogger("discord.http").setLevel(logging.WARNING)

log = logging.getLogger("ntm")

# ── Database ──────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Initialising database at %s", config.DB_PATH)
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS round (
                id              INTEGER PRIMARY KEY CHECK (id = 1),
                active          INTEGER NOT NULL DEFAULT 0,
                movie           TEXT,
                uploader_id     INTEGER,
                uploader_name   TEXT,
                uploader_avatar TEXT,
                released        INTEGER NOT NULL DEFAULT 0,
                reveal_at       REAL,
                round_number    INTEGER NOT NULL DEFAULT 0,
                lightning       INTEGER NOT NULL DEFAULT 0,
                guess_count     INTEGER NOT NULL DEFAULT 0,
                last_uploader_id INTEGER,
                round_id         INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS screenshots (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                local_path  TEXT NOT NULL,
                schedule_at REAL NOT NULL,
                released    INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS history (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                movie               TEXT NOT NULL,
                winner_id           INTEGER,
                winner_name         TEXT,
                uploader_id         INTEGER,
                uploader_name       TEXT,
                solved              INTEGER NOT NULL DEFAULT 1,
                solved_on_screenshot INTEGER,
                guess_count         INTEGER NOT NULL DEFAULT 0,
                points_awarded      INTEGER NOT NULL DEFAULT 0,
                played_at           TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS history_screenshots (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                history_id  INTEGER NOT NULL REFERENCES history(id),
                local_path  TEXT NOT NULL,
                seq         INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS leaderboard (
                user_id         INTEGER PRIMARY KEY,
                username        TEXT NOT NULL,
                wins            INTEGER NOT NULL DEFAULT 0,
                points          INTEGER NOT NULL DEFAULT 0,
                current_streak  INTEGER NOT NULL DEFAULT 0,
                best_streak     INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS movie_usage (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                movie         TEXT NOT NULL,
                uploader_name TEXT,
                played_at     TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS wrong_guesses (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                round_id    INTEGER NOT NULL,
                user_id     INTEGER NOT NULL,
                username    TEXT NOT NULL,
                guess       TEXT NOT NULL,
                guessed_at  TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(round_id, user_id, guess)
            );

            CREATE TABLE IF NOT EXISTS monthly_leaderboard (
                user_id     INTEGER NOT NULL,
                username    TEXT NOT NULL,
                year_month  TEXT NOT NULL,
                wins        INTEGER NOT NULL DEFAULT 0,
                points      INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, year_month)
            );

            INSERT OR IGNORE INTO round(id, active) VALUES(1, 0);
        """)

        # Migrations for existing installs
        round_cols = {row[1] for row in db.execute("PRAGMA table_info(round)")}
        for col, typedef in [
            ("uploader_name",   "TEXT"),
            ("uploader_avatar", "TEXT"),
            ("round_number",    "INTEGER NOT NULL DEFAULT 0"),
            ("lightning",       "INTEGER NOT NULL DEFAULT 0"),
            ("guess_count",     "INTEGER NOT NULL DEFAULT 0"),
            ("last_uploader_id", "INTEGER"),
            ("round_id",         "INTEGER NOT NULL DEFAULT 0"),
        ]:
            if col not in round_cols:
                db.execute(f"ALTER TABLE round ADD COLUMN {col} {typedef}")
                log.info("Migrated DB: added column round.%s", col)

        hist_cols = {row[1] for row in db.execute("PRAGMA table_info(history)")}
        for col, typedef in [
            ("uploader_id",          "INTEGER"),
            ("uploader_name",        "TEXT"),
            ("solved_on_screenshot", "INTEGER"),
            ("guess_count",          "INTEGER NOT NULL DEFAULT 0"),
            ("points_awarded",       "INTEGER NOT NULL DEFAULT 0"),
        ]:
            if col not in hist_cols:
                db.execute(f"ALTER TABLE history ADD COLUMN {col} {typedef}")
                log.info("Migrated DB: added column history.%s", col)

        usage_cols = {row[1] for row in db.execute("PRAGMA table_info(movie_usage)")}
        if "uploader_name" not in usage_cols:
            db.execute("ALTER TABLE movie_usage ADD COLUMN uploader_name TEXT")
            log.info("Migrated DB: added column movie_usage.uploader_name")

        lb_cols = {row[1] for row in db.execute("PRAGMA table_info(leaderboard)")}
        for col, typedef in [
            ("points",         "INTEGER NOT NULL DEFAULT 0"),
            ("current_streak", "INTEGER NOT NULL DEFAULT 0"),
            ("best_streak",    "INTEGER NOT NULL DEFAULT 0"),
        ]:
            if col not in lb_cols:
                db.execute(f"ALTER TABLE leaderboard ADD COLUMN {col} {typedef}")
                log.info("Migrated DB: added column leaderboard.%s", col)


# ── Helpers ───────────────────────────────────────────────────────────────────

def normalize(text: str) -> str:
    text = text.lower().strip()
    text = unicodedata.normalize("NFKD", text)
    text = re.sub(r"\s+", " ", text)
    return text


async def download_attachment(url: str, dest: Path):
    async with httpx.AsyncClient() as client:
        r = await client.get(url)
        r.raise_for_status()
        dest.write_bytes(r.content)


async def get_or_create_winner_role(guild: discord.Guild) -> discord.Role:
    role = discord.utils.get(guild.roles, name=config.WINNER_ROLE_NAME)
    if role is None:
        role = await guild.create_role(
            name=config.WINNER_ROLE_NAME,
            color=discord.Color.gold(),
            reason="Name That Movie - winner role",
        )
    return role


async def transfer_winner_role(guild: discord.Guild, new_winner: discord.Member):
    role = await get_or_create_winner_role(guild)
    for member in role.members:
        await member.remove_roles(role, reason="NTM: new round winner")
    await new_winner.add_roles(role, reason="NTM: won a round")


def is_winner(interaction: discord.Interaction) -> bool:
    role = discord.utils.get(interaction.guild.roles, name=config.WINNER_ROLE_NAME)
    return role is not None and role in interaction.user.roles


def is_admin(interaction: discord.Interaction) -> bool:
    return interaction.user.guild_permissions.manage_roles


def get_round(db: sqlite3.Connection) -> sqlite3.Row:
    return db.execute("SELECT * FROM round WHERE id = 1").fetchone()


def points_for_screenshot(seq: int) -> int:
    """Points awarded based on which screenshot the movie was guessed on."""
    return max(4 - seq, 1)  # seq1=3pts, seq2=2pts, seq3=1pt


async def get_or_create_webhook(channel: discord.TextChannel) -> discord.Webhook:
    webhooks = await channel.webhooks()
    for wh in webhooks:
        if wh.name == "NTM":
            return wh
    log.info("Creating NTM webhook in #%s", channel.name)
    return await channel.create_webhook(name="NTM")


async def post_screenshot_as_user(
    channel: discord.TextChannel,
    local_path: str,
    seq: int,
    total: int,
    uploader_name: str,
    uploader_avatar: Optional[str],
    lightning: bool = False,
):
    """Post a screenshot via webhook so it appears to come from the uploader."""
    webhook = await get_or_create_webhook(channel)

    filename = Path(local_path).name
    labels   = ["First Screenshot", "Second Screenshot", "Third Screenshot"]
    title    = labels[seq - 1] if seq <= 3 else f"Screenshot {seq}/{total}"
    if lightning:
        title = "⚡ " + title

    embed = discord.Embed(title=title, color=discord.Color.dark_gray())
    embed.set_image(url=f"attachment://{filename}")

    kwargs = dict(username=uploader_name, embeds=[embed])
    if uploader_avatar:
        kwargs["avatar_url"] = uploader_avatar

    with open(local_path, "rb") as f:
        await webhook.send(file=discord.File(f, filename=filename), **kwargs)


async def post_round_recap(
    channel: discord.TextChannel,
    movie: str,
    solved: bool,
    winner_name: Optional[str],
    uploader_name: Optional[str],
    points: int,
    guess_count: int,
    solved_on: Optional[int],
    screenshot_paths: list,
    skipped: bool = False,
    wrong_guessers: Optional[list] = None,
):
    """Post an end-of-round recap embed with all screenshots."""
    if skipped:
        color       = discord.Color.orange()
        result_line = f"⏭️ Round skipped by the Winner"
        title       = f"Round Over — {movie}"
    elif solved:
        color       = discord.Color.green()
        result_line = f"🏆 Guessed by **{winner_name}** on screenshot {solved_on} (+{points} pts)"
        title       = f"Round Over — {movie}"
    else:
        color = discord.Color.red()
        if skipped:
            result_line = "⏭️ Winner gave up — the movie remains a mystery"
        else:
            stumped_lines = [
                f"🧠 Absolutely nobody had a clue. Embarrassing, really.",
                f"🪦 The chat has collectively failed. Moment of silence.",
                f"🎬 **{uploader_name}** picked a banger and nobody got it. Respect.",
                f"😤 **{uploader_name}** is somewhere smiling right now.",
                f"🫥 The movie was right there. It was RIGHT THERE.",
                f"🐟 You lot couldn't guess your way out of a paper bag.",
                f"🎭 A masterpiece, unrecognised by the masses. Typical.",
                f"🤡 Incredible. Three screenshots and still nothing. Well done everyone.",
                f"📽️ **{uploader_name}** wins this round. The chat loses.",
                f"🧩 All the pieces were there. Nobody assembled them. Classic.",
                f"🏳️ The white flag has been raised. **{uploader_name}** reigns supreme.",
                f"🦗 *crickets* That's all we got. Just crickets.",
                f"🎓 Clearly more movie education is needed in this server.",
                f"😶 Not a single correct guess. The server has let itself down.",
                f"🌚 Darkness. The chat sat in darkness and achieved nothing.",
                f"👏 A round of applause for **{uploader_name}**, who outsmarted all of you.",
                f"🍿 You had the popcorn. You just didn't have the answers.",
                f"📖 Maybe try watching more movies. Just a suggestion.",
                f"🔍 Three clues. Zero correct answers. Sherlock would be ashamed.",
                f"💀 This chat is cooked. **{uploader_name}** has broken everyone.",
                f"🎪 Welcome to the circus, where nobody knows their films.",
                f"🛸 The answer has left the building. Along with everyone's credibility.",
            ]
            result_line = random.choice(stumped_lines)
            if uploader_name and points > 0:
                result_line += f" (+1 pt to **{uploader_name}**)"
        title = f"Round Over — {movie}"

    lines = [result_line]
    if uploader_name:
        lines.append(f"📽️ Set by **{uploader_name}**")
    lines.append(f"💬 Total guesses made: **{guess_count}**")
    if wrong_guessers and config.MAX_WRONG_GUESSES_SHOWN > 0:
        shown   = wrong_guessers[:config.MAX_WRONG_GUESSES_SHOWN]
        extra   = len(wrong_guessers) - len(shown)
        names   = ", ".join(f"**{n}**" for n in shown)
        if extra:
            names += f" (+{extra} more)"
        lines.append(f"❌ Wrong guessers: {names}")

    embed = discord.Embed(title=title, description="\n".join(lines), color=color)
    embed.set_footer(text="Use /ntm leaders to see the leaderboard")

    await channel.send(embed=embed)

    # Post any available screenshots as a gallery
    available = [p for p in screenshot_paths if Path(p).exists()]
    for i, p in enumerate(available):
        await channel.send(
            f"Screenshot {i + 1}/{len(screenshot_paths)}:",
            file=discord.File(p),
        )


def purge_old_screenshots(db: sqlite3.Connection):
    old_shots = db.execute(
        "SELECT hs.id, hs.local_path FROM history_screenshots hs "
        "WHERE hs.history_id NOT IN ("
        "  SELECT id FROM history ORDER BY id DESC LIMIT ?"
        ")",
        (config.SCREENSHOT_RETENTION_ROUNDS,)
    ).fetchall()

    for shot in old_shots:
        Path(shot["local_path"]).unlink(missing_ok=True)
        log.info("Purged old screenshot: %s", shot["local_path"])

    if old_shots:
        old_ids = tuple(s["id"] for s in old_shots)
        db.execute(
            f"DELETE FROM history_screenshots WHERE id IN ({','.join('?' * len(old_ids))})",
            old_ids,
        )


def end_round(
    db: sqlite3.Connection,
    solved: bool,
    winner_id: Optional[int],
    winner_name: Optional[str],
    solved_on_screenshot: Optional[int] = None,
    points: int = 0,
    skipped: bool = False,
) -> dict:
    """End the active round, archive it, update streaks/leaderboard. Returns recap data."""
    row = get_round(db)
    if not row or not row["active"]:
        return {}

    guess_count   = row["guess_count"]
    uploader_id   = row["uploader_id"]
    uploader_name = row["uploader_name"]
    movie         = row["movie"]
    round_id      = row["round_id"]

    # Fetch wrong guessers before clearing
    wrong_guessers = db.execute(
        "SELECT DISTINCT username FROM wrong_guesses WHERE round_id=? ORDER BY guessed_at",
        (round_id,)
    ).fetchall()

    log.info("Round ending — movie: %r  solved: %s  winner: %s  guesses: %d",
             movie, solved, winner_name or "nobody", guess_count)

    # Archive to history
    db.execute(
        "INSERT INTO history(movie, winner_id, winner_name, uploader_id, uploader_name, "
        "solved, solved_on_screenshot, guess_count, points_awarded) VALUES(?,?,?,?,?,?,?,?,?)",
        (movie, winner_id, winner_name, uploader_id, uploader_name,
         1 if solved else 0, solved_on_screenshot, guess_count, points),
    )
    hist_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

    shots = db.execute("SELECT * FROM screenshots ORDER BY schedule_at").fetchall()
    shot_paths = []
    for i, s in enumerate(shots):
        db.execute(
            "INSERT INTO history_screenshots(history_id, local_path, seq) VALUES(?,?,?)",
            (hist_id, s["local_path"], i),
        )
        shot_paths.append(s["local_path"])

    # Update leaderboard & streaks
    # The uploader cannot guess in their own round, so their streak is never
    # affected by rounds they set — it neither increments nor resets (Option B).
    if solved and winner_id:
        # Winner gets points, win, streak incremented
        db.execute(
            "INSERT INTO leaderboard(user_id, username, wins, points, current_streak, best_streak) "
            "VALUES(?,?,1,?,1,1) ON CONFLICT(user_id) DO UPDATE SET "
            "wins=wins+1, points=points+?, username=excluded.username, "
            "current_streak=current_streak+1, "
            "best_streak=MAX(best_streak, current_streak+1)",
            (winner_id, winner_name, points, points),
        )
        # Monthly leaderboard
        if config.MONTHLY_LEADERBOARD_ENABLED:
            ym = datetime.now(timezone.utc).strftime("%Y-%m")
            db.execute(
                "INSERT INTO monthly_leaderboard(user_id, username, year_month, wins, points) "
                "VALUES(?,?,?,1,?) ON CONFLICT(user_id, year_month) DO UPDATE SET "
                "wins=wins+1, points=points+?, username=excluded.username",
                (winner_id, winner_name, ym, points, points),
            )
        # Reset streaks for everyone except the winner AND the uploader
        # (uploader's streak is frozen while they are setting movies)
        db.execute(
            "UPDATE leaderboard SET current_streak=0 WHERE user_id != ? AND user_id != ?",
            (winner_id, uploader_id),
        )
    else:
        # Unsolved — uploader earns 1 point for stumping everyone (not awarded on skip)
        if uploader_id and uploader_name and not skipped:
            db.execute(
                "INSERT INTO leaderboard(user_id, username, wins, points, current_streak, best_streak) "
                "VALUES(?,?,0,1,0,0) ON CONFLICT(user_id) DO UPDATE SET "
                "points=points+1, username=excluded.username",
                (uploader_id, uploader_name),
            )
            points = 1
        # Reset streaks for everyone except the uploader
        if uploader_id:
            db.execute(
                "UPDATE leaderboard SET current_streak=0 WHERE user_id != ?",
                (uploader_id,)
            )
        else:
            db.execute("UPDATE leaderboard SET current_streak=0")

    db.execute("DELETE FROM screenshots")
    db.execute("DELETE FROM wrong_guesses WHERE round_id=?", (round_id,))
    new_round_id = round_id + 1
    db.execute(
        "UPDATE round SET active=0, movie=NULL, uploader_id=NULL, uploader_name=NULL, "
        "uploader_avatar=NULL, released=0, reveal_at=NULL, lightning=0, guess_count=0, "
        "last_uploader_id=?, round_id=? WHERE id=1",
        (uploader_id, new_round_id)
    )
    purge_old_screenshots(db)

    return {
        "movie":           movie,
        "uploader_id":     uploader_id,
        "uploader_name":   uploader_name,
        "winner_name":     winner_name,
        "solved":          solved,
        "solved_on":       solved_on_screenshot,
        "points":          points,
        "guess_count":     guess_count,
        "shot_paths":      shot_paths,
        "skipped":         skipped,
        "wrong_guessers":  [r["username"] for r in wrong_guessers],
    }


# ── Bot setup ─────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="SS", intents=intents)
ntm = app_commands.Group(name="ntm", description="Name That Movie commands")

# ── Scheduler ─────────────────────────────────────────────────────────────────

@tasks.loop(minutes=1)
async def scheduler():
    with get_db() as db:
        row = get_round(db)
        if not row or not row["active"]:
            return

        now      = datetime.now(timezone.utc).timestamp()
        channel  = bot.get_channel(config.GAME_CHANNEL_ID)
        if channel is None:
            return

        lightning     = bool(row["lightning"])
        uploader_name = row["uploader_name"] or "Winner"

        pending = db.execute(
            "SELECT * FROM screenshots WHERE released=0 AND schedule_at <= ? ORDER BY schedule_at",
            (now,)
        ).fetchall()

        for shot in pending:
            total    = db.execute("SELECT COUNT(*) FROM screenshots").fetchone()[0]
            released = db.execute("SELECT COUNT(*) FROM screenshots WHERE released=1").fetchone()[0]
            seq      = released + 1

            try:
                await post_screenshot_as_user(
                    channel=channel,
                    local_path=shot["local_path"],
                    seq=seq,
                    total=total,
                    uploader_name=uploader_name,
                    uploader_avatar=row["uploader_avatar"],
                    lightning=lightning,
                )
                log.info("Released screenshot %d/%d (lightning=%s)", seq, total, lightning)
            except discord.Forbidden:
                log.error("Webhook failed — missing Manage Webhooks permission in #%s", channel.name)
                await channel.send(
                    f"Screenshot {seq}/{total} - Name That Movie! (posted by {uploader_name})",
                    file=discord.File(shot["local_path"]),
                )
            except Exception as e:
                log.error("Failed to post screenshot %d: %s", seq, e)
                await channel.send(
                    f"Screenshot {seq}/{total} - Name That Movie! (posted by {uploader_name})",
                    file=discord.File(shot["local_path"]),
                )

            db.execute("UPDATE screenshots SET released=1 WHERE id=?", (shot["id"],))
            db.execute("UPDATE round SET released=released+1 WHERE id=1")

        # Auto-reveal
        row       = get_round(db)
        reveal_at = row["reveal_at"]
        if reveal_at and now >= reveal_at and row["active"]:
            all_released = db.execute(
                "SELECT COUNT(*) FROM screenshots WHERE released=0"
            ).fetchone()[0] == 0
            if all_released:
                log.info("Auto-reveal triggered for movie: %r", row["movie"])
                recap = end_round(db, solved=False, winner_id=None, winner_name=None)

        else:
            recap = None

    if recap:
        await post_round_recap(
            channel=channel,
            movie=recap["movie"],
            solved=False,
            winner_name=None,
            uploader_name=recap["uploader_name"],
            points=recap["points"],
            guess_count=recap["guess_count"],
            solved_on=None,
            screenshot_paths=recap["shot_paths"],
            wrong_guessers=recap.get("wrong_guessers"),
        )
        uploader_name = recap["uploader_name"] or "the previous setter"
        await channel.send(
            f"🆓 **Free game!** Since nobody guessed it, anyone except **{uploader_name}** "
            f"can start the next round with `/ntm movie`.\n"
            f"An admin can use `/ntm winner @user` to assign the Winner role first if needed."
        )


# ── Weekly summary scheduler ─────────────────────────────────────────────────

@tasks.loop(minutes=1)
async def weekly_summary():
    if not config.WEEKLY_SUMMARY_ENABLED:
        return
    now = datetime.now(timezone.utc)
    if now.weekday() != config.WEEKLY_SUMMARY_DAY or now.hour != config.WEEKLY_SUMMARY_HOUR or now.minute != 0:
        return

    channel = bot.get_channel(config.GAME_CHANNEL_ID)
    if not channel:
        return

    # Calculate start of last week (Monday 00:00 UTC)
    days_since_monday = now.weekday()
    week_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    from datetime import timedelta
    week_start -= timedelta(days=days_since_monday + 7)
    week_end   = week_start + timedelta(days=7)
    ws = week_start.strftime("%Y-%m-%d")
    we = week_end.strftime("%Y-%m-%d")

    with get_db() as db:
        rounds_played = db.execute(
            "SELECT COUNT(*) as c FROM history WHERE played_at >= ? AND played_at < ?", (ws, we)
        ).fetchone()["c"]

        if rounds_played == 0:
            return  # No activity last week, skip

        top_guesser = db.execute(
            "SELECT winner_name, COUNT(*) as wins FROM history "
            "WHERE solved=1 AND winner_name IS NOT NULL AND played_at >= ? AND played_at < ? "
            "GROUP BY winner_id ORDER BY wins DESC LIMIT 1", (ws, we)
        ).fetchone()

        top_setter = db.execute(
            "SELECT uploader_name, COUNT(*) as stumps FROM history "
            "WHERE solved=0 AND uploader_name IS NOT NULL AND played_at >= ? AND played_at < ? "
            "GROUP BY uploader_id ORDER BY stumps DESC LIMIT 1", (ws, we)
        ).fetchone()

        hardest = db.execute(
            "SELECT movie, uploader_name, guess_count FROM history "
            "WHERE played_at >= ? AND played_at < ? ORDER BY guess_count DESC LIMIT 1", (ws, we)
        ).fetchone()

        fastest = db.execute(
            "SELECT movie, winner_name, solved_on_screenshot FROM history "
            "WHERE solved=1 AND played_at >= ? AND played_at < ? "
            "ORDER BY solved_on_screenshot ASC, guess_count ASC LIMIT 1", (ws, we)
        ).fetchone()

    embed = discord.Embed(
        title=f"📊 Weekly Summary — {week_start.strftime('%b %d')} to {week_end.strftime('%b %d')}",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="🎬 Rounds Played", value=str(rounds_played), inline=True)

    if top_guesser:
        embed.add_field(
            name="🏆 Top Guesser",
            value=f"**{top_guesser['winner_name']}** ({top_guesser['wins']} wins)",
            inline=True,
        )
    if top_setter:
        embed.add_field(
            name="😈 Top Stumper",
            value=f"**{top_setter['uploader_name']}** ({top_setter['stumps']} unsolved)",
            inline=True,
        )
    if hardest:
        embed.add_field(
            name="🧠 Hardest Movie",
            value=f"**{hardest['movie']}** by {hardest['uploader_name']} ({hardest['guess_count']} guesses)",
            inline=False,
        )
    if fastest:
        embed.add_field(
            name="⚡ Fastest Guess",
            value=f"**{fastest['winner_name']}** got **{fastest['movie']}** on screenshot {fastest['solved_on_screenshot']}",
            inline=False,
        )

    embed.set_footer(text="See /ntm leaders for the all-time leaderboard")
    await channel.send(embed=embed)
    log.info("Weekly summary posted for week of %s", ws)


# ── Events ────────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    init_db()
    guild  = discord.Object(id=config.GUILD_ID)
    bot.tree.add_command(ntm, guild=guild)
    synced = await bot.tree.sync(guild=guild)
    g      = bot.get_guild(config.GUILD_ID)
    if g:
        await g.chunk()
    scheduler.start()
    weekly_summary.start()
    log.info("Bot ready as %s — synced %d slash commands to guild %d",
             bot.user, len(synced), config.GUILD_ID)
    if not g:
        log.warning("Guild %d not found — check GUILD_ID in .env", config.GUILD_ID)


# ── /ntm guess ────────────────────────────────────────────────────────────────

@ntm.command(name="guess", description="Submit a guess for the current Name That Movie.")
@app_commands.describe(title="Your movie title guess")
async def ntm_guess(interaction: discord.Interaction, title: str):
    if interaction.channel_id != config.GAME_CHANNEL_ID:
        await interaction.response.send_message("Please use this command in the game channel.", ephemeral=True)
        return

    recap  = None
    winner = interaction.user

    with get_db() as db:
        row = get_round(db)
        if not row or not row["active"]:
            await interaction.response.send_message("No round is currently active.", ephemeral=True)
            return

        if is_winner(interaction):
            await interaction.response.send_message("You set the movie - you can't guess it!", ephemeral=True)
            return

        if row["released"] == 0:
            await interaction.response.send_message("Wait for the first screenshot to be posted!", ephemeral=True)
            return

        # Increment guess count regardless of correctness
        db.execute("UPDATE round SET guess_count=guess_count+1 WHERE id=1")

        if normalize(title) != normalize(row["movie"]):
            # Track unique wrong guessers for the recap
            db.execute(
                "INSERT OR IGNORE INTO wrong_guesses(round_id, user_id, username, guess) "
                "VALUES(?,?,?,?)",
                (row["round_id"], winner.id, winner.display_name, title),
            )
            await interaction.response.send_message(
                f"Sorry, **{title}** is not correct. Keep guessing!", ephemeral=True
            )
            return

        # Correct guess
        solved_on = row["released"]   # which screenshot number was showing
        pts       = points_for_screenshot(solved_on)
        log.info("Correct guess by %s on screenshot %d for movie: %r (+%d pts)",
                 winner.display_name, solved_on, row["movie"], pts)

        recap = end_round(
            db,
            solved=True,
            winner_id=winner.id,
            winner_name=winner.display_name,
            solved_on_screenshot=solved_on,
            points=pts,
        )

    await interaction.response.send_message(
        f"🏆 {winner.mention} got it! The movie was **{recap['movie']}**!\n"
        f"They earned **{pts} point(s)** and are now the {config.WINNER_ROLE_NAME}!"
    )
    await transfer_winner_role(interaction.guild, winner)

    channel = bot.get_channel(config.GAME_CHANNEL_ID)
    if channel and recap:
        await post_round_recap(
            channel=channel,
            movie=recap["movie"],
            solved=True,
            winner_name=winner.display_name,
            uploader_name=recap["uploader_name"],
            points=pts,
            guess_count=recap["guess_count"],
            solved_on=solved_on,
            screenshot_paths=recap["shot_paths"],
            wrong_guessers=recap.get("wrong_guessers"),
        )
        # Hot streak announcement
        if config.HOT_STREAK_ENABLED:
            with get_db() as db:
                streak_row = db.execute(
                    "SELECT current_streak FROM leaderboard WHERE user_id=?",
                    (winner.id,)
                ).fetchone()
            streak = streak_row["current_streak"] if streak_row else 0
            if streak >= config.HOT_STREAK_THRESHOLD:
                streak_msgs = [
                    f"🔥 **{winner.display_name}** is on a **{streak}-game streak** — can anyone stop them?",
                    f"🔥 {streak} in a row for **{winner.display_name}**! They're absolutely on fire!",
                    f"🔥 **{winner.display_name}** keeps delivering! That's {streak} straight wins!",
                    f"🔥 Someone please stop **{winner.display_name}** — {streak} wins in a row now!",
                    f"🔥 **{winner.display_name}** is unstoppable. {streak}-game streak and counting!",
                ]
                await channel.send(random.choice(streak_msgs))


# ── /ntm movie ────────────────────────────────────────────────────────────────

@ntm.command(name="movie", description="Set the current movie and start a new round (Winner only).")
@app_commands.describe(title="The movie title", screenshot="First screenshot (required)")
async def ntm_movie(interaction: discord.Interaction, title: str, screenshot: discord.Attachment):
    if interaction.channel_id != config.GAME_CHANNEL_ID:
        await interaction.response.send_message("Please use this command in the game channel.", ephemeral=True)
        return

    with get_db() as db:
        row = get_round(db)

        # In a free-game situation (no Winner role holder), anyone can start EXCEPT
        # the person who just set the previous movie.
        winner_role = discord.utils.get(interaction.guild.roles, name=config.WINNER_ROLE_NAME)
        role_is_held = winner_role is not None and len(winner_role.members) > 0
        is_free_game = not role_is_held and (not row or not row["active"])

        if not is_winner(interaction) and not is_admin(interaction) and not is_free_game:
            await interaction.response.send_message(
                "Only the current Winner can set the movie.", ephemeral=True
            )
            return

        if is_free_game and not is_admin(interaction):
            last_uploader_id = row["last_uploader_id"] if row else None
            if last_uploader_id and interaction.user.id == last_uploader_id:
                await interaction.response.send_message(
                    "You set the last movie — someone else needs to start this round. "
                    "It's free game for everyone else!",
                    ephemeral=True,
                )
                return

        if row and row["active"]:
            await interaction.response.send_message(
                "A round is already in progress! It must end before starting a new one.", ephemeral=True
            )
            return

        if not screenshot.content_type or not screenshot.content_type.startswith("image"):
            await interaction.response.send_message(
                "Please attach a valid image as the first screenshot.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        for f in db.execute("SELECT local_path FROM screenshots").fetchall():
            Path(f["local_path"]).unlink(missing_ok=True)
        db.execute("DELETE FROM screenshots")

        ext  = screenshot.filename.rsplit(".", 1)[-1] if "." in screenshot.filename else "png"
        path = config.SHOTS_DIR / f"shot_{int(datetime.now(timezone.utc).timestamp())}_1.{ext}"
        await download_attachment(screenshot.url, path)

        now           = datetime.now(timezone.utc).timestamp()
        avatar_url    = interaction.user.display_avatar.url
        uploader_name = interaction.user.display_name

        # Determine round number and whether this is a lightning round
        prev_round_number = row["round_number"] if row else 0
        round_number      = prev_round_number + 1
        lightning         = (config.LIGHTNING_ROUNDS_ENABLED and random.random() < config.LIGHTNING_ROUND_PROBABILITY)

        interval_hours = config.LIGHTNING_INTERVAL_HOURS if lightning else config.SCREENSHOT_INTERVAL_HOURS
        reveal_hours   = config.LIGHTNING_REVEAL_HOURS   if lightning else config.REVEAL_AFTER_HOURS

        db.execute(
            "INSERT INTO screenshots(local_path, schedule_at, released) VALUES(?,?,1)",
            (str(path), now),
        )
        db.execute("INSERT INTO movie_usage(movie, uploader_name) VALUES(?,?)", (title, uploader_name))
        db.execute(
            "UPDATE round SET active=1, movie=?, uploader_id=?, uploader_name=?, "
            "uploader_avatar=?, released=1, reveal_at=NULL, round_number=?, lightning=?, guess_count=0 WHERE id=1",
            (title, interaction.user.id, uploader_name, avatar_url, round_number, 1 if lightning else 0),
        )
        log.info("Round #%d started by %s — movie: %r  lightning: %s",
                 round_number, uploader_name, title, lightning)

    interval_str = (
        f"{int(interval_hours * 60)} minutes" if lightning
        else f"{int(interval_hours)} hours"
    )

    channel = interaction.channel
    if lightning:
        await channel.send(
            "⚡ **LIGHTNING ROUND!** ⚡\n"
            f"Screenshots every **{int(interval_hours * 60)} minutes** — guess fast!"
        )

    try:
        await post_screenshot_as_user(
            channel=channel,
            local_path=str(path),
            seq=1,
            total=3,
            uploader_name=uploader_name,
            uploader_avatar=avatar_url,
            lightning=lightning,
        )
    except discord.Forbidden:
        log.error("Webhook failed — missing Manage Webhooks in #%s", channel.name)
        await channel.send(
            f"Screenshot 1/3 - Name That Movie! (posted by {uploader_name})",
            file=discord.File(str(path)),
        )
    except Exception as e:
        log.error("Webhook post failed: %s", e)
        await channel.send(
            f"Screenshot 1/3 - Name That Movie! (posted by {uploader_name})",
            file=discord.File(str(path)),
        )

    await interaction.followup.send(
        f"{'⚡ Lightning round! ' if lightning else ''}Round #{round_number} started with **{title}** as the answer.\n\n"
        f"Add screenshots 2 and 3 using `/ntm screenshot` (one at a time).\n"
        f"Each will be released **{interval_str}** after the previous one.",
        ephemeral=True,
    )


# ── /ntm screenshot ───────────────────────────────────────────────────────────

@ntm.command(name="screenshot", description="Add the next screenshot for the current round (Winner only).")
@app_commands.describe(screenshot="Screenshot image to add")
async def ntm_screenshot(interaction: discord.Interaction, screenshot: discord.Attachment):
    if not is_winner(interaction) and not is_admin(interaction):
        await interaction.response.send_message("Only the current Winner can add screenshots.", ephemeral=True)
        return

    with get_db() as db:
        row = get_round(db)
        if not row or not row["active"]:
            await interaction.response.send_message(
                "No active round. Start one with `/ntm movie` first.", ephemeral=True
            )
            return

        if not screenshot.content_type or not screenshot.content_type.startswith("image"):
            await interaction.response.send_message("Please attach a valid image.", ephemeral=True)
            return

        total = db.execute("SELECT COUNT(*) as c FROM screenshots").fetchone()["c"]
        if total >= 3:
            await interaction.response.send_message(
                "All 3 screenshots have already been added for this round.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        lightning      = bool(row["lightning"])
        interval_hours = config.LIGHTNING_INTERVAL_HOURS if lightning else config.SCREENSHOT_INTERVAL_HOURS
        reveal_hours   = config.LIGHTNING_REVEAL_HOURS   if lightning else config.REVEAL_AFTER_HOURS

        seq  = total + 1
        ext  = screenshot.filename.rsplit(".", 1)[-1] if "." in screenshot.filename else "png"
        path = config.SHOTS_DIR / f"shot_{int(datetime.now(timezone.utc).timestamp())}_{seq}.{ext}"
        await download_attachment(screenshot.url, path)

        prev_time   = db.execute("SELECT MAX(schedule_at) as t FROM screenshots").fetchone()["t"]
        schedule_at = (prev_time or datetime.now(timezone.utc).timestamp()) + interval_hours * 3600

        db.execute(
            "INSERT INTO screenshots(local_path, schedule_at, released) VALUES(?,?,0)",
            (str(path), schedule_at),
        )

        if seq == 3:
            reveal_at = schedule_at + reveal_hours * 3600
            db.execute("UPDATE round SET reveal_at=? WHERE id=1", (reveal_at,))

        log.info("Screenshot %d/3 added, scheduled for %s",
                 seq, datetime.fromtimestamp(schedule_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))

    release_str = f"<t:{int(schedule_at)}:R>"
    msg = f"Screenshot {seq}/3 added - it will be posted {release_str}."
    if seq == 3:
        msg += (f"\n\nAll 3 screenshots are queued! "
                f"Auto-reveal is {reveal_hours}h after the last one if nobody guesses.")
    else:
        msg += f"\n\nAdd {3 - seq} more screenshot(s) with `/ntm screenshot`."

    await interaction.followup.send(msg, ephemeral=True)

    # Public confirmation once all screenshots are uploaded
    if seq == 3:
        with get_db() as db:
            all_shots = db.execute(
                "SELECT schedule_at FROM screenshots ORDER BY schedule_at"
            ).fetchall()
        shot2_ts = int(all_shots[1]["schedule_at"]) if len(all_shots) >= 2 else None
        shot3_ts = int(all_shots[2]["schedule_at"]) if len(all_shots) >= 3 else None
        shot2_str = f"<t:{shot2_ts}:R>" if shot2_ts else "soon"
        shot3_str = f"<t:{shot3_ts}:R>" if shot3_ts else "later"
        channel = bot.get_channel(config.GAME_CHANNEL_ID)
        if channel:
            await channel.send(
                f"✅ **{interaction.user.display_name}** has uploaded all 3 screenshots!\n"
                f"Screenshot 2 drops {shot2_str}, screenshot 3 {shot3_str}.\n"
                f"Use `/ntm guess` to submit your answer — good luck! 🍿"
            )


# ── /ntm skip ─────────────────────────────────────────────────────────────────

@ntm.command(name="skip", description="Give up and reveal the answer early (Winner only).")
async def ntm_skip(interaction: discord.Interaction):
    if not is_winner(interaction) and not is_admin(interaction):
        await interaction.response.send_message("Only the current Winner can skip a round.", ephemeral=True)
        return

    recap = None
    with get_db() as db:
        row = get_round(db)
        if not row or not row["active"]:
            await interaction.response.send_message("No round is currently active.", ephemeral=True)
            return

        log.info("%s skipped the round — movie was: %r", interaction.user.display_name, row["movie"])
        recap = end_round(db, solved=False, winner_id=None, winner_name=None, skipped=True)

    skipped_by = interaction.user.display_name
    await interaction.response.send_message(
        f"⏭️ Round skipped! The movie was **{recap['movie']}**.\n"
        f"🆓 **Free game!** Anyone except **{skipped_by}** can start the next round with `/ntm movie`.\n"
        f"An admin can use `/ntm winner @user` to assign the Winner role first if needed.",
        ephemeral=False,
    )

    channel = bot.get_channel(config.GAME_CHANNEL_ID)
    if channel and recap:
        await post_round_recap(
            channel=channel,
            movie=recap["movie"],
            solved=False,
            winner_name=None,
            uploader_name=recap["uploader_name"],
            points=0,
            guess_count=recap["guess_count"],
            solved_on=None,
            screenshot_paths=recap["shot_paths"],
            skipped=True,
            wrong_guessers=recap.get("wrong_guessers"),
        )


# ── /ntm currentcheck ─────────────────────────────────────────────────────────

@ntm.command(name="currentcheck", description="Reveals the current movie title to you (Winner only).")
async def ntm_currentcheck(interaction: discord.Interaction):
    if not is_winner(interaction) and not is_admin(interaction):
        await interaction.response.send_message("Only the current Winner can use this command.", ephemeral=True)
        return

    with get_db() as db:
        row = get_round(db)
        if not row or not row["active"]:
            await interaction.response.send_message("No round is currently active.", ephemeral=True)
            return
        total    = db.execute("SELECT COUNT(*) as c FROM screenshots").fetchone()["c"]
        released = row["released"]
        lightning = bool(row["lightning"])

    await interaction.response.send_message(
        f"{'⚡ Lightning round! ' if lightning else ''}Current movie: **{row['movie']}**\n"
        f"Screenshots: **{released}/{total}** released  |  Guesses so far: **{row['guess_count']}**",
        ephemeral=True,
    )


# ── /ntm usagecheck ───────────────────────────────────────────────────────────

@ntm.command(name="usagecheck", description="Check how many times a movie has been used.")
@app_commands.describe(title="Movie title to look up")
async def ntm_usagecheck(interaction: discord.Interaction, title: str):
    with get_db() as db:
        rows = db.execute(
            "SELECT uploader_name, played_at FROM movie_usage "
            "WHERE lower(movie)=lower(?) ORDER BY played_at",
            (title,)
        ).fetchall()

    if not rows:
        await interaction.response.send_message(
            f"**{title}** has never been used in Name That Movie.", ephemeral=True
        )
    else:
        lines = []
        for r in rows:
            who  = r["uploader_name"] or "Unknown"
            date = r["played_at"][:10]
            lines.append(f"- **{who}** on {date}")
        times = f"{len(rows)} time" if len(rows) == 1 else f"{len(rows)} times"
        await interaction.response.send_message(
            f"**{title}** has been used **{times}**:\n" + "\n".join(lines),
            ephemeral=True,
        )


# ── /ntm last ─────────────────────────────────────────────────────────────────

@ntm.command(name="last", description="Post all screenshots from the previous round.")
async def ntm_last(interaction: discord.Interaction):
    if interaction.channel_id != config.GAME_CHANNEL_ID:
        await interaction.response.send_message("Please use this command in the game channel.", ephemeral=True)
        return

    with get_db() as db:
        hist = db.execute("SELECT * FROM history ORDER BY id DESC LIMIT 1").fetchone()
        if not hist:
            await interaction.response.send_message("No previous rounds found.", ephemeral=True)
            return
        shots = db.execute(
            "SELECT * FROM history_screenshots WHERE history_id=? ORDER BY seq",
            (hist["id"],)
        ).fetchall()

    await interaction.response.defer()

    solved_str = (
        f"Guessed by **{hist['winner_name']}**"
        if hist["solved"] and hist["winner_name"]
        else "Nobody guessed it"
    )
    await interaction.followup.send(f"Previous movie: **{hist['movie']}** - {solved_str}")

    for i, s in enumerate(shots):
        p = Path(s["local_path"])
        if p.exists():
            await interaction.channel.send(
                f"Screenshot {i + 1}/{len(shots)}:",
                file=discord.File(str(p)),
            )
        else:
            await interaction.channel.send(
                f"Screenshot {i + 1}/{len(shots)}: _(file no longer available)_"
            )


# ── /ntm repost ───────────────────────────────────────────────────────────────

@ntm.command(name="repost", description="Repost all currently revealed screenshots for the active round.")
async def ntm_repost(interaction: discord.Interaction):
    if interaction.channel_id != config.GAME_CHANNEL_ID:
        await interaction.response.send_message("Please use this command in the game channel.", ephemeral=True)
        return

    with get_db() as db:
        row = get_round(db)
        if not row or not row["active"]:
            await interaction.response.send_message("No round is currently active.", ephemeral=True)
            return
        shots           = db.execute(
            "SELECT * FROM screenshots WHERE released=1 ORDER BY schedule_at"
        ).fetchall()
        uploader_name   = row["uploader_name"] or "Winner"
        uploader_avatar = row["uploader_avatar"]
        lightning       = bool(row["lightning"])

    if not shots:
        await interaction.response.send_message("No screenshots have been released yet.", ephemeral=True)
        return

    await interaction.response.defer()
    await interaction.followup.send(f"Reposting **{len(shots)}** released screenshot(s) - Name That Movie!")

    total = len(shots)
    for i, s in enumerate(shots):
        p = Path(s["local_path"])
        if p.exists():
            try:
                await post_screenshot_as_user(
                    channel=interaction.channel,
                    local_path=str(p),
                    seq=i + 1,
                    total=total,
                    uploader_name=uploader_name,
                    uploader_avatar=uploader_avatar,
                    lightning=lightning,
                )
            except Exception:
                await interaction.channel.send(
                    f"Screenshot {i + 1}/{total}:",
                    file=discord.File(str(p)),
                )


# ── /ntm leaders ──────────────────────────────────────────────────────────────

@ntm.command(name="leaders", description="Show the all-time Name That Movie leaderboard.")
async def ntm_leaders(interaction: discord.Interaction):
    with get_db() as db:
        rows = db.execute(
            "SELECT username, wins, points, current_streak, best_streak "
            "FROM leaderboard ORDER BY points DESC, wins DESC LIMIT ?",
            (config.LEADERBOARD_SIZE,)
        ).fetchall()

        # Toughest setter: movie with most guesses before being solved (or unsolved)
        toughest = db.execute(
            "SELECT uploader_name, movie, guess_count FROM history "
            "WHERE uploader_name IS NOT NULL AND guess_count > 0 "
            "ORDER BY guess_count DESC LIMIT 1"
        ).fetchone()

    if not rows:
        await interaction.response.send_message("No wins recorded yet. Start guessing!", ephemeral=True)
        return

    medals = ["🥇", "🥈", "🥉"]
    lines  = []
    for i, r in enumerate(rows):
        medal  = medals[i] if i < 3 else f"**{i + 1}.**"
        streak = f"  🔥 {r['current_streak']}" if r["current_streak"] > 1 else ""
        lines.append(
            f"{medal} **{r['username']}** — {r['points']} pts · {r['wins']} win(s)"
            f" · best streak {r['best_streak']}{streak}"
        )

    embed = discord.Embed(
        title="Name That Movie — Leaderboard",
        description="\n".join(lines),
        color=discord.Color.gold(),
    )

    if toughest:
        embed.add_field(
            name="🎬 Toughest Movie Set",
            value=f"**{toughest['movie']}** by {toughest['uploader_name']} "
                  f"({toughest['guess_count']} guesses)",
            inline=False,
        )

    embed.set_footer(text="Sorted by points · 3pts for screenshot 1 · 2pts for 2 · 1pt for 3")
    await interaction.response.send_message(embed=embed)


# ── /ntm monthly ─────────────────────────────────────────────────────────────

@ntm.command(name="monthly", description="Show this month's Name That Movie leaderboard.")
async def ntm_monthly(interaction: discord.Interaction):
    if not config.MONTHLY_LEADERBOARD_ENABLED:
        await interaction.response.send_message(
            "Monthly leaderboard is not enabled.", ephemeral=True
        )
        return

    ym = datetime.now(timezone.utc).strftime("%Y-%m")
    month_label = datetime.now(timezone.utc).strftime("%B %Y")

    with get_db() as db:
        rows = db.execute(
            "SELECT username, wins, points FROM monthly_leaderboard "
            "WHERE year_month=? ORDER BY points DESC, wins DESC LIMIT ?",
            (ym, config.LEADERBOARD_SIZE)
        ).fetchall()

    if not rows:
        await interaction.response.send_message(
            f"No wins recorded yet this month ({month_label}). Start guessing!",
            ephemeral=True,
        )
        return

    medals = ["🥇", "🥈", "🥉"]
    lines  = []
    for i, r in enumerate(rows):
        medal = medals[i] if i < 3 else f"**{i + 1}.**"
        lines.append(f"{medal} **{r['username']}** — {r['points']} pts · {r['wins']} win(s)")

    embed = discord.Embed(
        title=f"Name That Movie — {month_label} Leaderboard",
        description="\n".join(lines),
        color=discord.Color.gold(),
    )
    embed.set_footer(text="Resets on the 1st of each month · /ntm leaders for all-time")
    await interaction.response.send_message(embed=embed)


# ── /ntm stats ────────────────────────────────────────────────────────────────

@ntm.command(name="stats", description="Show stats for yourself or another player.")
@app_commands.describe(member="The player to look up (leave blank for yourself)")
async def ntm_stats(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    target = member or interaction.user

    with get_db() as db:
        lb = db.execute(
            "SELECT wins, points, current_streak, best_streak FROM leaderboard WHERE user_id=?",
            (target.id,)
        ).fetchone()

        # Average screenshot solved on (as guesser)
        avg_row = db.execute(
            "SELECT AVG(solved_on_screenshot) as avg_shot, COUNT(*) as cnt "
            "FROM history WHERE winner_id=? AND solved=1",
            (target.id,)
        ).fetchone()

        # Hardest movie they set (most guesses)
        hardest = db.execute(
            "SELECT movie, guess_count FROM history "
            "WHERE uploader_id=? AND guess_count > 0 "
            "ORDER BY guess_count DESC LIMIT 1",
            (target.id,)
        ).fetchone()

        # Movies they set
        movies_set = db.execute(
            "SELECT COUNT(*) as cnt FROM history WHERE uploader_id=?",
            (target.id,)
        ).fetchone()["cnt"]

    if not lb and movies_set == 0:
        await interaction.response.send_message(
            f"No stats found for **{target.display_name}** yet.", ephemeral=True
        )
        return

    wins         = lb["wins"]          if lb else 0
    points       = lb["points"]        if lb else 0
    cur_streak   = lb["current_streak"] if lb else 0
    best_streak  = lb["best_streak"]   if lb else 0
    avg_shot     = avg_row["avg_shot"] if avg_row and avg_row["avg_shot"] else None

    embed = discord.Embed(
        title=f"Stats — {target.display_name}",
        color=discord.Color.blurple(),
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="🏆 Wins",           value=str(wins),   inline=True)
    embed.add_field(name="⭐ Points",          value=str(points), inline=True)
    embed.add_field(name="🔥 Current Streak",  value=str(cur_streak),  inline=True)
    embed.add_field(name="📈 Best Streak",     value=str(best_streak), inline=True)
    embed.add_field(name="🎬 Movies Set",      value=str(movies_set),  inline=True)
    if avg_shot is not None:
        embed.add_field(
            name="📸 Avg Screenshot Guessed On",
            value=f"{avg_shot:.1f}",
            inline=True,
        )
    if hardest:
        embed.add_field(
            name="😈 Hardest Movie Set",
            value=f"**{hardest['movie']}** ({hardest['guess_count']} guesses)",
            inline=False,
        )

    await interaction.response.send_message(embed=embed)


# ── /ntm winner ───────────────────────────────────────────────────────────────

@ntm.command(name="winner", description="Admin: Force-end the current round and assign a new winner.")
@app_commands.describe(member="The user to assign as the next Winner")
async def ntm_winner(interaction: discord.Interaction, member: discord.Member):
    if not is_admin(interaction):
        await interaction.response.send_message(
            "You need the Manage Roles permission to use this.", ephemeral=True
        )
        return

    recap = None
    with get_db() as db:
        row = get_round(db)
        if row and row["active"]:
            log.info("Admin %s force-ended round — movie was: %r", interaction.user.display_name, row["movie"])
            recap = end_round(db, solved=False, winner_id=None, winner_name=None)

    await transfer_winner_role(interaction.guild, member)
    await interaction.response.send_message(
        f"{member.mention} is now the {config.WINNER_ROLE_NAME} and can start the next round with `/ntm movie`.",
        ephemeral=True,
    )

    if recap:
        channel = bot.get_channel(config.GAME_CHANNEL_ID)
        if channel:
            await post_round_recap(
                channel=channel,
                movie=recap["movie"],
                solved=False,
                winner_name=None,
                uploader_name=recap["uploader_name"],
                points=0,
                guess_count=recap["guess_count"],
                solved_on=None,
                screenshot_paths=recap["shot_paths"],
                wrong_guessers=recap.get("wrong_guessers"),
            )


# ── /ntm reset ───────────────────────────────────────────────────────────────

@ntm.command(name="reset", description="Admin: Wipe all stats, history and screenshots. Cannot be undone.")
async def ntm_reset(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message(
            "You need the Manage Roles permission to use this.", ephemeral=True
        )
        return

    # Ask for confirmation via a follow-up with a button
    class ConfirmView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=30)
            self.confirmed = False

        @discord.ui.button(label="Yes, reset everything", style=discord.ButtonStyle.danger)
        async def confirm(self, btn_interaction: discord.Interaction, button: discord.ui.Button):
            if btn_interaction.user.id != interaction.user.id:
                await btn_interaction.response.send_message("Only the person who triggered this can confirm.", ephemeral=True)
                return
            self.confirmed = True
            self.stop()
            await btn_interaction.response.defer()

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, btn_interaction: discord.Interaction, button: discord.ui.Button):
            self.stop()
            await btn_interaction.response.defer()

    view = ConfirmView()
    await interaction.response.send_message(
        "⚠️ **This will permanently delete all stats, history, leaderboard, and screenshots.**\n"
        "This cannot be undone. Are you sure?",
        view=view,
        ephemeral=True,
    )
    await view.wait()

    if not view.confirmed:
        await interaction.edit_original_response(content="Reset cancelled.", view=None)
        return

    # Wipe all screenshot files from disk
    for f in config.SHOTS_DIR.glob("*"):
        f.unlink(missing_ok=True)

    with get_db() as db:
        db.executescript("""
            DELETE FROM history_screenshots;
            DELETE FROM history;
            DELETE FROM leaderboard;
            DELETE FROM monthly_leaderboard;
            DELETE FROM movie_usage;
            DELETE FROM screenshots;
            DELETE FROM wrong_guesses;
            UPDATE round SET
                active=0, movie=NULL, uploader_id=NULL, uploader_name=NULL,
                uploader_avatar=NULL, released=0, reveal_at=NULL,
                round_number=0, lightning=0, guess_count=0, round_id=0
            WHERE id=1;
        """)

    log.info("Full game reset performed by %s", interaction.user.display_name)
    await interaction.edit_original_response(
        content="✅ Game has been fully reset. All stats, history and screenshots have been deleted.",
        view=None,
    )


# ── /ntm help ─────────────────────────────────────────────────────────────────

@ntm.command(name="help", description="How to play Name That Movie.")
async def ntm_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="How to Play — Name That Movie",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="Objective",
        value="Guess the movie from screenshot clues posted in this channel.",
        inline=False,
    )
    embed.add_field(
        name="How a round works",
        value=(
            "1. The current **Winner** sets a movie with `/ntm movie` (+ first screenshot).\n"
            "2. They add screenshots 2 and 3 with `/ntm screenshot`.\n"
            f"3. Screenshots are revealed every **{config.SCREENSHOT_INTERVAL_HOURS} hours** (or "
            f"**{config.LIGHTNING_INTERVAL_HOURS * 60} min** in a ⚡ Lightning Round).\n"
            "4. Players guess with `/ntm guess`.\n"
            "5. If nobody guesses in time, the bot reveals the answer."
        ),
        inline=False,
    )
    embed.add_field(
        name="Scoring",
        value=(
            "🥇 Guess on screenshot 1 → **3 points**\n"
            "🥈 Guess on screenshot 2 → **2 points**\n"
            "🥉 Guess on screenshot 3 → **1 point**"
        ),
        inline=False,
    )
    embed.add_field(
        name="⚡ Lightning Rounds",
        value=f"Each round has a {int(config.LIGHTNING_ROUND_PROBABILITY * 100)}% chance of being a lightning round — faster screenshots, think quickly!",
        inline=False,
    )
    embed.add_field(
        name="Commands",
        value=(
            "`/ntm guess` — Submit a guess\n"
            "`/ntm movie` — Start a round (Winner only)\n"
            "`/ntm screenshot` — Add screenshots 2 & 3 (Winner only)\n"
            "`/ntm skip` — Give up and reveal the answer (Winner only)\n"
            "`/ntm currentcheck` — See the current answer (Winner only)\n"
            "`/ntm usagecheck` — Check if a movie has been used before\n"
            "`/ntm last` — Show the previous round's screenshots\n"
            "`/ntm repost` — Repost currently revealed screenshots\n"
            "`/ntm leaders` — All-time leaderboard\n"
            "`/ntm stats` — Personal stats\n"
            "`/ntm monthly` — This month's leaderboard\n"
            "`/ntm reset` — Wipe all data (Admin only)\n"
            "`/ntm help` — This message"
        ),
        inline=False,
    )
    embed.set_footer(text="Guessing is case-insensitive. Good luck!")
    await interaction.response.send_message(embed=embed)


# ── Run ───────────────────────────────────────────────────────────────────────

bot.run(config.DISCORD_TOKEN)
