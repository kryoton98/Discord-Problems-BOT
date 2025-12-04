# Problems BOT

Problems BOT is a Discord bot for daily puzzle solving.  
It posts a new puzzle every day, lets people submit answers via DMs, scores them with time‑decay and wrong‑answer penalties, and maintains leaderboards for both solvers and problem creators.

---

## Features

- **Daily puzzle at 12:00 PM IST**  
  Automatically posts one puzzle per day in your chosen channel, with a 24‑hour window for submissions.

- **Answer in direct messages**  
  Players DM the bot in the format `code answer` (for example, `2089 42`) so channels stay clean.

- **Time‑decayed scoring**  
  Each puzzle starts at 1000 points; 1 point is lost every 2 minutes after the puzzle opens.

- **Wrong‑answer penalty**  
  Every incorrect attempt costs 50 points, encouraging both speed and accuracy.

- **Leaderboards**  
  - Overall solver leaderboard by total points and number of puzzles solved.  
  - “Today” leaderboard for the currently active puzzle only.  
  - Curator leaderboard showing who creates puzzles and how well they’re rated.

- **Curator tools**  
  Curators can create puzzles, post the daily puzzle manually, reset scores for a buggy puzzle, and view creator stats.

---

## Tech Stack

- **Language:** Python 3.9+  
- **Discord library:** `discord.py` (slash commands + DMs)  
- **Database:** SQLite (`quiz_bot.db`)  
- **Config:** `.env` via `python-dotenv`

---

## Installation

### 1. Clone the repository

git clone https://github.com/kryoton98/Discord-Problems-BOT.git
cd Discord-Problems-BOT



### 2. (Optional) Create a virtual environment

python -m venv .venv
source .venv/bin/activate # Windows: .venv\Scripts\activate



### 3. Install dependencies

pip install -r requirements.txt



`requirements.txt` should contain at least:

discord.py==2.6.4
python-dotenv==1.2.1



### 4. Create a Discord application and bot

1. Open the Discord Developer Portal.  
2. Create a new application and add a **Bot** user.  
3. Enable the “Message Content Intent”.  
4. Copy the **bot token**.

### 5. Configure environment variables

Create a `.env` file next to `bot.py`:

echo "DISCORD_TOKEN=your_bot_token_here" > .env



The bot reads this variable at startup.

### 6. Configure guild and channel for daily puzzles

In `bot.py`, set these constants to your server’s IDs:

AUTO_GUILD_ID = 123456789012345678 # your server ID
AUTO_CHANNEL_ID = 234567890123456789 # channel for daily puzzles



The bot will then post the next unopened puzzle every day at **12:00 PM Asia/Kolkata (IST)**.

### 7. Run the bot

python bot.py



On first run, `quiz_bot.db` is created and all required tables are initialized.

---

## How It Works

### Daily puzzle lifecycle

1. Curators create puzzles and add them to the database.  
2. At 12:00 PM IST, the bot selects the oldest unopened puzzle and posts it to the configured channel.  
3. The puzzle is marked active for 24 hours; players can submit answers during this window.  
4. After 24 hours, the puzzle closes automatically.

### Answer format

Players DM the bot:

<puzzle_code> <answer>

example:
2089 42



The bot:

- checks that the code exists and is currently open,  
- compares the answer against the stored solution (case‑insensitive string match),  
- records a row in `submissions` with `is_correct` and `points` for every attempt.

### Scoring model

Constants in `bot.py`:

BASE_POINTS = 1000
DECAY_INTERVAL_SECONDS = 120 # 1 point lost every 2 minutes
WRONG_PENALTY = 50 # points deducted per wrong answer



- **Correct answer:**  
  \( \text{points} = \max(0,\; \text{BASE\_POINTS} - \lfloor t / \text{DECAY\_INTERVAL\_SECONDS} \rfloor) \),  
  where \( t \) is the number of seconds since the puzzle opened.

- **Wrong answer:**  
  \( \text{points} = -\text{WRONG\_PENALTY} \).

Every attempt (correct or wrong) is stored; total score for a user is the sum of all their `points`.

Leaderboards:

- **Overall:** sum of points across all puzzles, plus count of distinct puzzles with a correct submission.  
- **Today:** sum of points on the active puzzle, including penalties, but only shows users who have at least one correct submission on that puzzle.

---

## Commands

### Solver commands

- `/leaderboard overall`  
  Show global solver leaderboard by total points and number of puzzles solved.

- `/leaderboard today`  
  Show today’s leaderboard for the currently active puzzle.

- `/rate_problem code:<code> rating:1-5`  
  Rate a puzzle you have solved correctly (1–5 stars).
### Curator commands

Curator‑only commands require the `Curator` role in your Discord server.

- `/create_problem`  
  Create a new puzzle with statement, topics/tags, difficulty (1–5), answer, and optional image.  
  Limited to **1 puzzle per user per 24 hours**.

- `/post_today code:<code>`  
  Manually post a puzzle into the current channel and mark it active for 24 hours.

- `/unscore_problem code:<code> user:[optional]`  
  Clear scores and correct flags for all submissions on that puzzle, or just for a specific user.

- `/list_problems`  
  List all puzzles with their code, difficulty, and whether they are active.

- `/curator_leaderboard`  
  Show puzzle creators ordered by how many puzzles they have in the system and their average rating.

---

## Project Structure

- `bot.py` – main Discord bot; database setup, DM logic, scoring, and slash commands.
- `quiz_bot.db` – SQLite database created at runtime.  
- `requirements.txt` – Python dependencies.  
- `install-guide.pdf` – optional detailed installation guide, linked from the front‑end site.  
- `app/` – marketing / info site (Next.js, built via v0) with sections for hero, features, setup, FAQ, and timeline.

---

## Contributing

Pull requests and issue reports are welcome:

1. Fork the repository.  
2. Create a feature branch.  
3. Make your changes (with clear commit messages).  
4. Open a PR describing what you changed and why.

---

## License

This project is released under the MIT License. See `LICENSE` for details.

---

Made by **kryoton98**.  
Built for puzzle‑loving Discord communities.
