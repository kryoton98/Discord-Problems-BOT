import discord
from discord.ext import commands, tasks
from discord import app_commands
import sqlite3
import os
from datetime import datetime, timedelta, timezone, time
from zoneinfo import ZoneInfo
import logging
from typing import Optional

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Optional: load .env if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ============================================================================
# CONFIG
# ============================================================================

DB_NAME = "quiz_bot.db"

# Role that can manually post problems if needed
CURATOR_ROLE = "Curator"

# Timezone and scheduled daily post time (12:00 PM IST)
IST = ZoneInfo("Asia/Kolkata")  # India Standard Time
DAILY_POST_TIME = time(hour=12, minute=0, tzinfo=IST)

# Where the automatic daily post goes.
# Fill these with your actual IDs (right-click server/channel -> "Copy ID").
AUTO_GUILD_ID = 0       # e.g. 123456789012345678
AUTO_CHANNEL_ID = 0     # e.g. 234567890123456789

BASE_POINTS = 1000               # starting points for a problem
DECAY_INTERVAL_SECONDS = 120     # 1 point lost every 2 minutes
WRONG_PENALTY = 50               # points deducted per wrong answer
AUTHOR_BONUS_PER_SOLVE = 20      # points given to author per correct solve
MAX_DECAY_HOURS = 4              # Decay stops after 4 hours

# ============================================================================
# DATABASE SETUP
# ============================================================================

def init_db():
    """Initialize database with all required tables."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    # Problems table (includes author_id and image_url)
    c.execute(
        """CREATE TABLE IF NOT EXISTS problems (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE NOT NULL,
        statement TEXT NOT NULL,
        topics TEXT,
        difficulty TEXT,
        setter TEXT,
        source TEXT,
        answer TEXT NOT NULL,
        opens_at TIMESTAMP,
        closes_at TIMESTAMP,
        is_active INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        author_id TEXT,
        image_url TEXT
    )"""
    )

    # Submissions table (includes points; can be positive or negative)
    c.execute(
        """CREATE TABLE IF NOT EXISTS submissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        problem_id INTEGER NOT NULL,
        answer TEXT NOT NULL,
        is_correct INTEGER DEFAULT 0,
        points INTEGER DEFAULT 0,
        submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (problem_id) REFERENCES problems(id)
    )"""
    )

    # Ratings table: 1 row per (user, problem); 1-5 stars
    c.execute(
        """CREATE TABLE IF NOT EXISTS problem_ratings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        problem_id INTEGER NOT NULL,
        rating INTEGER NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, problem_id),
        FOREIGN KEY (problem_id) REFERENCES problems(id)
    )"""
    )

    conn.commit()
    conn.close()
    logger.info("Database initialized")

# ============================================================================
# DATABASE HELPER FUNCTIONS
# ============================================================================

def get_active_problem():
    """Get the currently active problem."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT * FROM problems WHERE is_active = 1")
    result = c.fetchone()
    conn.close()
    return result

def get_problem_by_code(code: str):
    """Get problem by its short code."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT * FROM problems WHERE code = ?", (code,))
    result = c.fetchone()
    conn.close()
    return result

def get_latest_problem_code() -> Optional[str]:
    """
    Next problem code for auto poster.

    Chooses the *oldest* problem that has never been opened yet
    (opens_at IS NULL), so problems are used in FIFO order.
    """
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "SELECT code FROM problems "
        "WHERE opens_at IS NULL "
        "ORDER BY created_at ASC LIMIT 1"
    )
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def activate_problem(code: str):
    """Set a problem as active and deactivate all others (24-hour window)."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    now = datetime.now(timezone.utc)
    closes_at = now + timedelta(hours=24)

    # Deactivate all
    c.execute("UPDATE problems SET is_active = 0")

    # Activate target
    c.execute(
        """UPDATE problems
           SET is_active = 1, opens_at = ?, closes_at = ?
           WHERE code = ?""",
        (now.isoformat(), closes_at.isoformat(), code),
    )

    conn.commit()
    conn.close()

