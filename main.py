#!/usr/bin/env python3
"""
F1 Paddock Quiz — Instagram Automation Pipeline

Generates hardcore F1 trivia via Gemini, renders a branded 1080x1350 quiz image,
hosts it publicly, publishes to Instagram, and schedules an answer comment 3 hours later.
Duplicate questions are blocked via SQLite (30-day rolling window).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types
from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
GEMINI_MAX_RETRIES = int(os.getenv("GEMINI_MAX_RETRIES", "4"))
GEMINI_RETRY_BASE_SECONDS = int(os.getenv("GEMINI_RETRY_BASE_SECONDS", "5"))
MODELS_TO_TRY = [
    "gemini-2.0-flash",
    "gemini-2.5-flash",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
]
GEMINI_FALLBACK_MODELS = [
    model.strip()
    for model in os.getenv("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash").split(",")
    if model.strip()
]
INSTAGRAM_ACCOUNT_ID = (os.getenv("INSTAGRAM_ACCOUNT_ID") or "").strip()
INSTAGRAM_ACCESS_TOKEN = (os.getenv("INSTAGRAM_ACCESS_TOKEN") or "").strip().strip('"').strip("'")
FACEBOOK_PAGE_ID = os.getenv("FACEBOOK_PAGE_ID", "")
BRAND_NAME = os.getenv("BRAND_NAME", "f1paddockquiz")
WATERMARK_TEXT = os.getenv("WATERMARK_TEXT", f"@{BRAND_NAME}")
BRAND_HANDLE = f"@{BRAND_NAME}"
IMGUR_CLIENT_ID = (os.getenv("IMGUR_CLIENT_ID") or "").strip()
GITHUB_HOST_PATH = os.getenv("GITHUB_HOST_PATH", "public/latest_post.jpg")

GRAPH_API_VERSION = "v26.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
INSTAGRAM_LOGIN_API_VERSION = os.getenv("INSTAGRAM_LOGIN_API_VERSION", "v23.0")
INSTAGRAM_LOGIN_API_BASE = f"https://graph.instagram.com/{INSTAGRAM_LOGIN_API_VERSION}"

CANVAS_WIDTH = 1080
CANVAS_HEIGHT = 1350
CARD_WIDTH = 780
QUIZ_FONT_SIZE = 230
QUIZ_STROKE_WIDTH = 12
QUIZ_SHIFT_LEFT = 120
F1_BADGE_SHIFT_RIGHT = 190
F1_BADGE_OVERLAP = 72
COLOR_BLACK = (17, 17, 17)
COLOR_BG = (0, 0, 0)
COLOR_WHITE = (255, 255, 255)
COLOR_GRAY_TEXT = (17, 17, 17)
COLOR_OPTION_BG = (240, 240, 240)
COLOR_LETTER_BG = (17, 17, 17)
COLOR_LETTER_TEXT = (255, 255, 255)
COLOR_SHADOW = (0, 0, 0, 60)

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_IMAGE = BASE_DIR / "final_post.jpg"
DATABASE_PATH = BASE_DIR / "database.db"
PENDING_COMMENTS_FILE = BASE_DIR / "pending_comments.json"
FONT_PATH = BASE_DIR / "font.ttf"
FONT_SANS_PATH = BASE_DIR / "fonts" / "Inter.ttf"
FONT_DISPLAY_PATHS = (
    BASE_DIR / "font-display.ttf",
    BASE_DIR / "FodaDisplay-Regular.otf",
    BASE_DIR / "Foda Display Regular.otf",
    BASE_DIR / "FodaDisplay-Regular.ttf",
    BASE_DIR / "foda-display.ttf",
)

POLL_INTERVAL_SECONDS = 5
POLL_MAX_WAIT_SECONDS = 30
DUPLICATE_WINDOW_DAYS = int(os.getenv("DUPLICATE_WINDOW_DAYS", "30"))
DUPLICATE_CONTENT_RETRIES = int(os.getenv("DUPLICATE_CONTENT_RETRIES", "5"))
COMMENT_DELAY_HOURS = int(os.getenv("COMMENT_DELAY_HOURS", "3"))
WAIT_FOR_COMMENT = os.getenv("WAIT_FOR_COMMENT", "").lower() in ("1", "true", "yes")

ANSWER_COMMENT_FOOTER = (
    "All facts are drawn from publicly available Formula 1 records and are shared "
    "for educational and entertainment purposes only.\n\n"
    "If you believe any detail is inaccurate, comment below and we will verify "
    "against official sources and correct if needed."
)

CONTENT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": "Hardcore F1 trivia question — on-track sport only.",
        },
        "options": {
            "type": "object",
            "properties": {
                "A": {"type": "string"},
                "B": {"type": "string"},
                "C": {"type": "string"},
                "D": {"type": "string"},
            },
            "required": ["A", "B", "C", "D"],
        },
        "correct_option": {
            "type": "string",
            "enum": ["A", "B", "C", "D"],
        },
        "explanation": {
            "type": "string",
            "description": "Detailed factual explanation of the correct answer.",
        },
        "caption": {
            "type": "string",
            "description": "Instagram caption with hook, CTA, and hashtags.",
        },
    },
    "required": ["question", "options", "correct_option", "explanation", "caption"],
}

GEMINI_SYSTEM_PROMPT = f"""You are the content engine for {BRAND_HANDLE} — an Instagram account that posts
REALLY HARD / HARDCORE Formula 1 trivia for true racing fans and F1 gurus.

Generate ONE unique quiz question that is:
- Genuinely difficult — not surface-level fan trivia
- Factually accurate and verifiable from official F1 history and statistics
- Engaging and save-worthy for motorsport enthusiasts

ALLOWED TOPICS (on-track sport only):
- F1 history, race statistics, Grand Prix wins and records
- Circuit layouts, lap records, track-specific facts
- Pit stop rules, sporting regulations, technical regulations
- Engine eras (V10, V8, hybrid), aero rules, tyre compounds
- Championship standings, points ties, qualifying records, team statistics

STRICT PROHIBITION — NEVER ask about:
- Drivers' personal lives, relationships, girlfriends, fashion, childhood
- Off-track celebrity drama, social media, or lifestyle content
- Rumors, gossip, or speculative off-track narratives

