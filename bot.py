import discord
from discord.ext import commands, tasks
from discord import app_commands, ui
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
CURATOR_ROLE = "Verifier"
# Role that can preview unreleased problems
VERIFIER_ROLE = "Verifier"

# Timezone and scheduled daily post time (12:00 PM IST)
IST = ZoneInfo("Asia/Kolkata")  # India Standard Time
DAILY_POST_TIME = time(hour=12, minute=0, tzinfo=IST)

# Where the automatic daily post goes.
# Fill these with your actual IDs (right-click server/channel -> "Copy ID").
AUTO_GUILD_ID = 0     # e.g. 123456789012345678
AUTO_CHANNEL_ID = 0    # e.g. 234567890123456789
VERIFIER_CHANNEL_ID = 0  # e.g. 345678901234567890 
ASSET_CHANNEL_ID = 0    # e.g. 456789012345678901

BASE_POINTS = 1000               # starting points for a problem
DECAY_INTERVAL_SECONDS = 120     # 1 point lost every 2 minutes
WRONG_PENALTY = 50               # points deducted per wrong answer
AUTHOR_BONUS_PER_SOLVE = 20      # points given to author per correct solve
MAX_DECAY_HOURS = 4              # Decay stops after 4 hours

# ============================================================================
# DATABASE SETUP
# ============================================================================

def init_db():
    """Initialize database with all required tables and a default System problem."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    # Problems table
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
        image_url TEXT,
        editorial_url TEXT,
        review_status TEXT DEFAULT 'pending'
    )"""
    )
    
    # Migrations for older DBs
    try: c.execute("ALTER TABLE problems ADD COLUMN editorial_url TEXT")
    except: pass
    try: c.execute("ALTER TABLE problems ADD COLUMN review_status TEXT DEFAULT 'pending'")
    except: pass

    # Submissions table
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

    # Ratings table
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

    # Users table for streaks
    c.execute(
        """CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY,
        current_streak INTEGER DEFAULT 0,
        max_streak INTEGER DEFAULT 0,
        last_solve_date TEXT
    )"""
    )

    # --- NEW: Insert System Dummy Problem (ID 0) ---
    # We use INSERT OR IGNORE to make sure it only happens once.
    # Note: AUTOINCREMENT usually starts at 1, so we explicitly set ID=0.
    try:
        c.execute(
            """INSERT OR IGNORE INTO problems 
            (id, code, statement, topics, difficulty, setter, source, answer, review_status)
            VALUES (0, 'SYSTEM', 'Points Adjustment Placeholder', 'System', '0', 'System', 'System', 'SYSTEM', 'approved')"""
        )
    except Exception as e:
        logger.warning(f"Could not insert system problem: {e}")

    conn.commit()
    conn.close()
    logger.info("Database initialized (System Problem ID 0 check complete)")


# ============================================================================
# DATABASE HELPER FUNCTIONS
# ============================================================================
async def save_attachment_permanently(attachment: discord.Attachment, bot: commands.Bot) -> Optional[str]:
    """Re-upload an attachment to a permanent asset channel and return its CDN URL."""
    if ASSET_CHANNEL_ID == 0:
        return None

    for guild in bot.guilds:
        channel = guild.get_channel(ASSET_CHANNEL_ID)
        if isinstance(channel, discord.TextChannel):
            file = await attachment.to_file()
            msg = await channel.send(file=file)
            if msg.attachments:
                return msg.attachments[0].url
            break

    return None


