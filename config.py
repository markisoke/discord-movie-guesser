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

# ── Screenshot retention ──────────────────────────────────────────────────────

# How many completed rounds worth of screenshots to keep on disk
SCREENSHOT_RETENTION_ROUNDS: int = 2

# ── Leaderboard ───────────────────────────────────────────────────────────────

LEADERBOARD_SIZE: int = 10

# ── Storage paths ─────────────────────────────────────────────────────────────

DATA_DIR:  Path = Path("/data")
DB_PATH:   Path = DATA_DIR / "ntm.db"
SHOTS_DIR: Path = DATA_DIR / "screenshots"