Rules:
- question MUST be a single clear trivia question (max ~35 words)
- options MUST have exactly 4 choices labeled A, B, C, D — all plausible but one correct
- correct_option MUST be exactly one of: A, B, C, or D
- explanation MUST be 2-4 sentences with specific facts (years, races, numbers)
- explanation MUST be split into 2-3 short mini-paragraphs separated by blank lines (\\n\\n) — each block 1-2 sentences max for mobile readability
- caption MUST include a hook, invite comments (A/B/C/D), note answer in 3 hours, and hashtags
- caption MUST be plain text — no markdown (**bold**, etc.)
- NEVER repeat any question listed in the "Recently published — DO NOT REUSE" section
- Output ONLY valid JSON matching the schema — no markdown, no commentary
"""

GEMINI_SYSTEM_PROMPT_BASE = GEMINI_SYSTEM_PROMPT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(BRAND_NAME)


def _strip_markdown(text: str) -> str:
    cleaned = text
    cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"__(.+?)__", r"\1", cleaned)
    cleaned = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", cleaned)
    cleaned = cleaned.replace("**", "").replace("__", "").replace("`", "")
    return re.sub(r"  +", " ", cleaned).strip()


def _normalize_question(text: str) -> str:
    lowered = text.lower().strip()
    lowered = re.sub(r"[^\w\s]", "", lowered)
    return re.sub(r"\s+", " ", lowered)


def _format_explanation_paragraphs(text: str) -> str:
    """Split explanation into short mini-paragraphs for Instagram comment readability."""
    text = text.strip()
    if not text:
        return text

    if re.search(r"\n\s*\n", text):
        paragraphs = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
        return "\n\n".join(paragraphs)

    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    if len(sentences) <= 1:
        return text

    paragraphs: list[str] = []
    for index in range(0, len(sentences), 2):
        chunk = " ".join(sentences[index : index + 2]).strip()
        if chunk:
            paragraphs.append(chunk)
    return "\n\n".join(paragraphs)


def _question_hash(text: str) -> str:
    normalized = _normalize_question(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass
class QuizContent:
    question: str
    options: dict[str, str]
    correct_option: str
    explanation: str
    caption: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QuizContent:
        options = {key: _strip_markdown(str(data["options"][key]).strip()) for key in ("A", "B", "C", "D")}
        correct = data["correct_option"].strip().upper()
        if correct not in options:
            raise ValueError(f"correct_option must be A, B, C, or D — got '{correct}'")

        question = _strip_markdown(data["question"].strip())
        if len(question.split()) < 5:
            raise ValueError(f"Question too short: {question}")

        return cls(
            question=question,
            options=options,
            correct_option=correct,
            explanation=_format_explanation_paragraphs(_strip_markdown(data["explanation"].strip())),
            caption=_strip_markdown(data["caption"].strip()),
        )

    def full_caption(self) -> str:
        return self.caption

    def answer_comment(self) -> str:
        correct_text = self.options[self.correct_option]
        return (
            f"✅ Correct Answer: {self.correct_option} — {correct_text}\n\n"
            f"📖 Explanation:\n\n{self.explanation}"
        )


# ---------------------------------------------------------------------------
# SQLite — duplicate prevention & comment scheduling
# ---------------------------------------------------------------------------


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question_hash TEXT NOT NULL,
                question_text TEXT NOT NULL,
                options_json TEXT NOT NULL,
                correct_option TEXT NOT NULL,
                explanation TEXT NOT NULL,
                caption TEXT NOT NULL,
                created_at TEXT NOT NULL,
                published_at TEXT,
                media_id TEXT,
                comment_scheduled_at TEXT,
                comment_posted_at TEXT,
                comment_text TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_question_hash_created "
            "ON questions (question_hash, created_at)"
        )
        conn.commit()


def is_duplicate_question(question_text: str) -> bool:
    """Return True if this question (or hash) exists within the rolling window."""
    init_db()
    question_hash = _question_hash(question_text)
    cutoff = datetime.now(timezone.utc) - timedelta(days=DUPLICATE_WINDOW_DAYS)
    cutoff_str = cutoff.isoformat()

    with _get_db() as conn:
        row = conn.execute(
            """
            SELECT id FROM questions
            WHERE question_hash = ? AND created_at >= ?
            LIMIT 1
            """,
            (question_hash, cutoff_str),
        ).fetchone()
        if row:
            return True

        normalized = _normalize_question(question_text)
        rows = conn.execute(
            "SELECT question_text FROM questions WHERE created_at >= ?",
            (cutoff_str,),
        ).fetchall()
        for existing in rows:
            if _normalize_question(existing["question_text"]) == normalized:
                return True
    return False


def record_question(
    content: QuizContent,
    *,
    published_at: str | None = None,
    media_id: str | None = None,
    comment_scheduled_at: str | None = None,
    comment_text: str | None = None,
) -> int:
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    with _get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO questions (
                question_hash, question_text, options_json, correct_option,
                explanation, caption, created_at, published_at, media_id,
                comment_scheduled_at, comment_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _question_hash(content.question),
                content.question,
                json.dumps(content.options, ensure_ascii=False),
                content.correct_option,
                content.explanation,
                content.caption,
                now,
                published_at,
                media_id,
                comment_scheduled_at,
                comment_text,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def mark_published(record_id: int, media_id: str, comment_scheduled_at: str, comment_text: str) -> None:
    with _get_db() as conn:
        conn.execute(
            """
            UPDATE questions
            SET published_at = ?, media_id = ?, comment_scheduled_at = ?, comment_text = ?
            WHERE id = ?
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                media_id,
                comment_scheduled_at,
                comment_text,
                record_id,
            ),
        )
        conn.commit()


def get_pending_comments() -> list[sqlite3.Row]:
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    with _get_db() as conn:
        return conn.execute(
            """
            SELECT * FROM questions
            WHERE media_id IS NOT NULL
              AND comment_scheduled_at IS NOT NULL
              AND comment_posted_at IS NULL
              AND comment_scheduled_at <= ?
            ORDER BY comment_scheduled_at ASC
            """,
            (now,),
        ).fetchall()


def mark_comment_posted(record_id: int) -> None:
    with _get_db() as conn:
        conn.execute(
            "UPDATE questions SET comment_posted_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), record_id),
        )
        conn.commit()


def load_recent_questions(limit: int = 30) -> list[str]:
    init_db()
    cutoff = datetime.now(timezone.utc) - timedelta(days=DUPLICATE_WINDOW_DAYS)
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT question_text FROM questions WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
            (cutoff.isoformat(), limit),
        ).fetchall()
    return [row["question_text"] for row in rows]


def show_question_history() -> None:
    init_db()
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT question_text, published_at, comment_posted_at FROM questions ORDER BY created_at DESC LIMIT 30"
        ).fetchall()
    if not rows:
        print(f"No entries in {DATABASE_PATH.name} yet.")
        return
    print(f"Last {len(rows)} questions in database:")
    for index, row in enumerate(rows, start=1):
        status = "commented" if row["comment_posted_at"] else "pending/no comment"
        print(f"  {index:2}. {row['question_text'][:70]}... — {row['published_at'] or 'not published'} ({status})")


# ---------------------------------------------------------------------------
# JSON-based Pending Comments (for reliable cross-workflow state)
# ---------------------------------------------------------------------------


def load_pending_comments_json() -> list[dict[str, Any]]:
    """Load pending comments from JSON file (git-committed for cross-workflow reliability)."""
    if not PENDING_COMMENTS_FILE.exists():
        return []
    try:
        with open(PENDING_COMMENTS_FILE, encoding="utf-8") as f:
            data = json.load(f)
            return data.get("pending", [])
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not load pending comments JSON: %s", exc)
        return []


def save_pending_comments_json(pending: list[dict[str, Any]]) -> None:
    """Save pending comments to JSON file."""
    with open(PENDING_COMMENTS_FILE, "w", encoding="utf-8") as f:
        json.dump({"pending": pending}, f, indent=2, ensure_ascii=False)
    logger.info("Saved %d pending comment(s) to %s", len(pending), PENDING_COMMENTS_FILE.name)


