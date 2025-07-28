import os
import json
import httpx
import asyncio
import logging
import random
from dataclasses import dataclass, asdict
from typing import Dict, Optional, Union

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ChatAction
from telegram.helpers import escape_markdown
from telegram.error import BadRequest

# --- Configuration ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = "YOUR_TELEGRAM_TOKEN_HERE"
GROQ_API_KEY = "YOUR_GROQ_API_KEY_HERE"

if not TELEGRAM_TOKEN or not GROQ_API_KEY:
    raise ValueError(
        "CRITICAL: Please set the TELEGRAM_TOKEN and GROQ_API_KEY variables."
    )

# Groq API configuration
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

GAME_SYSTEM_PROMPT = (
    "You are the 'Dungeon Master' for 'Castle of Bugs', a text-based debugging adventure game in Persian. Your role is to create challenging, atmospheric, and clever rooms for a programmer to solve. "
    "Your entire response MUST be a single, raw, valid JSON object and absolutely nothing else. Do not include explanations or markdown."
    "\n\n"
    "## JSON Object Structure:"
    "The JSON object must strictly contain these three keys:"
    "1. 'description' (string): A short, atmospheric narrative in Persian. The story MUST subtly hint at the specific type of bug. Examples:"
    "   - For an 'IndexError', the story could describe reaching for a book on a shelf that is just out of reach."
    "   - For a 'KeyError', it could mention trying to open a lock with a key that doesn't exist for that chest."
    "   - For a 'TypeError', it could describe trying to mix two incompatible magic potions."
    "2. 'buggy_snippet' (string): A Python code snippet of 4-10 lines. It must contain a single bug based on the difficulty tier. Use thematic variable names (e.g., `ghosts`, `spell_power`, `find_key`)."
    "3. 'correct_snippet' (string): The perfectly fixed version of the code."
    "\n\n"
    "## Bug Difficulty Tiers (Based on User's Room Number):"
    "- **Rooms 1-2 (Beginner Tier):** Focus on simple, obvious errors."
    "   - Bug Types: `SyntaxError` (missing ':', unbalanced '()', mismatched quotes), basic `NameError` (a clear typo in a variable name)."
    "- **Rooms 3-4 (Intermediate Tier):** Focus on runtime errors and common mistakes."
    "   - Bug Types: `TypeError` (e.g., `10 + '5'`), `IndexError` (list index out of range), `KeyError` (dictionary key not found), Assignment vs. Comparison (using `=` in an `if` statement instead of `==`)."
    "- **Rooms 5+ (Advanced Tier):** Focus on subtle logical errors that don't crash the program but produce wrong results."
    "   - Bug Types: Off-by-one errors in loops (`range(len(items))`), incorrect boolean logic (`>` instead of `>=`), infinite loops that should terminate, function returning the wrong value."
    "\n\n"
    "Crucially, avoid repeating the exact same bug type in consecutive rooms. Vary the challenges."
)

# Messages shown when a room is completed
ROOM_COMPLETE_MESSAGES = [
    "✅ طلسم این تالار شکسته شد! یک راهروی مخفی به سوی تالار شماره {room_number} پدیدار می‌شود...",
    "🎉 طلسم شکست! حالا دریچه‌ای به تالار شماره {room_number} گشوده شده است...",
    "🔓 دروازهٔ جدیدی باز شد و راه به تالار شماره {room_number} نمایان گشت...",
    "✨ با موفقیت طلسم شکسته شد؛ راهروی تاریک تا تالار شماره {room_number} روشن گردید...",
    "🏹 طلسم مغلوب شد و نشانه‌ای به تالار شماره {room_number} ظاهر شد...",
    "🗝️ قفل جادویی باز شد؛ در ورودی تالار شماره {room_number} نمایان شد...",
    "🔥 شعلهٔ امید روشن شد و مسیری به تالار شماره {room_number} گشوده گشت...",
    "🌌 در پس پردهٔ سایه‌ها، درِ تالار شماره {room_number} هویدا شد...",
    "💫 طلسم فرو ریخت و راه مخفی به تالار شماره {room_number} آشکار شد...",
    "🛡️ سپر افسون‌شکسته کنار رفت و راه به تالار شماره {room_number} نمایان شد..."
]

# --- Data Structures and Game State ---
USER_SESSIONS_FILE = "user_sessions.json"
user_processing_status: Dict[int, bool] = {}
user_sessions: Dict[int, 'RoomSession'] = {}