def check_problem_open(code: str) -> bool:
    """Check if a problem is currently open for submissions."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    now = datetime.now(timezone.utc).isoformat()
    c.execute(
        """SELECT 1 FROM problems
           WHERE code = ? AND opens_at <= ? AND closes_at >= ?""",
        (code, now, now),
    )
    result = c.fetchone()
    conn.close()
    return result is not None

def user_already_solved(user_id: str, problem_id: int) -> bool:
    """Check if user already solved this problem correctly."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        """SELECT 1 FROM submissions
           WHERE user_id = ? AND problem_id = ? AND is_correct = 1""",
        (user_id, problem_id),
    )
    result = c.fetchone()
    conn.close()
    return result is not None

def submit_answer(user_id: str, problem_id: int, answer: str, is_correct: bool, points: int):
    """Record an answer submission (points may be negative for penalties)."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        """INSERT INTO submissions (user_id, problem_id, answer, is_correct, points)
           VALUES (?, ?, ?, ?, ?)""",
        (user_id, problem_id, answer, 1 if is_correct else 0, points),
    )
    conn.commit()
    conn.close()

def get_user_total_solves(user_id: str) -> int:
    """Get total number of distinct problems solved by user."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        """SELECT COUNT(DISTINCT problem_id) FROM submissions
           WHERE user_id = ? AND is_correct = 1""",
        (user_id,),
    )
    result = c.fetchone()[0]
    conn.close()
    return result

def get_leaderboard_overall(limit: int = 10):
    """
    Overall solver leaderboard by total points (including penalties),
    then earliest correct solve.
    """
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        """SELECT user_id,
                  SUM(points) AS total_points,
                  COUNT(DISTINCT CASE WHEN is_correct = 1 THEN problem_id END) AS solves,
                  MIN(CASE WHEN is_correct = 1 THEN submitted_at END) AS first_solve
           FROM submissions
           GROUP BY user_id
           ORDER BY total_points DESC, first_solve ASC
           LIMIT ?""",
        (limit,),
    )
    results = c.fetchall()
    conn.close()
    return results

def get_leaderboard_today(problem_id: int, limit: int = 10):
    """
    Today's leaderboard for a specific problem.

    Points are the sum of all submissions (correct and wrong),
    but only users with at least one correct submission are shown.
    """
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        """SELECT user_id,
                  SUM(points) AS total_points,
                  MIN(CASE WHEN is_correct = 1 THEN submitted_at END) AS first_correct
           FROM submissions
           WHERE problem_id = ?
           GROUP BY user_id
           HAVING MAX(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END) = 1
           ORDER BY total_points DESC, first_correct ASC
           LIMIT ?""",
        (problem_id, limit),
    )
    results = c.fetchall()
    conn.close()
    return results

def add_problem(
    code: str,
    statement: str,
    topics: str,
    difficulty: str,
    setter: str,
    source: str,
    answer: str,
    author_id: Optional[str],
    image_url: Optional[str],
) -> int:
    """Add a new problem row."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        """INSERT INTO problems
           (code, statement, topics, difficulty, setter, source, answer,
            author_id, image_url)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (code, statement, topics, difficulty, setter, source, answer, author_id, image_url),
    )
    conn.commit()
    problem_id = c.lastrowid
    conn.close()
    return problem_id

def get_all_problems():
    """Get all problems with basic info."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "SELECT id, code, difficulty, is_active FROM problems ORDER BY created_at DESC"
    )
    results = c.fetchall()
    conn.close()
    return results

def user_recent_problem_count(user_id: str, hours: int = 24) -> int:
    """Number of problems this user created in the last X hours."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    c.execute(
        """SELECT COUNT(*) FROM problems
           WHERE author_id = ? AND created_at >= ?""",
        (user_id, cutoff),
    )
    count = c.fetchone()[0]
    conn.close()
    return count

