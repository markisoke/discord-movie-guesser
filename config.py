import os
from pathlib import Path

# ── Discord credentials (loaded from environment / .env) ──────────────────────

DISCORD_TOKEN: str = os.environ["DISCORD_TOKEN"]
GUILD_ID: int      = int(os.environ["GUILD_ID"])
GAME_CHANNEL_ID: int  = int(os.environ["GAME_CHANNEL_ID"])
WINNER_ROLE_NAME: str = os.environ.get("WINNER_ROLE_NAME", "Winner")

# ── Game timing ───────────────────────────────────────────────────────────────

# Hours between each screenshot release in a normal round
SCREENSHOT_INTERVAL_HOURS: int = 8

# Hours after the last screenshot before auto-reveal in a normal round
REVEAL_AFTER_HOURS: int = 48

# ── Lightning rounds ──────────────────────────────────────────────────────────

# Set to True to enable lightning rounds, False to disable them entirely
LIGHTNING_ROUNDS_ENABLED: bool = True

# Probability (0.0 – 1.0) that any given round is a lightning round
# 0.05 = 5% chance, 0.10 = 10% chance, etc.
LIGHTNING_ROUND_PROBABILITY: float = 0.05

# Hours between screenshot releases in a lightning round (0.5 = 30 minutes)
LIGHTNING_INTERVAL_HOURS: float = 0.5

# Hours after last screenshot before auto-reveal in a lightning round
LIGHTNING_REVEAL_HOURS: int = 2

# ── Hot streak announcements ──────────────────────────────────────────────────

# Set to True to publicly announce when a player hits a hot streak
HOT_STREAK_ENABLED: bool = True

# Minimum streak length before a public announcement is made
HOT_STREAK_THRESHOLD: int = 3

# ── Weekly summary ────────────────────────────────────────────────────────────

# Set to True to post a weekly summary in the game channel
WEEKLY_SUMMARY_ENABLED: bool = True

# Day of week to post the summary (0 = Monday, 6 = Sunday)
WEEKLY_SUMMARY_DAY: int = 0  # Monday

# Hour of day to post the summary (24h UTC)
WEEKLY_SUMMARY_HOUR: int = 9  # 09:00 UTC

# ── Monthly leaderboard ───────────────────────────────────────────────────────

# Set to True to track a separate monthly leaderboard
MONTHLY_LEADERBOARD_ENABLED: bool = True

# ── Recap wrong guesses ───────────────────────────────────────────────────────

# Maximum number of unique wrong guessers to list in the recap
# Set to 0 to disable the wrong guesser list entirely
MAX_WRONG_GUESSES_SHOWN: int = 5

# ── Screenshot retention ──────────────────────────────────────────────────────

# How many completed rounds worth of screenshots to keep on disk
SCREENSHOT_RETENTION_ROUNDS: int = 2

# ── Leaderboard ───────────────────────────────────────────────────────────────

LEADERBOARD_SIZE: int = 10

# ── Storage paths ─────────────────────────────────────────────────────────────

DATA_DIR:  Path = Path("/data")
DB_PATH:   Path = DATA_DIR / "ntm.db"
SHOTS_DIR: Path = DATA_DIR / "screenshots"