def approve_problem(problem_id: int):
    """Mark a problem as approved in the review queue."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "UPDATE problems SET review_status = 'approved' WHERE id = ?",
        (problem_id,),
    )
    conn.commit()
    conn.close()

def reject_problem(problem_id: int):
    """Reject a problem and shift subsequent codes down to fill the gap."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    # 1. Get the code of the problem we are about to delete
    c.execute("SELECT code FROM problems WHERE id = ?", (problem_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return
        
    deleted_code_str = row[0]
    
    try:
        deleted_code = int(deleted_code_str)
    except ValueError:
        # If code is not a number (e.g. "TEST"), just delete it and exit
        c.execute("DELETE FROM problems WHERE id = ?", (problem_id,))
        conn.commit()
        conn.close()
        return

    # 2. Delete the problem
    c.execute("DELETE FROM problems WHERE id = ?", (problem_id,))
    
    # 3. Shift down all problems with code > deleted_code
    #    e.g. if we delete 3, then 4 becomes 3, 5 becomes 4...
    c.execute("SELECT id, code FROM problems")
    all_probs = c.fetchall()
    
    for pid, pcode in all_probs:
        try:
            pcode_int = int(pcode)
            if pcode_int > deleted_code:
                new_code = str(pcode_int - 1)
                c.execute("UPDATE problems SET code = ? WHERE id = ?", (new_code, pid))
        except ValueError:
            continue

    conn.commit()
    conn.close()

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
    (opens_at IS NULL) AND is APPROVED.
    """
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "SELECT code FROM problems "
        "WHERE opens_at IS NULL AND review_status = 'approved' "
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
    editorial_url: Optional[str],
) -> int:
    """Add a new problem row with pending status."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        """INSERT INTO problems
           (code, statement, topics, difficulty, setter, source, answer,
            author_id, image_url, editorial_url, review_status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
        (code, statement, topics, difficulty, setter, source, answer, author_id, image_url, editorial_url),
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
        "SELECT id, code, difficulty, is_active, review_status FROM problems ORDER BY created_at DESC"
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

# --- NEW: STREAK LOGIC HELPERS ---

def update_streak(user_id: str):
    """Updates current/max streak based on IST date."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    # Get current data
    c.execute("SELECT current_streak, max_streak, last_solve_date FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    
    curr, maxx, last = 0, 0, None
    if row:
        curr, maxx, last = row
    
    now_ist = datetime.now(IST)
    today = now_ist.strftime("%Y-%m-%d")
    yesterday = (now_ist - timedelta(days=1)).strftime("%Y-%m-%d")
    
    if last == today:
        pass # Already solved today, streak maintains
    elif last == yesterday:
        curr += 1
    else:
        curr = 1 # Reset or start new
        
    if curr > maxx:
        maxx = curr
        
    c.execute("""
        INSERT INTO users (user_id, current_streak, max_streak, last_solve_date)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            current_streak = excluded.current_streak,
            max_streak = excluded.max_streak,
            last_solve_date = excluded.last_solve_date
    """, (user_id, curr, maxx, today))
    
    conn.commit()
    conn.close()
    return curr, maxx

def get_user_stats(user_id: str):
    """Returns (Rank, Total Points, Current Streak, Max Streak)."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    # Calculate Rank & Points dynamically
    c.execute("""
        SELECT user_id, SUM(points) as total 
        FROM submissions 
        GROUP BY user_id 
        ORDER BY total DESC
    """)
    all_scores = c.fetchall()
    
    rank = 0
    points = 0
    found = False
    
    for idx, (uid, pts) in enumerate(all_scores, 1):
        if uid == user_id:
            rank = idx
            points = pts
            found = True
            break
            
    # Get Streak data
    c.execute("SELECT current_streak, max_streak FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    curr, maxx = (row if row else (0, 0))
    
    conn.close()
    
    if not found:
        return None
    return (rank, points, curr, maxx)

# ============================================================================
# BOT SETUP
# ============================================================================

class RejectModal(ui.Modal, title="Reject Problem"):
    reason = ui.TextInput(label="Reason for Rejection", style=discord.TextStyle.paragraph, required=True)

    def __init__(self, problem_id: int, code: str, author_id: str, original_message: discord.Message):
        super().__init__()
        self.problem_id = problem_id
        self.code = code
        self.author_id = author_id
        self.original_message = original_message

    async def on_submit(self, interaction: discord.Interaction):
        # 1. Delete from DB
        reject_problem(self.problem_id)

        # 2. Notify Author
        try:
            u = await interaction.client.fetch_user(int(self.author_id))
            await u.send(f"❌ Your problem `{self.code}` was rejected.\n**Reason:** {self.reason.value}")
        except: pass

        # 3. Update the review message
        await self.original_message.edit(
            content=f"❌ `{self.code}` **REJECTED** by {interaction.user.mention}.\n**Reason:** {self.reason.value}",
            view=None, embed=None
        )

        # 4. Acknowledge modal interaction
        await interaction.response.send_message("Reason sent and problem rejected.", ephemeral=True)


class ReviewView(ui.View):
    def __init__(self, problem_id: int, code: str, author_id: str):
        super().__init__(timeout=None)
        self.problem_id = problem_id
        self.code = code
        self.author_id = author_id

    @ui.button(label="Approve", style=discord.ButtonStyle.green, custom_id="approve_btn")
    async def approve(self, interaction: discord.Interaction, button: ui.Button):
        if not any(r.name == VERIFIER_ROLE for r in interaction.user.roles):
            await interaction.response.send_message("❌ You need the Verifier role.", ephemeral=True)
            return

        approve_problem(self.problem_id)
        
        await interaction.response.edit_message(
            content=f"✅ Problem `{self.code}` **APPROVED** by {interaction.user.mention}.",
            view=None, 
            embed=None
        )
        
        try:
            author = await interaction.client.fetch_user(int(self.author_id))
            if author:
                await author.send(f"🎉 **Great news!** Your problem `{self.code}` was approved!")
        except Exception:
            pass

    @ui.button(label="Reject", style=discord.ButtonStyle.red, custom_id="reject_btn")
    async def reject(self, interaction: discord.Interaction, button: ui.Button):
        if not any(r.name == VERIFIER_ROLE for r in interaction.user.roles):
            await interaction.response.send_message("❌ You need the Verifier role.", ephemeral=True)
            return

        # Open the Modal instead of immediate reject
        await interaction.response.send_modal(
            RejectModal(self.problem_id, self.code, self.author_id, interaction.message)
        )

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
        problem_id, _code, statement, topics, difficulty, setter, 
        source, answer, opens_at, closes_at, _is_active, _created_at, 
        author_id, image_url, _editorial_url, _review_status
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

    # 1. POST EDITORIAL FOR PREVIOUS PROBLEM
    prev_active = get_active_problem()
    
    guild = None
    if AUTO_GUILD_ID:
        guild = bot.get_guild(AUTO_GUILD_ID)
    elif bot.guilds:
        guild = bot.guilds[0]

    channel = None
    if guild:
        if AUTO_CHANNEL_ID:
            ch = guild.get_channel(AUTO_CHANNEL_ID)
            if isinstance(ch, discord.TextChannel):
                channel = ch
        else:
            for chan in guild.text_channels:
                if chan.permissions_for(guild.me).send_messages:
                    channel = chan
                    break

    if channel and prev_active:
        prev_code = prev_active[1]
        prev_ans = prev_active[7]
        prev_editorial = prev_active[14]
        
        editorial_embed = discord.Embed(
            title=f"🛑 Day {prev_code} Closed", 
            color=discord.Color.red()
        )
        editorial_embed.add_field(name="Official Answer", value=f"||{prev_ans}||", inline=False)
        
        if prev_editorial:
            editorial_embed.add_field(name="Editorial / Solution", value=prev_editorial, inline=False)
        else:
            editorial_embed.set_footer(text="No editorial provided for this problem.")
            
        await channel.send(embed=editorial_embed)

    # 2. POST NEW PROBLEM
    code = get_latest_problem_code()
    if code is None:
        logger.warning(
            "daily_post_task: no (unopened) problems in DB, posting exhaustion message."
        )
        if channel:
             await channel.send(
                "😔 **No new puzzle today.**\n"
                "The problem set is currently exhausted.\n"
                "Curators, please create new problems with `/create_problem`!"
            )
        return

    if not channel:
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
            # 1. CHECK IF USER IS A VERIFIER
            # Since this is a DM, we must fetch the user from the main guild to see their roles.
            if AUTO_GUILD_ID:
                guild = bot.get_guild(AUTO_GUILD_ID)
                if guild:
                    member = guild.get_member(message.author.id)
                    # If member is found and has the Verifier role
                    if member and any(r.name == VERIFIER_ROLE for r in member.roles):
                        await message.author.send("🚫 **Verifiers cannot solve problems for points.**")
                        return
            
            # 2. NORMAL SOLVING LOGIC
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
                # --- STREAK UPDATE ---
                curr, maxx = update_streak(str(message.author.id))
                total_solves = get_user_total_solves(user_id)
                
                await message.author.send(
                    f"✅ **Correct!**\n"
                    f"Your solve for Day `{problem_code}` has been recorded.\n"
                    f"🏅 This correct submission is worth **{points}** points.\n"
                    f"📊 Your total for this problem (after penalties): "
                    f"**{problem_points_total}** points.\n\n"
                    f"🔥 **Streak:** {curr} (Max: {maxx})\n"
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
# PROBLEM CREATION (any user)
# ============================================================================

@bot.tree.command(
    name="create_problem",
    description="Create a new puzzle/problem"
)
@app_commands.describe(
    answer="Official answer string (what solvers must DM)",
    difficulty="Difficulty 1-5",
    topics="Comma-separated tags (e.g. game theory,probability)",
    statement="Full problem statement text",
    editorial="Link to solution or explanation (Compulsory)",
    image="Optional image/diagram attachment",
)
async def create_problem(
    interaction: discord.Interaction,
    answer: str,
    difficulty: app_commands.Range[int, 1, 5],
    topics: str,
    statement: str,
    editorial: str,
    image: Optional[discord.Attachment] = None,
):
    """User-facing command to create a problem."""
    user_id = str(interaction.user.id)

    await interaction.response.defer(ephemeral=True)

    # Basic validation
    if not statement.strip():
        await interaction.followup.send("❌ Problem statement cannot be empty.", ephemeral=True)
        return

    if not editorial.strip():
        await interaction.followup.send(
            "❌ Editorial is compulsory. Please provide a link or explanation.",
            ephemeral=True
        )
        return

    # Compute next available numeric code
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT code FROM problems")
    existing_codes = set()
    for (code_str,) in c.fetchall():
        try:
            existing_codes.add(int(code_str))
        except (TypeError, ValueError):
            continue
    conn.close()

    candidate = 1
    while candidate in existing_codes:
        candidate += 1
    code = str(candidate)

    # Handle image: upload to permanent asset channel if provided
    image_url = None
    if image is not None:
        image_url = await save_attachment_permanently(image, bot)

    try:
        # Insert into DB
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
            editorial_url=editorial.strip(),
        )

        # Confirmation embed to the author
        embed = discord.Embed(
            title=f"Problem {code} created",
            description=statement,
            color=discord.Color.green(),
        )
        embed.add_field(name="Difficulty", value=str(difficulty), inline=True)
        embed.add_field(name="Topics", value=topics or "N/A", inline=False)
        embed.add_field(name="Answer", value=f"||{answer}||", inline=False)
        embed.add_field(name="Editorial", value=f"||{editorial}||", inline=False)
        if image_url:
            embed.set_image(url=image_url)

        await interaction.followup.send(
            content=(
                f"✅ Problem `{code}` submitted! (ID: {problem_id}).\n"
                f"It is now **Pending Review** by verifiers."
            ),
            embed=embed,
            ephemeral=True,
        )

        # Send to verifier review channel
        if VERIFIER_CHANNEL_ID:
            guild = interaction.guild
            review_channel = guild.get_channel(VERIFIER_CHANNEL_ID) if guild else None

            if review_channel:
                review_embed = discord.Embed(
                    title="New Problem Pending Review",
                    color=discord.Color.orange(),
                )
                review_embed.add_field(name="Code", value=code, inline=True)
                review_embed.add_field(name="Setter", value=interaction.user.mention, inline=True)
                review_embed.add_field(name="Statement", value=statement[:1024], inline=False)
                review_embed.add_field(name="Answer", value=f"||{answer}||", inline=True)
                review_embed.add_field(name="Editorial", value=f"||{editorial}||", inline=False)
                if image_url:
                    review_embed.set_image(url=image_url)

                view = ReviewView(problem_id, code, user_id)
                await review_channel.send(embed=review_embed, view=view)

    except Exception as e:
        logger.error(f"Error in create_problem: {e}")
        await interaction.followup.send("⚠️ An error occurred while creating the problem.", ephemeral=True)
        
# ============================================================================
# COMMANDS
# ============================================================================

@bot.tree.command(name="post_today", description="Post today's problem (Verifier only)")
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
    name="unscore_problem", description="Remove scores for a problem (Verifier only)"
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

@bot.tree.command(name="grant_points", description="Manually add/remove points (Verifier Only)")
@app_commands.describe(user="User to modify", points="Points to add (negative to remove)", reason="Reason for adjustment")
async def grant_points(interaction: discord.Interaction, user: discord.User, points: int, reason: str):
    # Check Verifier Role
    if not any(r.name == VERIFIER_ROLE for r in interaction.user.roles):
        await interaction.response.send_message("❌ Verifier only.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    # We link to Problem ID 0 (The System Problem)
    # This avoids the need to search for a random problem ID.
    problem_id = 0
    
    try:
        c.execute(
            """INSERT INTO submissions (user_id, problem_id, answer, is_correct, points, submitted_at)
               VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (str(user.id), problem_id, f"MANUAL: {reason}", 1, points)
        )
        conn.commit()
        msg = f"✅ **{'Added' if points > 0 else 'Removed'} {abs(points)} points** to {user.mention}.\nReason: {reason}"
    except sqlite3.IntegrityError:
        # Fallback if ID 0 somehow doesn't exist (e.g. old DB without re-init)
        msg = "❌ Database Error: System Problem ID 0 not found. Restart bot to fix."
    except Exception as e:
        msg = f"❌ Error: {e}"
        
    conn.close()
    await interaction.followup.send(msg)


@bot.tree.command(name="list_problems", description="List all problems (paginated)")
@app_commands.describe(page="Page number to view (default 1)")
async def list_problems(interaction: discord.Interaction, page: int = 1):
    """List all problems with basic info."""
    await interaction.response.defer()

    PROBLEMS_PER_PAGE = 15
    if page < 1:
        page = 1

    try:
        all_problems = get_all_problems()

        if not all_problems:
            await interaction.followup.send("📭 No problems found.")
            return

        total_problems = len(all_problems)
        total_pages = (total_problems // PROBLEMS_PER_PAGE) + (1 if total_problems % PROBLEMS_PER_PAGE > 0 else 0)

        if page > total_pages:
            await interaction.followup.send(f"❌ Invalid page. There are only {total_pages} pages.")
            return

        # Get the slice of problems for the current page
        start_index = (page - 1) * PROBLEMS_PER_PAGE
        end_index = start_index + PROBLEMS_PER_PAGE
        problems_for_page = all_problems[start_index:end_index]

        embed = discord.Embed(
            title=f"All Problems (Page {page}/{total_pages})",
            color=discord.Color.blue()
        )

        for prob_id, code, difficulty, is_active, review_status in problems_for_page:
            status = "🔴 Active" if is_active else "⚪ Inactive"
            embed.add_field(
                name=f"{code}",
                value=f"Difficulty: {difficulty or 'N/A'} | {status}",
                inline=False,
            )
        
        embed.set_footer(text=f"Showing {start_index + 1}-{min(end_index, total_problems)} of {total_problems}. Use /view_problem <code> to read.")
        await interaction.followup.send(embed=embed)

    except Exception as e:
        logger.error(f"Error listing problems: {e}")
        await interaction.followup.send("⚠️ An error occurred.")

@bot.tree.command(name="view_problem", description="View problem details")
@app_commands.describe(code="Problem code to view")
async def view_problem(interaction: discord.Interaction, code: str):
    # 1. Check if user is verifier
    is_verifier = any(role.name == VERIFIER_ROLE for role in interaction.user.roles)
    
    # 2. Determine ephemeral status BEFORE deferring
    # (Safe bet: make it ephemeral if user is Verifier OR if we don't know yet)
    # Actually, let's query DB first to know if it's released.
    # We can't query DB before deferring if DB is slow, but usually it's fast enough.
    
    prob = get_problem_by_code(code)
    
    if not prob:
        await interaction.response.send_message("❌ Not found.", ephemeral=True)
        return

    # Unpack details
    (pid, _code, statement, topics, diff, setter, src, ans, op_at, cl_at, active, created, auth, img, edit, rev) = prob
    
    # 3. Access Control Logic
    is_released = (op_at is not None)
    
    if not is_released and not is_verifier:
        await interaction.response.send_message("🔒 This problem is not yet released.", ephemeral=True)
        return

    # 4. Visibility Logic
    # If Verifier looking at unreleased -> Ephemeral (Secret)
    # If Verifier looking at released -> Ephemeral (Optional, but cleaner)
    # If User looking at released -> Public (or Ephemeral if you prefer)
    
    # Your preference seemed to be "Show only for you" (Ephemeral)
    await interaction.response.defer(ephemeral=True)
    
    # 5. Build Embed
    # Color: Green if active, Red if closed, Orange if unreleased
    color = discord.Color.green() if active else (discord.Color.red() if is_released else discord.Color.orange())
    
    embed = discord.Embed(title=f"Problem {code}", description=statement, color=color)
    if img: embed.set_image(url=img)
    
    embed.add_field(name="Topics", value=topics or "N/A", inline=False)
    embed.add_field(name="Difficulty", value=str(diff) if diff else "N/A", inline=True)
    embed.add_field(name="Setter", value=setter or "N/A", inline=True)
    
    status_text = "🔴 Active" if active else ("⚪ Closed" if is_released else "⏳ Unreleased")
    embed.add_field(name="Status", value=status_text, inline=False)

    # 6. Sensitive Info (Answer/Editorial) - ONLY for Verifiers
    if is_verifier:
        embed.add_field(name="[Verifier] Answer", value=f"||{ans}||", inline=False)
        if edit: embed.add_field(name="[Verifier] Editorial", value=f"||{edit}||", inline=False)
        embed.set_footer(text="Verifier View (Visible only to you)")
    else:
        embed.set_footer(text="Visible only to you.")

    await interaction.followup.send(embed=embed)


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

# --- NEW: MY STATS COMMAND ---

@bot.tree.command(name="my_stats", description="View your rank, points, and streaks")
async def my_stats(interaction: discord.Interaction):
    """Show personal stats including rank and streak."""
    await interaction.response.defer()
    stats = get_user_stats(str(interaction.user.id))
    
    if not stats:
        await interaction.followup.send("📭 You haven't solved any problems yet.")
        return
        
    rank, points, curr, maxx = stats
    
    embed = discord.Embed(
        title=f"📊 Stats for {interaction.user.display_name}", 
        color=discord.Color.gold()
    )
    embed.add_field(name="🏆 Overall Rank", value=f"#{rank}", inline=True)
    embed.add_field(name="💰 Total Points", value=str(points), inline=True)
    embed.add_field(name="🔥 Current Streak", value=str(curr), inline=True)
    embed.add_field(name="⚡ Max Streak", value=str(maxx), inline=True)
    
    await interaction.followup.send(embed=embed)

if __name__ == "__main__":
    logger.info(">>> STARTING Problems BOT v0.5 (Verifier+Pagination) <<<")

    init_db()

    token = os.getenv("DISCORD_TOKEN")
    if not token:
        logger.error("DISCORD_TOKEN environment variable not set!")
        raise SystemExit(1)

    bot.run(token)