def add_or_update_rating(user_id: str, problem_id: int, rating: int):
    """Insert or update a rating for (user, problem)."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        """INSERT INTO problem_ratings (user_id, problem_id, rating)
           VALUES (?, ?, ?)
           ON CONFLICT(user_id, problem_id)
           DO UPDATE SET rating = excluded.rating,
                        created_at = CURRENT_TIMESTAMP""",
        (user_id, problem_id, rating),
    )
    conn.commit()
    conn.close()

def has_solved_problem(user_id: str, problem_id: int) -> bool:
    """Return True if user has at least one correct submission for this problem."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        """SELECT 1 FROM submissions
           WHERE user_id = ? AND problem_id = ? AND is_correct = 1
           LIMIT 1""",
        (user_id, problem_id),
    )
    result = c.fetchone()
    conn.close()
    return result is not None

def get_curator_leaderboard(limit: int = 10):
    """Leaderboard of problem creators (curators)."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        """SELECT
             author_id,
             COUNT(DISTINCT p.id) AS problems_created,
             COALESCE(AVG(r.rating), 0) AS avg_rating,
             COUNT(r.id) AS ratings_count
           FROM problems p
           LEFT JOIN problem_ratings r ON r.problem_id = p.id
           WHERE author_id IS NOT NULL
           GROUP BY author_id
           ORDER BY problems_created DESC, avg_rating DESC
           LIMIT ?""",
        (limit,),
    )
    results = c.fetchall()
    conn.close()
    return results

def unscore_submissions(problem_id: int, user_id: Optional[str] = None) -> int:
    """
    Set is_correct=0 and points=0 for a problem.
    If user_id is given, affects only that user; otherwise all users.
    Returns number of rows updated.
    """
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    if user_id is None:
        c.execute(
            """UPDATE submissions
               SET is_correct = 0, points = 0
               WHERE problem_id = ?""",
            (problem_id,),
        )
    else:
        c.execute(
            """UPDATE submissions
               SET is_correct = 0, points = 0
               WHERE problem_id = ? AND user_id = ?""",
            (problem_id, user_id),
        )

    affected = c.rowcount
    conn.commit()
    conn.close()
    return affected

# ============================================================================
# BOT SETUP
# ============================================================================

intents = discord.Intents.default()
intents.message_content = True
intents.dm_messages = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    """Bot is ready."""
    logger.info(f"Logged in as {bot.user}")

    # Sync slash commands
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} command(s)")
    except Exception as e:
        logger.error(f"Failed to sync commands: {e}")

    # Start scheduled daily task
    if not daily_post_task.is_running():
        daily_post_task.start()
        logger.info("Started daily_post_task")

# ============================================================================
# CORE POSTING HELPER (shared by command + scheduler)
# ============================================================================

async def post_problem_to_channel(channel: discord.TextChannel, code: str):
    """Activate and post the given problem code into a channel."""
    prob = get_problem_by_code(code)
    if not prob:
        raise ValueError(f"Problem {code} not found")

    # Activate problem (sets opens_at/closes_at)
    activate_problem(code)

    (
        problem_id,
        _code,
        statement,
        topics,
        difficulty,
        setter,
        source,
        answer,
        opens_at,
        closes_at,
        _is_active,
        _created_at,
        author_id,
        image_url,
    ) = prob

    embed1 = discord.Embed(
        title=f"Day {code} — {datetime.now().strftime('%a %d %b %Y')}",
        description=statement,
        color=discord.Color.blurple(),
    )
    if image_url:
        embed1.set_image(url=image_url)

    embed2 = discord.Embed(title="Problem Info", color=discord.Color.dark_gray())
    embed2.add_field(name="Topics", value=topics or "N/A", inline=False)
    embed2.add_field(
        name="Difficulty",
        value=str(difficulty) if difficulty else "N/A",
        inline=True,
    )
    embed2.add_field(name="Setter", value=setter or "N/A", inline=True)
    embed2.add_field(name="Source", value=source or "N/A", inline=False)
    if author_id:
        embed2.add_field(
            name="Author ID",
            value=author_id,
            inline=False,
        )
    embed2.add_field(
        name="Window",
        value=(
            f"⏰ Open for 24 hours\n"
            f"Base points: {BASE_POINTS}\n"
            f"Time decay: −1 point every 2 minutes (Caps at {MAX_DECAY_HOURS} hours)\n"
            f"Wrong answer penalty: −{WRONG_PENALTY} points per attempt\n"
            f"DM me: `{code} <answer>`"
        ),
        inline=False,
    )

    await channel.send(embeds=[embed1, embed2])

# ============================================================================
# SCHEDULED DAILY TASK (12:00 PM IST)
# ============================================================================

@tasks.loop(time=DAILY_POST_TIME)
async def daily_post_task():
    """Automatically post the daily problem at 12:00 PM IST."""
    await bot.wait_until_ready()

    code = get_latest_problem_code()
    if code is None:
        # No unopened problems left – post a fallback message instead of silently skipping
        logger.warning(
            "daily_post_task: no (unopened) problems in DB, posting exhaustion message."
        )

        # Resolve guild and channel just like for a normal post
        guild = None
        if AUTO_GUILD_ID:
            guild = bot.get_guild(AUTO_GUILD_ID)
        elif bot.guilds:
            guild = bot.guilds[0]

        if not guild:
            logger.warning("daily_post_task: bot is not in the target guild.")
            return

        channel: Optional[discord.TextChannel] = None
        if AUTO_CHANNEL_ID:
            ch = guild.get_channel(AUTO_CHANNEL_ID)
            if isinstance(ch, discord.TextChannel):
                channel = ch
        else:
            for chan in guild.text_channels:
                if chan.permissions_for(guild.me).send_messages:
                    channel = chan
                    break

        if channel is None:
            logger.warning("daily_post_task: no writable text channel found.")
            return

        await channel.send(
            "😔 **No new puzzle today.**\n"
            "The problem set is currently exhausted.\n"
            "Curators, please create new problems with `/create_problem`!"
        )
        return

    # Resolve guild and channel for normal auto‑post
    guild = None
    if AUTO_GUILD_ID:
        guild = bot.get_guild(AUTO_GUILD_ID)
    elif bot.guilds:
        guild = bot.guilds[0]

    if not guild:
        logger.warning("daily_post_task: bot is not in the target guild.")
        return

    channel: Optional[discord.TextChannel] = None
    if AUTO_CHANNEL_ID:
        ch = guild.get_channel(AUTO_CHANNEL_ID)
        if isinstance(ch, discord.TextChannel):
            channel = ch
    else:
        for chan in guild.text_channels:
            if chan.permissions_for(guild.me).send_messages:
                channel = chan
                break

    if channel is None:
        logger.warning("daily_post_task: no writable text channel found.")
        return

    try:
        await post_problem_to_channel(channel, code)
        logger.info(f"Auto-posted daily problem {code} in #{channel.name}")
    except Exception as e:
        logger.error(f"Error in daily_post_task: {e}")

# ============================================================================
# DM MESSAGE HANDLER (answers + point computation + penalties)
# ============================================================================

@bot.event
async def on_message(message: discord.Message):
    """Handle incoming messages, especially DMs for answer submission."""
    if message.author.bot:
        await bot.process_commands(message)
        return

    # Only process DMs (guild is None)
    if message.guild is None:
        try:
            parts = message.content.strip().split(maxsplit=1)
            if len(parts) != 2:
                await message.author.send(
                    "📝 **Invalid format!**\n"
                    "Please send your answer as: `<problem_code> <answer>`\n\n"
                    "Example: `2089 42`"
                )
                await bot.process_commands(message)
                return

            problem_code, user_answer = parts

            # Get problem by code
            prob = get_problem_by_code(problem_code)
            if not prob:
                await message.author.send(f"❌ Problem code `{problem_code}` not found.")
                await bot.process_commands(message)
                return

            problem_id = prob[0]

            # 12 is the index for author_id in your DB schema
            author_id = prob[12]

            # Prevent author from answering their own problem
            if author_id == str(message.author.id):
                await message.author.send("❌ You created this problem! You cannot submit an answer for points.")
                await bot.process_commands(message)
                return

            # Check if problem is open
            if not check_problem_open(problem_code):
                await message.author.send(
                    f"⏰ Problem `{problem_code}` is not currently open.\n"
                    f"Wait for the next daily problem!"
                )
                await bot.process_commands(message)
                return

            # Check if already solved
            if user_already_solved(str(message.author.id), problem_id):
                await message.author.send(
                    f"✅ You already solved problem `{problem_code}`!"
                )
                await bot.process_commands(message)
                return

            # Check answer
            correct_answer = prob[7].strip().lower()  # answer column
            user_answer_clean = user_answer.strip().lower()
            is_correct = correct_answer == user_answer_clean

            # Compute points
            points = 0
            if is_correct:
                # Time-decay scoring from problem open time
                now = datetime.now(timezone.utc)
                opens_at_str = prob[8]  # opens_at
                try:
                    opens_at_dt = datetime.fromisoformat(opens_at_str)
                except Exception:
                    opens_at_dt = now

                elapsed_seconds = (now - opens_at_dt).total_seconds()
                
                # CAP DECAY at 4 hours
                max_decay_seconds = MAX_DECAY_HOURS * 3600
                if elapsed_seconds > max_decay_seconds:
                    effective_seconds = max_decay_seconds
                else:
                    effective_seconds = elapsed_seconds

                decay_steps = int(max(0, effective_seconds) // DECAY_INTERVAL_SECONDS)
                points = max(0, BASE_POINTS - decay_steps)
            else:
                # Penalty for wrong attempt
                points = -WRONG_PENALTY

            # Record submission (includes penalties)
            submit_answer(
                str(message.author.id), problem_id, user_answer, is_correct, points
            )

            # If correct, AWARD BONUS TO AUTHOR
            if is_correct and author_id:
                 # Give author bonus points
                 # We record this as a special "bonus" submission so it shows up in their total score
                 submit_answer(
                    author_id, problem_id, "AUTHOR_BONUS", 1, AUTHOR_BONUS_PER_SOLVE
                 )

            # Calculate total points for this user on this problem
            user_id = str(message.author.id)
            conn = sqlite3.connect(DB_NAME)
            c = conn.cursor()
            c.execute(
                "SELECT SUM(points) FROM submissions WHERE user_id = ? AND problem_id = ?",
                (user_id, problem_id),
            )
            row = c.fetchone()
            conn.close()
            problem_points_total = row[0] if row and row[0] is not None else 0

            if is_correct:
                total_solves = get_user_total_solves(user_id)
                await message.author.send(
                    f"✅ **Correct!**\n"
                    f"Your solve for Day `{problem_code}` has been recorded.\n"
                    f"🏅 This correct submission is worth **{points}** points.\n"
                    f"📊 Your total for this problem (after penalties): "
                    f"**{problem_points_total}** points.\n\n"
                    f"📊 Total solved: **{total_solves}** problem(s)\n\n"
                    f"⭐ If you want, rate this problem in the server with:\n"
                    f"`/rate_problem code:{problem_code} rating:1-5`"
                )
            else:
                await message.author.send(
                    f"❌ Not quite right for `{problem_code}`.\n"
                    f"➖ You lost **{WRONG_PENALTY}** points.\n"
                    f"📊 Your total for this problem is now: "
                    f"**{problem_points_total}** points.\n"
                    f"The 24-hour window is still open. Try again!"
                )

        except Exception as e:
            logger.error(f"Error processing DM: {e}")
            try:
                await message.author.send(
                    "⚠️ An error occurred processing your answer."
                )
            except Exception:
                pass

    await bot.process_commands(message)

# ============================================================================
# PROBLEM CREATION (any user, 1 per 24h)
# ============================================================================

@bot.tree.command(
    name="create_problem", description="Create a new puzzle/problem (1 per 24h)"
)
@app_commands.describe(
    code="Short code like 2089",
    answer="Official answer string (what solvers must DM)",
    difficulty="Difficulty 1-5",
    topics="Comma-separated tags (e.g. game theory,probability)",
    statement="Full problem statement text",
    image="Optional image/diagram attachment",
)
async def create_problem(
    interaction: discord.Interaction,
    code: str,
    answer: str,
    difficulty: app_commands.Range[int, 1, 5],
    topics: str,
    statement: str,
    image: Optional[discord.Attachment] = None,
):
    """User-facing command to create a problem; limited to 1 per 24h."""
    user_id = str(interaction.user.id)

    await interaction.response.defer(ephemeral=True)

    # Enforce 1 problem per 24 hours
    recent = user_recent_problem_count(user_id, hours=24)
    if recent >= 1:
        await interaction.followup.send(
            "❌ You have already created a problem in the last 24 hours.\n"
            "Please wait before creating another.",
            ephemeral=True,
        )
        return

    # Validate unique code
    if get_problem_by_code(code) is not None:
        await interaction.followup.send(
            f"❌ Problem code `{code}` is already in use. Choose another.",
            ephemeral=True,
        )
        return

    # Ensure at least some statement text
    if not statement.strip():
        await interaction.followup.send(
            "❌ Problem statement cannot be empty.", ephemeral=True
        )
        return

    image_url = image.url if image is not None else None

    try:
        problem_id = add_problem(
            code=code,
            statement=statement,
            topics=topics,
            difficulty=str(difficulty),
            setter=interaction.user.display_name,
            source="User-created",
            answer=answer.strip(),
            author_id=user_id,
            image_url=image_url,
        )

        # Confirmation embed
        embed = discord.Embed(
            title=f"Problem {code} created",
            description=statement,
            color=discord.Color.green(),
        )
        embed.add_field(name="Difficulty", value=str(difficulty), inline=True)
        embed.add_field(name="Topics", value=topics or "N/A", inline=False)
        embed.add_field(name="Answer", value=f"||{answer}||", inline=False)
        if image_url:
            embed.set_image(url=image_url)

        await interaction.followup.send(
            content=(
                f"✅ Problem `{code}` created (ID: {problem_id}).\n"
                f"It can be auto-posted at 12 PM IST by the bot, "
                f"or manually via `/post_today {code}` if you like."
            ),
            embed=embed,
            ephemeral=True,
        )

    except Exception as e:
        logger.error(f"Error in create_problem: {e}")
        await interaction.followup.send("⚠️ An error occurred.", ephemeral=True)

# ============================================================================
# CURATOR COMMANDS (manual posting, listing, unscoring)
# ============================================================================

@bot.tree.command(name="post_today", description="Post today's problem (Curator only)")
@app_commands.describe(code="Problem code (e.g., 2089)")
async def post_today(interaction: discord.Interaction, code: str):
    """Manually post the daily problem into the current channel."""
    # Check curator role
    if not any(role.name == CURATOR_ROLE for role in interaction.user.roles):
        await interaction.response.send_message(
            "❌ You need the Curator role.", ephemeral=True
        )
        return

    try:
        if not interaction.channel or not isinstance(
            interaction.channel, discord.TextChannel
        ):
            await interaction.response.send_message(
                "❌ No text channel found.", ephemeral=True
            )
            return

        await post_problem_to_channel(interaction.channel, code)

        await interaction.response.send_message(
            f"✅ Problem `{code}` posted successfully!", ephemeral=True
        )

    except Exception as e:
        logger.error(f"Error posting problem: {e}")
        await interaction.response.send_message("⚠️ An error occurred.", ephemeral=True)

@bot.tree.command(
    name="unscore_problem", description="Remove scores for a problem (Curator only)"
)
@app_commands.describe(
    code="Problem code (e.g., 2089)",
    user="Optional user to unscore; leave empty to clear everyone",
)
async def unscore_problem(
    interaction: discord.Interaction,
    code: str,
    user: Optional[discord.User] = None,
):
    """Zero out points / correct flags for a problem."""
    # Curator check
    if not any(role.name == CURATOR_ROLE for role in interaction.user.roles):
        await interaction.response.send_message(
            "❌ You need the Curator role.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    prob = get_problem_by_code(code)
    if not prob:
        await interaction.followup.send(
            f"❌ Problem `{code}` not found.", ephemeral=True
        )
        return

    problem_id = prob[0]

    try:
        if user is None:
            rows = unscore_submissions(problem_id)
            if rows == 0:
                msg = f"ℹ️ No submissions found for `{code}`."
            else:
                msg = (
                    f"✅ Cleared scores for **{rows}** submission(s) "
                    f"on problem `{code}`."
                )
        else:
            rows = unscore_submissions(problem_id, user_id=str(user.id))
            if rows == 0:
                msg = (
                    f"ℹ️ `{user.display_name}` has no submissions "
                    f"for `{code}`."
                )
            else:
                msg = (
                    f"✅ Cleared scores for `{user.display_name}` "
                    f"on problem `{code}`."
                )

        await interaction.followup.send(msg, ephemeral=True)

    except Exception as e:
        logger.error(f"Error in unscore_problem: {e}")
        await interaction.followup.send("⚠️ An error occurred.", ephemeral=True)

@bot.tree.command(name="list_problems", description="List all problems")
async def list_problems(interaction: discord.Interaction):
    """List all problems with basic info."""
    await interaction.response.defer()

    try:
        problems = get_all_problems()

        if not problems:
            await interaction.followup.send("📭 No problems found.")
            return

        embed = discord.Embed(title="All Problems", color=discord.Color.blue())

        for prob_id, code, difficulty, is_active in problems:
            status = "🔴 Active" if is_active else "⚪ Inactive"
            embed.add_field(
                name=f"{code}",
                value=f"Difficulty: {difficulty or 'N/A'} | {status}",
                inline=False,
            )

        await interaction.followup.send(embed=embed)

    except Exception as e:
        logger.error(f"Error listing problems: {e}")
        await interaction.followup.send("⚠️ An error occurred.")

# ============================================================================
# SOLVER LEADERBOARD COMMANDS
# ============================================================================

@bot.tree.command(name="leaderboard", description="View solver leaderboard")
@app_commands.describe(period="'overall' or 'today'")
async def leaderboard(interaction: discord.Interaction, period: str = "overall"):
    """Display solver leaderboard."""
    await interaction.response.defer()

    try:
        if period.lower() == "overall":
            results = get_leaderboard_overall(10)

            embed = discord.Embed(
                title="🏆 Overall Solver Leaderboard",
                color=discord.Color.gold(),
            )

            for idx, (user_id, total_points, solves, _first_solve) in enumerate(
                results, 1
            ):
                try:
                    user = await bot.fetch_user(int(user_id))
                    name = user.name
                except Exception:
                    name = f"User {user_id}"

                embed.add_field(
                    name=f"#{idx} {name}",
                    value=f"Points: **{total_points}** | Solves: **{solves}**",
                    inline=False,
                )

            await interaction.followup.send(embed=embed)

        elif period.lower() == "today":
            prob = get_active_problem()
            if not prob:
                await interaction.followup.send("📭 No active problem right now.")
                return

            problem_id = prob[0]
            code = prob[1]
            results = get_leaderboard_today(problem_id, 10)

            embed = discord.Embed(
                title=f"🏆 Today's Leaderboard - Day {code}",
                color=discord.Color.gold(),
            )

            for idx, (user_id, points, submitted_at) in enumerate(results, 1):
                try:
                    user = await bot.fetch_user(int(user_id))
                    name = user.name
                except Exception:
                    name = f"User {user_id}"

                try:
                    time_str = datetime.fromisoformat(submitted_at).strftime(
                        "%H:%M:%S"
                    )
                except Exception:
                    time_str = submitted_at

                embed.add_field(
                    name=f"#{idx} {name}",
                    value=f"Points: **{points}** | ⏰ {time_str}",
                    inline=False,
                )

            await interaction.followup.send(embed=embed)

        else:
            await interaction.followup.send("❌ Use 'overall' or 'today'.")

    except Exception as e:
        logger.error(f"Error displaying leaderboard: {e}")
        await interaction.followup.send("⚠️ An error occurred.")

# ============================================================================
# RATING & CURATOR LEADERBOARD COMMANDS
# ============================================================================

@bot.tree.command(name="rate_problem", description="Rate a problem you solved (1-5)")
@app_commands.describe(
    code="Problem code (e.g., 2089)",
    rating="Rating from 1 (bad) to 5 (great)",
)
async def rate_problem(
    interaction: discord.Interaction,
    code: str,
    rating: app_commands.Range[int, 1, 5],
):
    """Allow solvers to rate problems they have solved."""
    await interaction.response.defer(ephemeral=True)
    user_id = str(interaction.user.id)

    prob = get_problem_by_code(code)
    if not prob:
        await interaction.followup.send(
            f"❌ Problem `{code}` not found.", ephemeral=True
        )
        return

    problem_id = prob[0]

    if not has_solved_problem(user_id, problem_id):
        await interaction.followup.send(
            "❌ You can only rate problems you have solved correctly.",
            ephemeral=True,
        )
        return

    try:
        add_or_update_rating(user_id, problem_id, rating)
        await interaction.followup.send(
            f"✅ Recorded your rating **{rating}** for problem `{code}`.",
            ephemeral=True,
        )
    except Exception as e:
        logger.error(f"Error in rate_problem: {e}")
        await interaction.followup.send("⚠️ An error occurred.", ephemeral=True)

@bot.tree.command(
    name="curator_leaderboard",
    description="View problem creator (curator) leaderboard",
)
async def curator_leaderboard(interaction: discord.Interaction):
    """Leaderboard of users who created problems, sorted by problems and rating."""
    await interaction.response.defer()

    try:
        results = get_curator_leaderboard(10)
        if not results:
            await interaction.followup.send("📭 No curator data yet.")
            return

        embed = discord.Embed(
            title="📚 Curator Leaderboard (Problem Creators)",
            color=discord.Color.purple(),
        )

        for idx, (author_id, problems_created, avg_rating, ratings_count) in enumerate(
            results, 1
        ):
            try:
                user = await bot.fetch_user(int(author_id))
                name = user.name
            except Exception:
                name = f"User {author_id}"

            embed.add_field(
                name=f"#{idx} {name}",
                value=(
                    f"Problems created: **{problems_created}**\n"
                    f"Avg rating: **{avg_rating:.2f}** "
                    f"({ratings_count} rating(s))"
                ),
                inline=False,
            )

        await interaction.followup.send(embed=embed)

    except Exception as e:
        logger.error(f"Error in curator_leaderboard: {e}")
        await interaction.followup.send("⚠️ An error occurred.")

# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    logger.info(">>> STARTING Problems BOT v0.3 (with unscore_problem) <<<")

    init_db()

    token = os.getenv("DISCORD_TOKEN")
    if not token:
        logger.error("DISCORD_TOKEN environment variable not set!")
        raise SystemExit(1)

    bot.run(token)