def add_pending_comment_json(
    media_id: str,
    comment_text: str,
    scheduled_at: str,
    question_text: str,
) -> None:
    """Add a new pending comment to the JSON file."""
    pending = load_pending_comments_json()
    pending.append({
        "media_id": media_id,
        "comment_text": comment_text,
        "scheduled_at": scheduled_at,
        "question_text": question_text,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    save_pending_comments_json(pending)


def get_due_pending_comments_json() -> list[dict[str, Any]]:
    """Return pending comments whose scheduled time has passed."""
    pending = load_pending_comments_json()
    now = datetime.now(timezone.utc).isoformat()
    return [p for p in pending if p.get("scheduled_at", "") <= now]


def remove_pending_comment_json(media_id: str) -> None:
    """Remove a pending comment after it has been posted."""
    pending = load_pending_comments_json()
    updated = [p for p in pending if p.get("media_id") != media_id]
    save_pending_comments_json(updated)
    logger.info("Removed pending comment for media_id %s", media_id)


# ---------------------------------------------------------------------------
# Stage 1 — Content Engine (Gemini)
# ---------------------------------------------------------------------------


def _uses_instagram_login_api() -> bool:
    """Instagram Login tokens start with IG…; Facebook/Page tokens start with EAA…"""
    token = INSTAGRAM_ACCESS_TOKEN or ""
    if token.startswith("EAA"):
        return False
    return token.startswith("IG")


def _instagram_api_base() -> str:
    return INSTAGRAM_LOGIN_API_BASE if _uses_instagram_login_api() else GRAPH_API_BASE


def _format_graph_api_error(error: dict[str, Any], api_label: str) -> str:
    message = error.get("message", "Unknown error")
    code = error.get("code")
    if code == 200 or "access blocked" in message.lower():
        return (
            f"{api_label} error: {message}\n"
            "Meta blocked Instagram API access for this token or app. Try:\n"
            "  1. https://developers.facebook.com/tools/debug/accesstoken/ — check token expiry\n"
            "  2. Meta Developer Console → your app → generate a new Instagram access token\n"
            "  3. Required scopes: instagram_business_basic, instagram_business_content_publish\n"
            "  4. App must be Live with Advanced Access (Development mode only works for testers)\n"
            f"  5. Meta Business Suite → Instagram accounts → reconnect {BRAND_HANDLE} to the app"
        )
    return f"{api_label} error: {message}"


def _resolve_instagram_account_id() -> str:
    if INSTAGRAM_ACCOUNT_ID and INSTAGRAM_ACCOUNT_ID.isdigit():
        logger.info("Using Instagram account id from .env: %s", INSTAGRAM_ACCOUNT_ID)
        return INSTAGRAM_ACCOUNT_ID

    if not _uses_instagram_login_api():
        raise EnvironmentError(
            "INSTAGRAM_ACCOUNT_ID is required for Facebook Login tokens. "
            "Run: python main.py --lookup-ig-id"
        )

    logger.info("Resolving Instagram account via Instagram Login API (/me)...")
    data = _graph_request("GET", "me", params={"fields": "id,user_id,username"})
    account_id = data.get("user_id") or data.get("id")
    if not account_id:
        raise RuntimeError("Could not resolve Instagram account id from /me")
    logger.info(
        "Instagram account resolved — @%s (publish id: %s)",
        data.get("username", "unknown"),
        account_id,
    )
    return str(account_id)


PLACEHOLDER_MARKERS = ("your_", "changeme", "replace_me", "xxx", "example")


def _looks_like_placeholder(value: str | None) -> bool:
    if not value:
        return True
    lowered = value.strip().lower()
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def validate_instagram_env() -> None:
    """Validate credentials required for Instagram publish/comment only."""
    required = {
        "INSTAGRAM_ACCESS_TOKEN": INSTAGRAM_ACCESS_TOKEN,
    }
    if not _uses_instagram_login_api():
        required["INSTAGRAM_ACCOUNT_ID"] = INSTAGRAM_ACCOUNT_ID

    missing = [name for name, value in required.items() if not value]
    if missing:
        raise EnvironmentError(f"Missing required environment variables: {', '.join(missing)}")

    placeholders = [name for name, value in required.items() if _looks_like_placeholder(value)]
    if placeholders:
        raise EnvironmentError(
            f"{', '.join(placeholders)} still contain placeholder values in .env."
        )

    if not _uses_instagram_login_api():
        if not INSTAGRAM_ACCOUNT_ID.isdigit():
            raise EnvironmentError(
                f"INSTAGRAM_ACCOUNT_ID must be numeric, not '{INSTAGRAM_ACCOUNT_ID}'. "
                "Run: python main.py --lookup-ig-id"
            )


def validate_env() -> None:
    validate_instagram_env()

    if not GEMINI_API_KEY:
        raise EnvironmentError("Missing required environment variables: GEMINI_API_KEY")

    if _looks_like_placeholder(GEMINI_API_KEY):
        raise EnvironmentError("GEMINI_API_KEY still contains placeholder values in .env.")

    if not GEMINI_API_KEY.startswith(("AIza", "AQ.")):
        raise EnvironmentError(
            "GEMINI_API_KEY must start with 'AIza' or 'AQ.' from "
            "https://aistudio.google.com/apikey"
        )


def _gemini_models_to_try() -> list[str]:
    if os.getenv("GEMINI_FALLBACK_MODELS"):
        models = [GEMINI_MODEL]
        for model in GEMINI_FALLBACK_MODELS:
            if model not in models:
                models.append(model)
        return models

    models: list[str] = []
    for model in [GEMINI_MODEL, *MODELS_TO_TRY]:
        if model not in models:
            models.append(model)
    return models


def _is_invalid_gemini_api_key_error(exc: Exception) -> bool:
    message = str(exc)
    return "API key not valid" in message or "API_KEY_INVALID" in message


def build_gemini_prompt(recent_questions: list[str], *, duplicate_retry: bool = False) -> str:
    prompt = GEMINI_SYSTEM_PROMPT_BASE
    if recent_questions:
        prompt += (
            f"\n\nRecently published — DO NOT REUSE any of these last "
            f"{len(recent_questions)} questions:\n"
        )
        for question in recent_questions:
            prompt += f"- {question}\n"
        prompt += "Generate a completely different question not on this list."
    if duplicate_retry:
        prompt += (
            "\nYour previous answer duplicated a banned question. "
            "Generate something entirely new with different facts and topic."
        )
    return prompt


def _is_retryable_error(exc: Exception) -> bool:
    """Check if error is retryable (503, 429, rate limit, etc.)."""
    message = str(exc).lower()
    return any(term in message for term in ["503", "429", "unavailable", "rate limit", "overloaded", "high demand"])


def _call_gemini_for_content(client: genai.Client, prompt: str) -> QuizContent:
    config = types.GenerateContentConfig(
        temperature=0.9,
        response_mime_type="application/json",
        response_json_schema=CONTENT_JSON_SCHEMA,
    )
    last_error: Exception | None = None

    for model in _gemini_models_to_try():
        for retry in range(GEMINI_MAX_RETRIES):
            try:
                if retry > 0:
                    wait_time = GEMINI_RETRY_BASE_SECONDS * (2 ** (retry - 1))
                    logger.info("Retry %d/%d for %s — waiting %ds...", retry, GEMINI_MAX_RETRIES - 1, model, wait_time)
                    time.sleep(wait_time)

                logger.info("Attempting content generation with model: %s", model)
                response = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=config,
                )
                raw = response.text
                if not raw:
                    raise ValueError("Gemini returned an empty response")
                data = json.loads(raw)
                return QuizContent.from_dict(data)
            except json.JSONDecodeError as exc:
                logger.warning("Model %s returned invalid JSON (%s). Trying next model...", model, exc)
                last_error = exc
                break
            except ValueError as exc:
                logger.warning("Model %s validation failed (%s). Trying next model...", model, exc)
                last_error = exc
                break
            except genai.errors.ClientError as exc:
                if _is_invalid_gemini_api_key_error(exc):
                    raise RuntimeError("Invalid GEMINI_API_KEY.") from exc
                if _is_retryable_error(exc) and retry < GEMINI_MAX_RETRIES - 1:
                    logger.warning("Model %s got retryable error (%s). Will retry...", model, exc)
                    last_error = exc
                    continue
                logger.warning("Model %s failed (%s). Trying next model...", model, exc)
                last_error = exc
                break
            except Exception as exc:
                if _is_retryable_error(exc) and retry < GEMINI_MAX_RETRIES - 1:
                    logger.warning("Model %s got retryable error (%s). Will retry...", model, exc)
                    last_error = exc
                    continue
                logger.warning("Model %s failed (%s). Trying next model...", model, exc)
                last_error = exc
                break

    raise RuntimeError("All Gemini models failed.") from last_error


def generate_content() -> QuizContent:
    client = genai.Client(api_key=GEMINI_API_KEY)
    recent = load_recent_questions()
    if recent:
        logger.info("Avoiding %s recent questions from database", len(recent))

    for duplicate_attempt in range(1, DUPLICATE_CONTENT_RETRIES + 1):
        prompt = build_gemini_prompt(recent, duplicate_retry=duplicate_attempt > 1)
        content = _call_gemini_for_content(client, prompt)

        if is_duplicate_question(content.question):
            logger.warning(
                "Duplicate question blocked — regenerating (%s/%s)",
                duplicate_attempt,
                DUPLICATE_CONTENT_RETRIES,
            )
            continue

        logger.info("[✓] Gemini Content Generated — %s", content.question[:80])
        return content

    raise RuntimeError(
        f"Could not generate a unique question after {DUPLICATE_CONTENT_RETRIES} attempts."
    )


# ---------------------------------------------------------------------------
# Stage 2 — Visual Engine (F1 Quiz Card / Pillow)
# ---------------------------------------------------------------------------


def _sans_font_candidates(bold: bool = False) -> list[Path]:
    candidates: list[Path] = []
    env_path = os.getenv("SANS_FONT_PATH")
    if env_path:
        candidates.append(Path(env_path))
    if FONT_SANS_PATH.exists():
        candidates.append(FONT_SANS_PATH)
    if FONT_PATH.exists():
        candidates.append(FONT_PATH)

    if sys.platform == "darwin":
        if bold:
            candidates.extend([
                Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
                Path("/System/Library/Fonts/Helvetica.ttc"),
                Path("/Library/Fonts/Arial Bold.ttf"),
            ])
        else:
            candidates.extend([
                Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
                Path("/System/Library/Fonts/Helvetica.ttc"),
                Path("/Library/Fonts/Arial.ttf"),
            ])
    elif sys.platform.startswith("linux"):
        dejavu_name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
        candidates.extend([
            Path(f"/usr/share/fonts/truetype/dejavu/{dejavu_name}"),
            Path(f"/usr/share/fonts/dejavu/{dejavu_name}"),
            Path(f"/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
        ])
    else:
        candidates.append(
            Path("C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf")
        )
    return candidates


def _display_font_candidates() -> list[Path]:
    """Foda Display paths for the F1 Paddock Quiz header badge."""
    candidates: list[Path] = []
    env_path = os.getenv("FODA_DISPLAY_FONT_PATH")
    if env_path:
        candidates.append(Path(env_path))
    candidates.extend(FONT_DISPLAY_PATHS)

    if sys.platform == "darwin":
        candidates.extend([
            Path.home() / "Library/Fonts/Foda Display Regular.otf",
            Path.home() / "Library/Fonts/FodaDisplay-Regular.otf",
            Path("/Library/Fonts/Foda Display Regular.otf"),
            Path("/Library/Fonts/FodaDisplay-Regular.otf"),
        ])
    elif sys.platform.startswith("linux"):
        candidates.extend([
            Path.home() / ".local/share/fonts/FodaDisplay-Regular.otf",
            Path("/usr/local/share/fonts/FodaDisplay-Regular.otf"),
        ])
    else:
        candidates.append(Path("C:/Windows/Fonts/FodaDisplay-Regular.otf"))
    return candidates


def _load_display_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _display_font_candidates():
        if path.exists():
            try:
                font = ImageFont.truetype(str(path), size=size)
                logger.info("Foda Display loaded: %s (size %s)", path.name, size)
                return font
            except OSError:
                continue
    logger.warning("Foda Display not found — falling back to sans-serif for header")
    return _load_font(size, bold=True)


def _apply_sans_weight(
    font: ImageFont.FreeTypeFont,
    *,
    bold: bool,
) -> ImageFont.FreeTypeFont:
    if not bold:
        return font
    try:
        axes = font.get_variation_axes()
    except OSError:
        return font
    if not axes:
        return font
    values = [axis["default"] for axis in axes]
    for index, axis in enumerate(axes):
        if axis.get("name") == b"Weight" or index == 1:
            values[index] = min(700, axis["maximum"])
            break
    try:
        font.set_variation_by_axes(values)
    except OSError:
        pass
    return font


def _load_bundled_sans_font(
    size: int,
    *,
    bold: bool = False,
) -> ImageFont.FreeTypeFont | None:
    if not FONT_SANS_PATH.exists():
        return None
    try:
        font = ImageFont.truetype(str(FONT_SANS_PATH), size=size)
        font = _apply_sans_weight(font, bold=bold)
        logger.info("Bundled sans font loaded: %s (size %s, bold=%s)", FONT_SANS_PATH.name, size, bold)
        return font
    except OSError:
        return None


def _load_font(
    size: int,
    *,
    bold: bool = False,
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    bundled = _load_bundled_sans_font(size, bold=bold)
    if bundled is not None:
        return bundled

    for path in _sans_font_candidates(bold=bold):
        if path == FONT_SANS_PATH:
            continue
        if path.exists():
            try:
                font = ImageFont.truetype(str(path), size=size)
                logger.info("Sans font loaded: %s (size %s, bold=%s)", path.name, size, bold)
                return font
            except OSError:
                continue
    logger.warning("No sans-serif font found — using Pillow default")
    return ImageFont.load_default()


def _line_height(font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> int:
    dummy = Image.new("RGB", (1, 1))
    bbox = ImageDraw.Draw(dummy).textbbox((0, 0), "Ay", font=font)
    return bbox[3] - bbox[1]


def _text_width(
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> int:
    dummy = Image.new("RGB", (1, 1))
    bbox = ImageDraw.Draw(dummy).textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _wrap_text(
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
) -> list[str]:
    words = text.split()
    if not words:
        return [""]

    lines: list[str] = []
    current = words[0]
    dummy = Image.new("RGB", (1, 1))
    draw = ImageDraw.Draw(dummy)

    for word in words[1:]:
        trial = f"{current} {word}"
        if draw.textbbox((0, 0), trial, font=font)[2] <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _create_background(width: int, height: int) -> Image.Image:
    """Solid black canvas background."""
    return Image.new("RGB", (width, height), COLOR_BG)


def _rounded_rect(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int, int, int],
    radius: int,
    fill: tuple[int, ...],
    outline: tuple[int, ...] | None = None,
    width: int = 0,
) -> None:
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)


def _draw_option_row(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    letter: str,
    text: str,
    font_letter: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    font_option: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
    row_height: int = 84,
) -> int:
    """Option row — letter pill + full-width black rounded bar (reference layout)."""
    letter_label = f"{letter}."
    letter_w = 56
    gap = 12
    bar_x = x + letter_w + gap
    bar_w = max_width - letter_w - gap

    _rounded_rect(
        draw,
        (x, y, x + letter_w, y + row_height),
        radius=row_height // 2,
        fill=COLOR_BLACK,
    )
    letter_bbox = draw.textbbox((0, 0), letter_label, font=font_letter)
    letter_tx = x + (letter_w - (letter_bbox[2] - letter_bbox[0])) // 2
    letter_ty = y + (row_height - (letter_bbox[3] - letter_bbox[1])) // 2 - letter_bbox[1]
    draw.text((letter_tx, letter_ty), letter_label, font=font_letter, fill=COLOR_WHITE)

    lines = _wrap_text(text, font_option, bar_w - 48)
    line_h = _line_height(font_option)
    bar_h = max(row_height, len(lines) * line_h + 24)

    _rounded_rect(
        draw,
        (bar_x, y, bar_x + bar_w, y + bar_h),
        radius=bar_h // 2,
        fill=COLOR_BLACK,
    )

    ty = y + (bar_h - len(lines) * line_h) // 2
    for line in lines:
        draw.text((bar_x + 24, ty), line, font=font_option, fill=COLOR_WHITE)
        ty += line_h

    return y + bar_h + 18


def _measure_option_row_height(
    text: str,
    font_option: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
    row_height: int = 84,
) -> int:
    letter_w = 56
    gap = 12
    bar_w = max_width - letter_w - gap
    lines = _wrap_text(text, font_option, bar_w - 48)
    line_h = _line_height(font_option)
    bar_h = max(row_height, len(lines) * line_h + 24)
    return bar_h + 18


def _quiz_half_height(
    font_quiz_huge: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> int:
    """Vertical half of QUIZ text (incl. stroke) for card-edge alignment."""
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    probe = measure.textbbox((0, 0), "QUIZ", font=font_quiz_huge, stroke_width=QUIZ_STROKE_WIDTH)
    return (probe[3] - probe[1]) // 2


def _layout_header(
    card_left: int,
    card_right: int,
    card_top: int,
    font_quiz_huge: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    font_f1_badge: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> dict[str, Any]:
    """QUIZ centered on card; F1 PADDOCK badge overlaid on QUIZ from the right."""
    quiz_text = "QUIZ"
    f1_text = "F1 PADDOCK"
    f1_pad_x = 40
    f1_pad_y = 16

    f1_w = _text_width(f1_text, font_f1_badge) + f1_pad_x * 2
    f1_h = _line_height(font_f1_badge) + f1_pad_y * 2
    card_center_x = (card_left + card_right) // 2

    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    probe = measure.textbbox((0, 0), quiz_text, font=font_quiz_huge, stroke_width=QUIZ_STROKE_WIDTH)
    text_h = probe[3] - probe[1]
    quiz_w = probe[2] - probe[0]
    quiz_y = card_top - probe[1] - text_h // 2
    quiz_x = card_center_x - quiz_w // 2 - QUIZ_SHIFT_LEFT

    quiz_bbox = measure.textbbox(
        (quiz_x, quiz_y),
        quiz_text,
        font=font_quiz_huge,
        stroke_width=QUIZ_STROKE_WIDTH,
    )

    qui_bbox = measure.textbbox(
        (quiz_x, quiz_y),
        "QUI",
        font=font_quiz_huge,
        stroke_width=QUIZ_STROKE_WIDTH,
    )
    z_bbox = measure.textbbox(
        (qui_bbox[2], quiz_y),
        "Z",
        font=font_quiz_huge,
        stroke_width=QUIZ_STROKE_WIDTH,
    )
    z_center_y = (z_bbox[1] + z_bbox[3]) // 2

    f1_x = quiz_bbox[2] - f1_w + F1_BADGE_OVERLAP + F1_BADGE_SHIFT_RIGHT
    f1_y = z_center_y - f1_h // 2
    header_bottom = max(quiz_bbox[3], f1_y + f1_h)

    return {
        "quiz_text": quiz_text,
        "f1_text": f1_text,
        "quiz_x": quiz_x,
        "quiz_y": quiz_y,
        "quiz_bbox": quiz_bbox,
        "f1_x": f1_x,
        "f1_y": f1_y,
        "f1_w": f1_w,
        "f1_h": f1_h,
        "header_bottom": header_bottom,
        "quiz_half_h": text_h // 2,
    }


def create_post_image(content: QuizContent) -> Path:
    """Compose branded F1 quiz image on solid black background."""
    logger.info("Creating F1 quiz post image...")
    try:
        card_radius = 36
        card_bottom_pad = 44
        card_left = (CANVAS_WIDTH - CARD_WIDTH) // 2
        card_right = card_left + CARD_WIDTH

        font_quiz_huge = _load_display_font(QUIZ_FONT_SIZE)
        font_f1_badge = _load_display_font(56)
        font_question = _load_font(46, bold=True)
        font_option = _load_font(38)
        font_letter = _load_font(30, bold=True)
        quiz_half_h = _quiz_half_height(font_quiz_huge)

        # Measure content height, then center white card on canvas
        probe_card_top = 120
        header = _layout_header(card_left, card_right, probe_card_top, font_quiz_huge, font_f1_badge)
        inner_pad = 40
        inner_left = card_left + inner_pad
        inner_right = card_right - inner_pad
        inner_width = inner_right - inner_left

        y = max(header["header_bottom"] + 28, probe_card_top + quiz_half_h)
        question_lines = _wrap_text(content.question, font_question, inner_width)
        q_line_h = _line_height(font_question)
        for _ in question_lines:
            y += q_line_h + 10
        y += 32
        for letter in ("A", "B", "C", "D"):
            y += _measure_option_row_height(content.options[letter], font_option, inner_width)

        card_height = y + card_bottom_pad - probe_card_top
        visual_height = card_height + quiz_half_h
        card_top = (CANVAS_HEIGHT - visual_height) // 2 + quiz_half_h
        card_bottom = card_top + card_height

        header = _layout_header(card_left, card_right, card_top, font_quiz_huge, font_f1_badge)
        quiz_text = header["quiz_text"]
        f1_text = header["f1_text"]
        quiz_x = header["quiz_x"]
        quiz_y = header["quiz_y"]
        quiz_bbox = header["quiz_bbox"]
        f1_x = header["f1_x"]
        f1_y = header["f1_y"]
        f1_w = header["f1_w"]
        f1_h = header["f1_h"]
        header_bottom = header["header_bottom"]

        canvas = _create_background(CANVAS_WIDTH, CANVAS_HEIGHT)

        # Drop shadow
        shadow = Image.new("RGBA", (CANVAS_WIDTH, CANVAS_HEIGHT), (0, 0, 0, 0))
        shadow_draw = ImageDraw.Draw(shadow)
        _rounded_rect(
            shadow_draw,
            (card_left + 8, card_top + 12, card_right + 8, card_bottom + 12),
            radius=card_radius,
            fill=COLOR_SHADOW,
        )
        shadow = shadow.filter(ImageFilter.GaussianBlur(radius=18))
        canvas = canvas.convert("RGBA")
        canvas = Image.alpha_composite(canvas, shadow)
        canvas = canvas.convert("RGB")
        draw = ImageDraw.Draw(canvas)

        # White card — height fits content (no extra whitespace below options)
        _rounded_rect(
            draw,
            (card_left, card_top, card_right, card_bottom),
            radius=card_radius,
            fill=COLOR_WHITE,
        )

        draw.text(
            (quiz_x, quiz_y),
            quiz_text,
            font=font_quiz_huge,
            fill=COLOR_BLACK,
            stroke_width=QUIZ_STROKE_WIDTH,
            stroke_fill=COLOR_WHITE,
        )

        _rounded_rect(
            draw,
            (f1_x, f1_y, f1_x + f1_w, f1_y + f1_h),
            radius=f1_h // 2,
            fill=COLOR_WHITE,
            outline=COLOR_BLACK,
            width=3,
        )
        f1_bbox = draw.textbbox((0, 0), f1_text, font=font_f1_badge)
        f1_tx = f1_x + (f1_w - (f1_bbox[2] - f1_bbox[0])) // 2
        f1_ty = f1_y + (f1_h - (f1_bbox[3] - f1_bbox[1])) // 2 - f1_bbox[1]
        draw.text((f1_tx, f1_ty), f1_text, font=font_f1_badge, fill=COLOR_BLACK)

        y = max(header_bottom + 28, card_top + quiz_half_h)

        for line in question_lines:
            draw.text((inner_left, y), line, font=font_question, fill=COLOR_GRAY_TEXT)
            y += q_line_h + 10
        y += 32

        for letter in ("A", "B", "C", "D"):
            y = _draw_option_row(
                draw,
                inner_left,
                y,
                letter,
                content.options[letter],
                font_letter,
                font_option,
                inner_width,
            )

        canvas.save(OUTPUT_IMAGE, format="JPEG", quality=95, optimize=True)
        logger.info("[✓] Image Created — saved to %s", OUTPUT_IMAGE)
        return OUTPUT_IMAGE
    except Exception as exc:
        logger.exception("Image creation failed")
        raise RuntimeError("Image creation failed") from exc


# ---------------------------------------------------------------------------
# Stage 3 — Hosting
# ---------------------------------------------------------------------------

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
CATBOX_HEADERS = {
    "User-Agent": BROWSER_USER_AGENT,
    "Referer": "https://catbox.moe/",
    "Origin": "https://catbox.moe",
    "Accept": "*/*",
}
LITTERBOX_HEADERS = {
    "User-Agent": BROWSER_USER_AGENT,
    "Referer": "https://litterbox.catbox.moe/",
    "Origin": "https://litterbox.catbox.moe",
    "Accept": "*/*",
}


def _github_branch() -> str:
    ref = os.getenv("GITHUB_REF", "refs/heads/main")
    if ref.startswith("refs/heads/"):
        return ref.removeprefix("refs/heads/")
    if ref.startswith("refs/tags/"):
        return ref.removeprefix("refs/tags/")
    return "main"


def _github_repository() -> str | None:
    repo = (os.getenv("GITHUB_REPOSITORY") or "").strip()
    return repo if "/" in repo else None


def _github_token() -> str | None:
    token = (os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or "").strip()
    return token or None


def _verify_public_image_url(url: str) -> bool:
    """Return True when URL serves a direct image Instagram can fetch."""
    headers = {"User-Agent": BROWSER_USER_AGENT}
    trusted_prefixes = (
        "https://files.catbox.moe/",
        "https://litter.catbox.moe/",
        "https://i.imgur.com/",
        "https://raw.githubusercontent.com/",
    )
    try:
        response = requests.head(url, headers=headers, timeout=30, allow_redirects=True)
        if response.status_code >= 400:
            response = requests.get(
                url,
                headers={**headers, "Range": "bytes=0-511"},
                timeout=30,
                allow_redirects=True,
            )
        content_type = response.headers.get("Content-Type", "").lower()
        if content_type.startswith("image/"):
            return True
        if "text/html" in content_type:
            return False
    except requests.RequestException as exc:
        if any(url.startswith(prefix) for prefix in trusted_prefixes):
            logger.warning(
                "Could not probe %s locally (%s) — trusting known CDN URL",
                url,
                exc,
            )
            return True
        return False
    return False


def upload_to_imgur(image_path: Path) -> str:
    """Upload to Imgur — reliable public URL for Instagram Graph API."""
    if not IMGUR_CLIENT_ID:
        raise ValueError("IMGUR_CLIENT_ID not configured")

    logger.info("Uploading image to Imgur...")
    try:
        with image_path.open("rb") as handle:
            response = requests.post(
                "https://api.imgur.com/3/image",
                headers={"Authorization": f"Client-ID {IMGUR_CLIENT_ID}"},
                files={"image": (image_path.name, handle, "image/jpeg")},
                timeout=60,
            )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success"):
            raise ValueError(f"Imgur API error: {payload}")
        data = payload.get("data", {})
        url = data.get("link") or data.get("url")
        if not url or not str(url).startswith("https://"):
            raise ValueError(f"Unexpected Imgur response: {payload}")
        logger.info("[✓] Hosted at Imgur — %s", url)
        return str(url)
    except (requests.RequestException, ValueError, TypeError) as exc:
        logger.exception("Imgur upload failed")
        raise RuntimeError("Imgur upload failed") from exc


def upload_to_github_contents(image_path: Path) -> str:
    """Publish image to the repo via GitHub API — works from GitHub Actions runners."""
    token = _github_token()
    repo = _github_repository()
    if not token or not repo:
        raise ValueError("GITHUB_TOKEN and GITHUB_REPOSITORY required for GitHub hosting")

    owner, repo_name = repo.split("/", 1)
    branch = _github_branch()
    path = GITHUB_HOST_PATH
    api_url = f"https://api.github.com/repos/{owner}/{repo_name}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    content_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    body: dict[str, Any] = {
        "message": "ci: update Instagram post image",
        "content": content_b64,
        "branch": branch,
    }

    logger.info("Uploading image to GitHub Contents API (%s on %s)...", path, branch)
    try:
        existing = requests.get(api_url, headers=headers, params={"ref": branch}, timeout=30)
        if existing.status_code == 200:
            sha = existing.json().get("sha")
            if sha:
                body["sha"] = sha

        response = requests.put(api_url, headers=headers, json=body, timeout=60)
        response.raise_for_status()
        url = (
            f"https://raw.githubusercontent.com/{owner}/{repo_name}/{branch}/{path}"
        )
        logger.info("[✓] Hosted on GitHub — %s", url)
        return url
    except requests.RequestException as exc:
        logger.exception("GitHub Contents upload failed")
        raise RuntimeError("GitHub Contents upload failed") from exc


def upload_to_litterbox(image_path: Path) -> str:
    """Upload to Litterbox — direct files.catbox.moe-style JPEG URL for Instagram."""
    logger.info("Uploading image to Litterbox...")
    try:
        with image_path.open("rb") as handle:
            response = requests.post(
                "https://litterbox.catbox.moe/resources/internals/api.php",
                data={"reqtype": "fileupload", "time": "24h"},
                files={"fileToUpload": (image_path.name, handle, "image/jpeg")},
                headers=LITTERBOX_HEADERS,
                timeout=60,
            )
        response.raise_for_status()
        url = response.text.strip()
        if not url.startswith("https://"):
            raise ValueError(f"Unexpected Litterbox response: {url[:200]}")
        logger.info("[✓] Hosted at Litterbox — %s", url)
        return url
    except (requests.RequestException, ValueError) as exc:
        logger.exception("Litterbox upload failed")
        raise RuntimeError("Litterbox upload failed") from exc


def upload_to_catbox(image_path: Path) -> str:
    """Upload to catbox.moe with browser-like headers."""
    logger.info("Uploading image to Catbox...")
    try:
        with image_path.open("rb") as handle:
            response = requests.post(
                "https://catbox.moe/user/api.php",
                data={"reqtype": "fileupload"},
                files={"fileToUpload": (image_path.name, handle, "image/jpeg")},
                headers=CATBOX_HEADERS,
                timeout=60,
            )
        response.raise_for_status()
        url = response.text.strip()
        if not url.startswith("https://"):
            raise ValueError(f"Unexpected Catbox response: {url[:200]}")
        logger.info("[✓] Hosted at Catbox — %s", url)
        return url
    except (requests.RequestException, ValueError) as exc:
        logger.exception("Catbox upload failed")
        raise RuntimeError("Catbox upload failed") from exc


def host_image(image_path: Path) -> str:
    """Upload and return an Instagram-compatible direct image URL."""
    uploaders: list[tuple[str, Any]] = []

    if IMGUR_CLIENT_ID:
        uploaders.append(("Imgur", upload_to_imgur))

    # GitHub Actions: Catbox often returns 412 from datacenter IPs — prefer GitHub hosting
    if _github_token() and _github_repository():
        uploaders.append(("GitHub", upload_to_github_contents))

    uploaders.extend([
        ("Catbox", upload_to_catbox),
        ("Litterbox", upload_to_litterbox),
    ])

    errors: list[str] = []

    for name, upload in uploaders:
        try:
            url = upload(image_path)
            if _verify_public_image_url(url):
                return url
            logger.warning(
                "%s URL is not a direct image (Instagram would reject it): %s",
                name,
                url,
            )
            errors.append(f"{name}: URL does not serve image/* content-type")
        except (RuntimeError, requests.RequestException, ValueError) as exc:
            errors.append(f"{name}: {exc}")
            logger.warning("%s failed — trying next host...", name)

    raise RuntimeError("All image hosts failed — " + "; ".join(errors))


# ---------------------------------------------------------------------------
# Stage 4 — Instagram Graph API Publishing
# ---------------------------------------------------------------------------


def _graph_request(method: str, endpoint: str, **kwargs: Any) -> dict[str, Any]:
    api_label = "Instagram Login API" if _uses_instagram_login_api() else "Facebook Graph API"
    url = f"{_instagram_api_base()}/{endpoint.lstrip('/')}"
    params = kwargs.pop("params", {})
    params["access_token"] = INSTAGRAM_ACCESS_TOKEN

    try:
        response = requests.request(method, url, params=params, timeout=60, **kwargs)
        data = response.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"{api_label} request failed: {endpoint}") from exc

    if not response.ok or "error" in data:
        error = data.get("error", {})
        raise RuntimeError(_format_graph_api_error(error, api_label))
    return data


def create_media_container(image_url: str, caption: str, account_id: str) -> str:
    logger.info("Creating Instagram media container...")
    data = _graph_request(
        "POST",
        f"{account_id}/media",
        data={"image_url": image_url, "caption": caption},
    )
    creation_id = data.get("id")
    if not creation_id:
        raise RuntimeError("Media container response missing creation id")
    logger.info("Media container created — id: %s", creation_id)
    return creation_id


def wait_for_container_ready(container_id: str) -> None:
    logger.info("Polling container status...")
    deadline = time.time() + POLL_MAX_WAIT_SECONDS
    terminal_error_states = {"ERROR", "EXPIRED"}

    while time.time() < deadline:
        data = _graph_request("GET", container_id, params={"fields": "status_code,status"})
        status = data.get("status_code", "UNKNOWN")
        logger.info("Container status: %s", status)

        if status == "FINISHED":
            return
        if status in terminal_error_states:
            raise RuntimeError(f"Container entered terminal state: {status}")
        if status == "PUBLISHED":
            return
        time.sleep(POLL_INTERVAL_SECONDS)

    raise RuntimeError(f"Container not ready after {POLL_MAX_WAIT_SECONDS}s")


def publish_media(container_id: str, account_id: str) -> str:
    logger.info("Publishing to Instagram...")
    data = _graph_request(
        "POST",
        f"{account_id}/media_publish",
        data={"creation_id": container_id},
    )
    media_id = data.get("id")
    if not media_id:
        raise RuntimeError("Publish response missing media id")
    logger.info("[✓] Published to Instagram — media id: %s", media_id)
    return media_id


def publish_to_instagram(image_url: str, caption: str) -> str:
    account_id = _resolve_instagram_account_id()
    container_id = create_media_container(image_url, caption, account_id)
    wait_for_container_ready(container_id)
    return publish_media(container_id, account_id)


def post_comment(media_id: str, message: str) -> str:
    """Post a comment on a published Instagram media object."""
    logger.info("Posting answer comment on media %s...", media_id)
    data = _graph_request(
        "POST",
        f"{media_id}/comments",
        data={"message": message},
    )
    comment_id = data.get("id")
    if not comment_id:
        raise RuntimeError("Comment response missing id")
    logger.info("[✓] Comment posted — id: %s", comment_id)
    return comment_id


def post_disclaimer_comment(media_id: str) -> str:
    """Post the fixed disclaimer as the first comment on a published post."""
    logger.info("Posting disclaimer as first comment on media %s...", media_id)
    return post_comment(media_id, ANSWER_COMMENT_FOOTER)


def process_pending_comments() -> int:
    """Post all scheduled comments whose delay has elapsed."""
    pending = get_pending_comments()
    if not pending:
        logger.info("No pending comments to post")
        return 0

    posted = 0
    for row in pending:
        try:
            post_comment(row["media_id"], row["comment_text"])
            mark_comment_posted(row["id"])
            posted += 1
            logger.info("Answer comment posted for question id %s", row["id"])
        except RuntimeError as exc:
            logger.error("Failed to post comment for id %s: %s", row["id"], exc)
    return posted


def wait_and_post_comment(record_id: int, media_id: str, comment_text: str, scheduled_at: datetime) -> None:
    """Sleep until scheduled time then post the answer comment."""
    now = datetime.now(timezone.utc)
    if scheduled_at > now:
        wait_seconds = (scheduled_at - now).total_seconds()
        logger.info("Waiting %.0f seconds until answer comment (scheduled at %s)", wait_seconds, scheduled_at.isoformat())
        time.sleep(wait_seconds)
    post_comment(media_id, comment_text)
    mark_comment_posted(record_id)


# ---------------------------------------------------------------------------
# Pipeline Orchestration
# ---------------------------------------------------------------------------


def test_instagram_token() -> None:
    """Print Instagram token diagnostics without publishing."""
    if not INSTAGRAM_ACCESS_TOKEN:
        raise EnvironmentError("INSTAGRAM_ACCESS_TOKEN is required")

    token = INSTAGRAM_ACCESS_TOKEN
    print(f"Token prefix: {token[:8]}...")
    print(f"Token length: {len(token)}")
    print(f"API mode: {'Instagram Login' if _uses_instagram_login_api() else 'Facebook Graph'}")
    if INSTAGRAM_ACCOUNT_ID:
        print(f"INSTAGRAM_ACCOUNT_ID (env): {INSTAGRAM_ACCOUNT_ID}")

    base = _instagram_api_base()
    print(f"API host: {base}")

    try:
        data = _graph_request("GET", "me", params={"fields": "id,user_id,username,account_type"})
        print(f"[OK] /me — @{data.get('username', 'unknown')}")
        print(f"     publish id: {data.get('user_id') or data.get('id')}")
    except RuntimeError as exc:
        print(f"[FAIL] /me — {exc}")

    if INSTAGRAM_ACCOUNT_ID and INSTAGRAM_ACCOUNT_ID.isdigit():
        try:
            data = _graph_request(
                "GET",
                INSTAGRAM_ACCOUNT_ID,
                params={"fields": "id,username,name"},
            )
            print(f"[OK] account — @{data.get('username', 'unknown')}")
        except RuntimeError as exc:
            print(f"[FAIL] account lookup — {exc}")


def run_pipeline(image_only: bool = False) -> None:
    logger.info("=" * 60)
    logger.info("F1 Paddock Quiz — Instagram Automation Pipeline")
    logger.info("=" * 60)

    validate_env()
    init_db()

    # Post any overdue comments from previous runs first (JSON only)
    overdue = process_pending_comments_json()
    if overdue:
        logger.info("Posted %s overdue answer comment(s)", overdue)

    content = generate_content()
    if image_only:
        image_path = create_post_image(content)
        logger.info("=" * 60)
        logger.info("Image-only mode — Instagram publish skipped")
        logger.info("  Question: %s", content.question)
        logger.info("  Image   : %s", image_path.resolve())
        logger.info("=" * 60)
        return

    record_id = record_question(content)

    image_path = create_post_image(content)
    public_url = host_image(image_path)
    media_id = publish_to_instagram(public_url, content.full_caption())

    try:
        post_disclaimer_comment(media_id)
    except RuntimeError as exc:
        logger.error("Failed to post disclaimer comment: %s", exc)

    scheduled_at = datetime.now(timezone.utc) + timedelta(hours=COMMENT_DELAY_HOURS)
    comment_text = content.answer_comment()
    mark_published(record_id, media_id, scheduled_at.isoformat(), comment_text)

    add_pending_comment_json(
        media_id=media_id,
        comment_text=comment_text,
        scheduled_at=scheduled_at.isoformat(),
        question_text=content.question,
    )

    logger.info(
        "Answer comment scheduled for %s (%d hours after publish)",
        scheduled_at.isoformat(),
        COMMENT_DELAY_HOURS,
    )

    if WAIT_FOR_COMMENT:
        wait_and_post_comment(record_id, media_id, comment_text, scheduled_at)
        remove_pending_comment_json(media_id)
    else:
        logger.info(
            "Run `python main.py --post-comments` after %s to publish the answer, "
            "or set WAIT_FOR_COMMENT=true to wait in-process.",
            scheduled_at.isoformat(),
        )

    logger.info("=" * 60)
    logger.info("Pipeline complete!")
    logger.info("  Question: %s", content.question)
    logger.info("  Answer  : %s — %s", content.correct_option, content.options[content.correct_option])
    logger.info("  Image   : %s", image_path.resolve())
    logger.info("  URL     : %s", public_url)
    logger.info("  Media ID: %s", media_id)
    logger.info("=" * 60)


def process_pending_comments_json() -> int:
    """Post all due answer comments from JSON storage (primary method)."""
    due = get_due_pending_comments_json()
    if not due:
        logger.info("No pending comments due in JSON storage")
        return 0

    posted = 0
    for item in due:
        media_id = item.get("media_id")
        comment_text = item.get("comment_text")
        if not media_id or not comment_text:
            logger.warning("Invalid pending comment entry: %s", item)
            continue

        try:
            post_comment(media_id, comment_text)
            remove_pending_comment_json(media_id)
            posted += 1
            logger.info(
                "Answer comment posted for media_id %s (question: %s...)",
                media_id,
                item.get("question_text", "")[:50],
            )
        except RuntimeError as exc:
            logger.error("Failed to post comment for media_id %s: %s", media_id, exc)
    return posted


def run_comment_job() -> None:
    """Standalone job: post all due answer comments from JSON storage."""
    validate_instagram_env()

    count = process_pending_comments_json()
    logger.info("Comment job finished — %s comment(s) posted", count)


def lookup_instagram_account_id() -> None:
    if not INSTAGRAM_ACCESS_TOKEN:
        raise EnvironmentError("INSTAGRAM_ACCESS_TOKEN is required for lookup")

    if _uses_instagram_login_api():
        data = _graph_request("GET", "me", params={"fields": "id,user_id,username,name,account_type"})
        print("Token type: Instagram Login (IGAA...)")
        print(f"  Username: @{data.get('username', 'unknown')}")
        print(f"  INSTAGRAM_ACCOUNT_ID={data.get('user_id') or data.get('id')}")
        return

    logger.info("Looking up Instagram Business Account ID...")
    response = requests.get(
        f"{GRAPH_API_BASE}/me/accounts",
        params={
            "access_token": INSTAGRAM_ACCESS_TOKEN,
            "fields": "id,name,instagram_business_account{id,username,name}",
        },
        timeout=30,
    )
    data = response.json()
    if "error" in data:
        raise RuntimeError(f"Graph API error: {data['error'].get('message')}")

    for page in data.get("data", []):
        ig = page.get("instagram_business_account") or {}
        if ig.get("id"):
            print(f"Page: {page.get('name')}")
            print(f"  INSTAGRAM_ACCOUNT_ID={ig['id']}")
            print(f"  Username: @{ig.get('username', 'unknown')}")


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--lookup-ig-id":
        load_dotenv()
        try:
            lookup_instagram_account_id()
            return 0
        except (EnvironmentError, RuntimeError) as exc:
            logger.error("%s", exc)
            return 1

    if len(sys.argv) > 1 and sys.argv[1] == "--show-history":
        load_dotenv()
        show_question_history()
        return 0

    if len(sys.argv) > 1 and sys.argv[1] == "--post-comments":
        load_dotenv()
        try:
            run_comment_job()
            return 0
        except (EnvironmentError, RuntimeError) as exc:
            logger.error("%s", exc)
            return 1

    if len(sys.argv) > 1 and sys.argv[1] == "--test-token":
        load_dotenv()
        try:
            test_instagram_token()
            return 0
        except (EnvironmentError, RuntimeError) as exc:
            logger.error("%s", exc)
            return 1

    if len(sys.argv) > 1 and sys.argv[1] == "--image-only":
        load_dotenv()
        try:
            validate_env()
            run_pipeline(image_only=True)
            return 0
        except (EnvironmentError, RuntimeError) as exc:
            logger.error("Pipeline failed: %s", exc)
            return 1

    try:
        run_pipeline()
        return 0
    except (EnvironmentError, RuntimeError) as exc:
        logger.error("Pipeline failed: %s", exc)
        return 1
    except KeyboardInterrupt:
        logger.warning("Pipeline interrupted by user")
        return 130


if __name__ == "__main__":
    sys.exit(main())
