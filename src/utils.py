"""
Utility functions and logging setup for Telebrief.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

TELEGRAM_MAX_MESSAGE_CHARS = 32768
TELEGRAM_SAFE_MESSAGE_CHARS = 32000


def setup_logging(log_level: str = "INFO") -> logging.Logger:
    """
    Set up logging configuration.

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR)

    Returns:
        Configured logger instance
    """
    # Create logs directory if it doesn't exist
    os.makedirs("logs", exist_ok=True)

    level = getattr(logging, log_level.upper())
    # Configure logging format
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"
    formatter = logging.Formatter(log_format, date_format)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)

    # File handler
    log_file = "logs/telebrief.log"
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)

    # Configure root logger so all modules (src.*, procrastinate, etc.) log to file and console
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers.clear()
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    # Create telebrief logger
    logger = logging.getLogger("telebrief")
    logger.setLevel(level)
    logger.handlers.clear()
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    logger.propagate = False

    # Suppress verbose third-party loggers that spam repetitive low-value messages
    logging.getLogger("procrastinate").setLevel(logging.WARNING)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    # Suppress noisy Telethon MTProto push notifications ("Got difference for channel ...")
    # These are normal server-push updates that arrive while the connection is open;
    # they are not actionable and clutter the log during long processing runs.
    logging.getLogger("telethon.client.updates").setLevel(logging.WARNING)

    return logger


def get_lookback_time(hours: int) -> datetime:
    """
    Calculate lookback datetime.

    Args:
        hours: Number of hours to look back

    Returns:
        Datetime object
    """
    return datetime.now(timezone.utc) - timedelta(hours=hours)


def split_message(text: str, max_length: int = 4000) -> list[str]:
    """
    Split long message into multiple parts for Telegram.

    Args:
        text: Text to split
        max_length: Maximum length per message

    Returns:
        List of message parts
    """
    if len(text) <= max_length:
        return [text]

    parts = []
    current_part = ""

    for line in text.split("\n"):
        # Handle very long lines that exceed max_length
        if len(line) > max_length:
            # Save current part if it exists
            if current_part:
                parts.append(current_part.strip())
                current_part = ""

            # Split the long line into chunks
            while len(line) > max_length:
                parts.append(line[:max_length])
                line = line[max_length:]

            # Add remaining part of line to current_part
            if line:
                current_part = line + "\n"
        elif len(current_part) + len(line) + 1 <= max_length:
            current_part += line + "\n"
        else:
            if current_part:
                parts.append(current_part.strip())
            current_part = line + "\n"

    if current_part:
        parts.append(current_part.strip())

    return parts


# Message ID storage for cleanup functionality
MESSAGE_STORAGE_FILE = "data/digest_messages.json"


def save_digest_message_ids(message_ids: List[int], user_id: str | int) -> None:
    """
    Save message IDs of sent digest messages for later cleanup.

    Args:
        message_ids: List of Telegram message IDs
        user_id: Target user ID
    """
    # Create data directory if it doesn't exist
    storage_path = Path(MESSAGE_STORAGE_FILE)
    storage_path.parent.mkdir(parents=True, exist_ok=True)

    # Load existing data
    data = {}
    if storage_path.exists():
        try:
            with open(storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError):
            data = {}

    # Store message IDs with timestamp
    user_key = str(user_id)
    data[user_key] = {
        "message_ids": message_ids,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # Save to file
    with open(storage_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_digest_message_ids(user_id: str | int) -> List[int]:
    """
    Get stored message IDs for cleanup.

    Args:
        user_id: Target user ID

    Returns:
        List of message IDs
    """
    storage_path = Path(MESSAGE_STORAGE_FILE)

    if not storage_path.exists():
        return []

    try:
        with open(storage_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        user_key = str(user_id)
        if user_key in data:
            message_ids: List[int] = data[user_key].get("message_ids", [])
            return message_ids

    except (json.JSONDecodeError, IOError, KeyError):
        pass

    return []


def clear_digest_message_ids(user_id: str | int) -> None:
    """
    Clear stored message IDs after cleanup.

    Args:
        user_id: Target user ID
    """
    storage_path = Path(MESSAGE_STORAGE_FILE)

    if not storage_path.exists():
        return

    try:
        with open(storage_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        user_key = str(user_id)
        if user_key in data:
            del data[user_key]

        # Save updated data
        with open(storage_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    except (json.JSONDecodeError, IOError, KeyError):
        pass