@dataclass
class RoomSession:
    """Stores the state of a user's game session."""
    user_id: int
    room_number: int = 1
    description: str = ""
    buggy_snippet: str = ""
    correct_snippet: str = ""
    attempts: int = 0
    is_complete: bool = False

# --- Persistence Functions ---
def save_sessions_to_file():
    """Saves the current user sessions to a JSON file."""
    try:
        sessions_to_save = {uid: asdict(session) for uid, session in user_sessions.items()}
        with open(USER_SESSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(sessions_to_save, f, ensure_ascii=False, indent=4)
        logger.info("User sessions saved to file.")
    except Exception as e:
        logger.error(f"Failed to save sessions to file: {e}")

def load_sessions_from_file():
    """Loads user sessions from a JSON file at startup."""
    global user_sessions
    try:
        if os.path.exists(USER_SESSIONS_FILE):
            with open(USER_SESSIONS_FILE, "r", encoding="utf-8") as f:
                sessions_from_file = json.load(f)
                for uid, session_data in sessions_from_file.items():
                    user_sessions[int(uid)] = RoomSession(**session_data)
                logger.info(f"Loaded {len(user_sessions)} user sessions from file.")
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.error(f"Could not load session file ({e}). Starting with empty sessions.")
        user_sessions = {}

# --- Groq API Interaction ---
async def call_groq_api(
    system_prompt: str, user_prompt: str, expect_json: bool = True
) -> Optional[Union[dict, str]]:
    """A highly robust and safe function to call the Groq API."""
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        "temperature": 0.7,
        "max_tokens": 1024,
    }

    if expect_json:
        payload["response_format"] = {"type": "json_object"}

    async with httpx.AsyncClient(timeout=40.0) as client:
        try:
            response = await client.post(GROQ_API_URL, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()

            if not data.get("choices") or not data["choices"][0].get("message"):
                logger.error(f"Groq API returned an unexpected structure: {data}")
                return None

            message_content = data["choices"][0]["message"]["content"]

            if expect_json:
                try:
                    return json.loads(message_content)
                except json.JSONDecodeError:
                    logger.error(f"Failed to parse JSON string despite JSON mode: {message_content}")
                    return None
            else:
                return message_content

        except httpx.HTTPStatusError as e:
            logger.error(f"Groq API HTTP error: {e.response.status_code} - {e.response.text}")
        except httpx.RequestError as e:
            logger.error(f"Groq API request error: {e}")
        except Exception as e:
            logger.error(f"An unexpected error occurred when calling Groq API: {e}")

        return None

# --- Bot Helper Functions ---
def get_main_keyboard() -> ReplyKeyboardMarkup:
    """Creates the main interactive keyboard."""
    keyboard = [
        [KeyboardButton("🎮 ورود به قلعه"), KeyboardButton("💡 دریافت راهنمایی")],
        [KeyboardButton("🚪 ترک بازی"), KeyboardButton("📊 پیشرفت من")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)

def normalize_code(code: str) -> str:
    """Normalizes code for a more reliable comparison by removing all whitespace."""
    return "".join(code.split())

def format_room_message(description: str, snippet: str, room_number: int) -> str:
    """Formats the message for a new room, escaping the description for MarkdownV2."""

    safe_description = escape_markdown(description, version=2)

    return (
        f"🏰 *تالار شماره {room_number}*\n\n"
        f"{safe_description}\n\n"
        f"*کد نفرین‌شده:*\n```python\n{snippet}\n```\n\n"
        "برای شکستن طلسم، کد اصلاح شده را ارسال کنید\."
    )

async def cleanup_session(user_id: int):
    """Cleans up a user's session and resets their processing status."""
    user_sessions.pop(user_id, None)
    save_sessions_to_file()
    logger.info(f"Session for user {user_id} has been cleaned up.")

# --- Bot Command and Message Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends the welcome message."""
    welcome_text = (
        "درهای سنگین *قلعه باگ‌ها* به روی شما گشوده می‌شود 🏰\n\n"
        "زمزمه کدهای شبح‌زده در راهروها می‌پیچد\. تنها راه فرار، یافتن و ترمیم اشکالات جادویی است که در هر تالار پنهان شده\.\n\n"
        "برای ورود به اولین تالار، دکمه 'ورود به قلعه' را لمس کن\."
    )
    await update.message.reply_text(welcome_text, parse_mode="MarkdownV2", reply_markup=get_main_keyboard())

async def god_mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A hidden command to reveal the correct answer."""
    user_id = update.effective_user.id
    session = user_sessions.get(user_id)

    if session and not session.is_complete:
        answer = session.correct_snippet
        message = (
            "🤫 *God Mode Activated\\!* 🤫\n\n"
            "The correct solution for this room is:\n\n"
            f"```python\n{answer}\n```"
        )
        await update.message.reply_text(message, parse_mode="MarkdownV2")
    else:
        await update.message.reply_text("You must be in the castle to use this command\\. Press 'Enter Castle' to start\\.")

async def enter_castle_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The logic for starting a new game."""
    user_id = update.effective_user.id

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    system_prompt = GAME_SYSTEM_PROMPT
    user_prompt = "Generate the first room (Room 1) for the debugging adventure game."

    response = await call_groq_api(system_prompt, user_prompt, expect_json=True)

    if isinstance(response, dict) and all(k in response for k in ("description", "buggy_snippet", "correct_snippet")):
        session = RoomSession(user_id=user_id, **response)
        user_sessions[user_id] = session
        save_sessions_to_file()
        msg = format_room_message(session.description, session.buggy_snippet, session.room_number)
        await update.message.reply_text(msg, parse_mode="MarkdownV2", reply_markup=get_main_keyboard())
    else:
        logger.error(f"Failed to get a valid room structure from Groq. Response: {response}")
        await update.message.reply_text("دروازه قلعه توسط یک باگ مرموز قفل شده است! لطفاً کمی بعد دوباره تلاش کنید.")

async def hint_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The logic for providing a hint."""
    user_id = update.effective_user.id
    session = user_sessions.get(user_id)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    system_prompt = (
        "You are a wise, cryptic ghost from the 'Castle of Bugs'. Your role is to give a single, clever hint in Persian to a struggling programmer. "
        "Your hint must NOT reveal the direct answer or the correct code. "
        "Instead of stating the problem, ask a leading question that guides the user to the solution. "
        "Focus on the programming concept behind the error."
        "\n\n"
        "Example: If the bug is a missing colon, a BAD hint is 'You forgot a colon'. A GOOD hint is 'In Python, what special character is required at the end of a function or loop definition line?'"
        "\n\n"
        "Your response must be ONLY the hint text and nothing else."
    )
    user_prompt = (
        f"Provide a short hint in Persian for this buggy code:\n"
        f"Buggy Code: ```python\n{session.buggy_snippet}\n```\n"
        f"Correct Code: ```python\n{session.correct_snippet}\n```"
    )

    hint_text = await call_groq_api(system_prompt, user_prompt, expect_json=False)

    if hint_text:
        safe_hint_text = escape_markdown(hint_text, version=2)
        await update.message.reply_text(f"💡 *نجوای راهنما:* {safe_hint_text}", parse_mode="MarkdownV2")
    else:
        await update.message.reply_text("ارواح قلعه در سکوت فرو رفته‌اند و پاسخی نمی‌دهند. شاید بعداً بتوان نجواهایشان را شنید...")

async def code_submission_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The logic for handling a user's code submission."""
    user_id = update.effective_user.id
    session = user_sessions.get(user_id)

    user_code = normalize_code(update.message.text)
    correct_code = normalize_code(session.correct_snippet)

    if user_code == correct_code:
        session.room_number += 1
        session.attempts = 0

        if session.room_number > 5: # تعداد کل اتاق‌ها
            await update.message.reply_text(
                "🎉 *طلسم‌ها شکسته شد و درهای قلعه باز شدند!* شما باگ نهایی را مغلوب کردید و از *قلعه باگ‌ها* گریختید. نام شما در میان دیباگرهای افسانه‌ای ثبت خواهد شد.\n\n"
                "برای یک چالش جدید، دوباره 'ورود به قلعه' را انتخاب کنید.",
                parse_mode="MarkdownV2"
            )
            await cleanup_session(user_id)
            return

        room_msg = random.choice(ROOM_COMPLETE_MESSAGES).format(room_number=session.room_number)
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        await update.message.reply_text(room_msg)

        system_prompt = GAME_SYSTEM_PROMPT
        user_prompt = f"Generate room number {session.room_number} of the debugging adventure game."
        response = await call_groq_api(system_prompt, user_prompt, expect_json=True)

        if isinstance(response, dict) and all(k in response for k in ("description", "buggy_snippet", "correct_snippet")):
            session.description = response["description"]
            session.buggy_snippet = response["buggy_snippet"]
            session.correct_snippet = response["correct_snippet"]
            save_sessions_to_file()
            msg = format_room_message(session.description, session.buggy_snippet, session.room_number)
            await update.message.reply_text(msg, parse_mode="MarkdownV2")
        else:
            logger.error(f"Invalid structure from Groq for room {session.room_number}: {response}")
            await update.message.reply_text("💀 قلعه در حال فروریختن است... بازی به پایان رسید.")
            await cleanup_session(user_id)
    else:
        session.attempts += 1
        save_sessions_to_file()
        await update.message.reply_text("❌ کد شما بر دیوارهای سنگی قلعه اثری نداشت. باگ همچنان پابرجاست. دوباره تلاش کنید یا از ارواح راهنما کمک بگیرید.")

# --- Main Message Router ---
async def message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Routes all incoming messages to the correct handler task."""
    user_id = update.effective_user.id
    text = update.message.text

    if user_processing_status.get(user_id):
        await update.message.reply_text("کمی صبر کنید... در حال تمرکز بر روی طلسم فعلی هستم...")
        return

    session = user_sessions.get(user_id)
    task_to_run = None

    if text == "🎮 ورود به قلعه":
        if session and not session.is_complete:
            # FIX: Instead of a generic message, resend the current puzzle.
            await update.message.reply_text("شما به تالارهای قلعه بازگشتید. آخرین معما در انتظار شماست:")
            msg = format_room_message(session.description, session.buggy_snippet, session.room_number)
            await update.message.reply_text(msg, parse_mode="MarkdownV2", reply_markup=get_main_keyboard())
        else:
            # If there's no session, start a new game.
            task_to_run = enter_castle_task(update, context)
    elif text == "💡 دریافت راهنمایی":
        if not session or session.is_complete:
            await update.message.reply_text("ارواح فقط در داخل دیوارهای قلعه نجوا می‌کنند. برای شنیدن صدایشان، ابتدا وارد قلعه شوید.")
        else:
            task_to_run = hint_task(update, context)
    elif text == "🚪 ترک بازی":
        if session:
            await cleanup_session(user_id)
            await update.message.reply_text("شما با استفاده از یک اسکرول تلپورت، از قلعه خارج شدید. سایه‌های قلعه منتظر بازگشت شما برای یک چالش دیگر هستند!")
        else:
            await update.message.reply_text("شما خارج از دیوارهای قلعه باگ‌ها هستید.")
    elif text == "📊 پیشرفت من":
        if not session or session.is_complete:
            await update.message.reply_text("شما خارج از دیوارهای قلعه باگ‌ها هستید.")
        else:
            progress_msg = (
                f"📜 *طومار پیشرفت شما:*\n\n"
                f"📍 تالار شماره: {session.room_number} از 5\n"
                f"❌ تلاش‌های ناموفق در این تالار: {session.attempts}"
            )
            await update.message.reply_text(progress_msg, parse_mode="MarkdownV2")
    else:
        if session and not session.is_complete:
            task_to_run = code_submission_task(update, context)
        else:
            await update.message.reply_text("پژواک کلمات شما در راهروهای خالی قلعه گم شد... از دکمه‌های جادویی پایین برای تعامل استفاده کنید.")

    if task_to_run:
        user_processing_status[user_id] = True
        try:
            await task_to_run
        finally:
            user_processing_status[user_id] = False

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log Errors caused by Updates and send a user-friendly message."""
    logger.error("Exception while handling an update:", exc_info=context.error)

    if update and hasattr(update, 'effective_message'):
        try:
            await update.effective_message.reply_text("یک خطای پیش‌بینی نشده در جادوی قلعه رخ داد. لطفاً کمی بعد دوباره تلاش کنید.")
        except Exception as e:
            logger.error(f"Failed to send error message to user: {e}")

# --- Main Bot Execution ---
def main() -> None:
    """Starts the bot."""
    load_sessions_from_file()

    logger.info("Building application...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("godmode", god_mode_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_router))
    app.add_error_handler(error_handler)

    logger.info("Starting Debugging Adventure Bot...")
    app.run_polling()

if __name__ == "__main__":
    main()