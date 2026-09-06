# Telegram UGC News Bot ("Предложка") with AI Literary Editor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a dedicated Telegram crowdsourced news submission bot ("Предложка") that accepts raw user reports and media, transforms them via an AI literary editor into verified, readable local news posts adhering to Telebrief's regional editorial contract, and routes them through a Telegram-native admin moderation approval queue before channel publication.

**Architecture:** A lightweight, asynchronous service built on `python-telegram-bot>=20.0` and integrated with Telebrief's `AIProvider` cascade. Users submit arbitrary text, photos, or media albums. A debouncing buffer aggregates media groups; an AI Literary Editor transforms raw inputs into structured headline/lead/body drafts with strict resident attribution ("по сообщениям жителей") and Evidence Boundary guards; an Admin Moderation engine in a private Telegram chat provides interactive inline buttons for one-click publishing, manual editing, rewriting, or asking the author for clarification; approved posts publish to the public channel and feed into Telebrief's persistent storage for the daily digest.

**Tech Stack:** Python 3.14, `python-telegram-bot>=20.0`, `aiosqlite`, `pytest`, `pytest-asyncio`, Telebrief `AIProvider` & `news-style`.

---

### File Structure Map

- **Models & Config:**
  - `src/ugc/models.py`: Data structures (`UGCSubmission`, `UGCEditedDraft`, `UGCStatus`).
  - `src/ugc/config.py`: UGC configuration loader (`UGCConfig`).
- **Storage:**
  - `src/ugc/repository.py`: Async SQLite repository for submissions and moderation state.
- **Media Ingestion:**
  - `src/ugc/buffer.py`: Debouncing buffer for Telegram albums (`media_group_id`).
- **AI Literary Editor:**
  - `src/ugc/editor.py`: Prompt builder and AI editor following Telebrief's `news-style` and Evidence Boundary.
- **Moderation & Publishing:**
  - `src/ugc/moderation.py`: Formatter for admin review cards, inline keyboards, and channel publication logic.
- **Bot Application:**
  - `src/ugc/bot.py`: Telegram bot handlers (start, help, message ingestion, callback query handling, user clarification routing).
- **Integration & Entrypoint:**
  - `src/ugc/runner.py`: Lifecycle runner and hook to persist approved news into Telebrief's `src/storage.py`.
- **Tests:**
  - `tests/ugc/test_models.py`
  - `tests/ugc/test_repository.py`
  - `tests/ugc/test_buffer.py`
  - `tests/ugc/test_editor.py`
  - `tests/ugc/test_moderation.py`
  - `tests/ugc/test_bot.py`
  - `tests/ugc/test_runner.py`

---

### Task 1: UGC Domain Models & Configuration

**Files:**
- Create: `src/ugc/__init__.py`
- Create: `src/ugc/models.py`
- Create: `src/ugc/config.py`
- Test: `tests/ugc/__init__.py`
- Test: `tests/ugc/test_models.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/ugc/test_models.py
import pytest
from src.ugc.models import UGCSubmission, UGCEditedDraft, UGCStatus
from src.ugc.config import UGCConfig

def test_ugc_models_initialization():
    draft = UGCEditedDraft(
        title="В нагорной части города отключился светофор",
        lead="На перекрестке Победы и Мира не работает светофорный объект.",
        body="По сообщениям очевидцев, на участке затруднено движение транспорта.",
        spam_or_inappropriate=False,
    )
    submission = UGCSubmission(
        id="sub-123",
        user_id=111222,
        username="testuser",
        first_name="Иван",
        raw_text="на победы светофор сдох",
        media_type="photo",
        media_file_ids=["file_photo_1"],
        draft=draft,
    )
    assert submission.status == UGCStatus.PENDING_REVIEW
    assert submission.draft.title == "В нагорной части города отключился светофор"
    assert submission.is_anonymous is True

def test_ugc_config_from_dict_and_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_UGC_BOT_TOKEN", "123:ABC_TEST")
    monkeypatch.setenv("TELEGRAM_UGC_ADMIN_CHAT_ID", "-100999888")
    monkeypatch.setenv("TELEGRAM_UGC_TARGET_CHANNEL_ID", "@city_channel")
    
    config = UGCConfig.from_env()
    assert config.bot_token == "123:ABC_TEST"
    assert config.admin_chat_id == -100999888
    assert config.target_channel_id == "@city_channel"
    assert config.enabled is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/ugc/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.ugc'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/ugc/__init__.py
"""UGC (User-Generated Content) submission and moderation package."""
```

```python
# src/ugc/models.py
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional


class UGCStatus(str, Enum):
    PENDING_REVIEW = "pending_review"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    PUBLISHED = "published"
    REJECTED = "rejected"


@dataclass
class UGCEditedDraft:
    """Draft produced by the AI Literary Editor."""

    title: str
    lead: str
    body: str
    spam_or_inappropriate: bool = False
    rejection_reason: Optional[str] = None
    verification_warning: Optional[str] = None

    def format_post(self) -> str:
        """Format post text with standard journalistic layout."""
        parts = [f"*{self.title}*", "", self.lead]
        if self.body and self.body != self.lead:
            parts.extend(["", self.body])
        return "\n".join(parts)


@dataclass
class UGCSubmission:
    """User submission record."""

    id: str
    user_id: int
    first_name: str
    raw_text: str
    username: Optional[str] = None
    media_type: str = "none"  # "none", "photo", "album", "video"
    media_file_ids: List[str] = field(default_factory=list)
    status: UGCStatus = UGCStatus.PENDING_REVIEW
    is_anonymous: bool = True
    draft: Optional[UGCEditedDraft] = None
    admin_message_id: Optional[int] = None
    published_message_id: Optional[int] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
```

```python
# src/ugc/config.py
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, Union


@dataclass
class UGCConfig:
    """Configuration for UGC News Bot."""

    bot_token: str
    admin_chat_id: Union[int, str]
    target_channel_id: Union[int, str]
    model: str = "minimax/minimax-m3:free:floor"
    enabled: bool = True
    db_path: str = "data/ugc_submissions.db"
    cooldown_seconds: int = 30

    @classmethod
    def from_env(cls) -> UGCConfig:
        """Load UGC config from environment variables."""
        bot_token = os.getenv("TELEGRAM_UGC_BOT_TOKEN", "").strip()
        admin_chat_raw = os.getenv("TELEGRAM_UGC_ADMIN_CHAT_ID", "").strip()
        target_channel_raw = os.getenv("TELEGRAM_UGC_TARGET_CHANNEL_ID", "").strip()

        admin_chat_id: Union[int, str]
        if admin_chat_raw.lstrip("-").isdigit():
            admin_chat_id = int(admin_chat_raw)
        else:
            admin_chat_id = admin_chat_raw

        target_channel_id: Union[int, str]
        if target_channel_raw.lstrip("-").isdigit():
            target_channel_id = int(target_channel_raw)
        else:
            target_channel_id = target_channel_raw

        return cls(
            bot_token=bot_token,
            admin_chat_id=admin_chat_id,
            target_channel_id=target_channel_id,
            model=os.getenv("TELEGRAM_UGC_MODEL", "minimax/minimax-m3:free:floor"),
            enabled=os.getenv("TELEGRAM_UGC_ENABLED", "true").lower() in ("true", "1", "yes"),
            db_path=os.getenv("TELEGRAM_UGC_DB_PATH", "data/ugc_submissions.db"),
        )
```

```python
# tests/ugc/__init__.py
"""Tests for UGC bot."""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/ugc/test_models.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ugc/__init__.py src/ugc/models.py src/ugc/config.py tests/ugc/__init__.py tests/ugc/test_models.py
git commit -m "feat(ugc): define UGC submission models and configuration loader"
```

---

### Task 2: Async SQLite UGC Repository

**Files:**
- Create: `src/ugc/repository.py`
- Test: `tests/ugc/test_repository.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/ugc/test_repository.py
import pytest
from src.ugc.models import UGCSubmission, UGCEditedDraft, UGCStatus
from src.ugc.repository import UGCRepository

@pytest.mark.asyncio
async def test_ugc_repository_lifecycle(tmp_path):
    db_path = str(tmp_path / "test_ugc.db")
    repo = UGCRepository(db_path=db_path)
    await repo.init_db()

    draft = UGCEditedDraft(
        title="Тестовая новость",
        lead="Тестовый лид события.",
        body="Тестовое тело заметки.",
    )
    sub = UGCSubmission(
        id="sub-100",
        user_id=42,
        username="reporter",
        first_name="Петр",
        raw_text="На улице Ленина упало дерево",
        media_type="photo",
        media_file_ids=["file1", "file2"],
        draft=draft,
    )

    await repo.save_submission(sub)
    loaded = await repo.get_submission("sub-100")
    assert loaded is not None
    assert loaded.id == "sub-100"
    assert loaded.raw_text == "На улице Ленина упало дерево"
    assert loaded.media_file_ids == ["file1", "file2"]
    assert loaded.draft is not None
    assert loaded.draft.title == "Тестовая новость"
    assert loaded.status == UGCStatus.PENDING_REVIEW

    # Update status and admin message id
    await repo.update_status(
        submission_id="sub-100",
        status=UGCStatus.PUBLISHED,
        admin_message_id=999,
        published_message_id=888,
    )

    updated = await repo.get_submission("sub-100")
    assert updated.status == UGCStatus.PUBLISHED
    assert updated.admin_message_id == 999
    assert updated.published_message_id == 888
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/ugc/test_repository.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.ugc.repository'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/ugc/repository.py
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

from src.ugc.models import UGCEditedDraft, UGCStatus, UGCSubmission


class UGCRepository:
    """Asynchronous SQLite repository for UGC submissions."""

    def __init__(self, db_path: str = "data/ugc_submissions.db"):
        self.db_path = db_path

    async def init_db(self) -> None:
        """Initialize submissions table and indices."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS ugc_submissions (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    first_name TEXT NOT NULL,
                    raw_text TEXT NOT NULL,
                    media_type TEXT NOT NULL DEFAULT 'none',
                    media_file_ids TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'pending_review',
                    is_anonymous INTEGER NOT NULL DEFAULT 1,
                    draft_json TEXT,
                    admin_message_id INTEGER,
                    published_message_id INTEGER,
                    created_at TEXT NOT NULL
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_ugc_status ON ugc_submissions(status)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_ugc_user_id ON ugc_submissions(user_id)"
            )
            await db.commit()

    async def save_submission(self, sub: UGCSubmission) -> None:
        """Insert or update a submission."""
        draft_json = (
            json.dumps(
                {
                    "title": sub.draft.title,
                    "lead": sub.draft.lead,
                    "body": sub.draft.body,
                    "spam_or_inappropriate": sub.draft.spam_or_inappropriate,
                    "rejection_reason": sub.draft.rejection_reason,
                    "verification_warning": sub.draft.verification_warning,
                },
                ensure_ascii=False,
            )
            if sub.draft
            else None
        )

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO ugc_submissions (
                    id, user_id, username, first_name, raw_text,
                    media_type, media_file_ids, status, is_anonymous,
                    draft_json, admin_message_id, published_message_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    draft_json=excluded.draft_json,
                    admin_message_id=excluded.admin_message_id,
                    published_message_id=excluded.published_message_id
                """,
                (
                    sub.id,
                    sub.user_id,
                    sub.username,
                    sub.first_name,
                    sub.raw_text,
                    sub.media_type,
                    json.dumps(sub.media_file_ids),
                    sub.status.value,
                    1 if sub.is_anonymous else 0,
                    draft_json,
                    sub.admin_message_id,
                    sub.published_message_id,
                    sub.created_at.isoformat(),
                ),
            )
            await db.commit()

    async def get_submission(self, submission_id: str) -> Optional[UGCSubmission]:
        """Fetch submission by ID."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM ugc_submissions WHERE id = ?", (submission_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None

                draft = None
                if row["draft_json"]:
                    d_data = json.loads(row["draft_json"])
                    draft = UGCEditedDraft(
                        title=d_data["title"],
                        lead=d_data["lead"],
                        body=d_data["body"],
                        spam_or_inappropriate=d_data.get("spam_or_inappropriate", False),
                        rejection_reason=d_data.get("rejection_reason"),
                        verification_warning=d_data.get("verification_warning"),
                    )

                return UGCSubmission(
                    id=row["id"],
                    user_id=row["user_id"],
                    username=row["username"],
                    first_name=row["first_name"],
                    raw_text=row["raw_text"],
                    media_type=row["media_type"],
                    media_file_ids=json.loads(row["media_file_ids"]),
                    status=UGCStatus(row["status"]),
                    is_anonymous=bool(row["is_anonymous"]),
                    draft=draft,
                    admin_message_id=row["admin_message_id"],
                    published_message_id=row["published_message_id"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                )

    async def update_status(
        self,
        submission_id: str,
        status: UGCStatus,
        admin_message_id: Optional[int] = None,
        published_message_id: Optional[int] = None,
    ) -> None:
        """Update submission state."""
        async with aiosqlite.connect(self.db_path) as db:
            query = "UPDATE ugc_submissions SET status = ?"
            params: list = [status.value]
            if admin_message_id is not None:
                query += ", admin_message_id = ?"
                params.append(admin_message_id)
            if published_message_id is not None:
                query += ", published_message_id = ?"
                params.append(published_message_id)
            query += " WHERE id = ?"
            params.append(submission_id)
            await db.execute(query, tuple(params))
            await db.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/ugc/test_repository.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ugc/repository.py tests/ugc/test_repository.py
git commit -m "feat(ugc): implement persistent async SQLite repository for UGC submissions"
```

---

### Task 3: MediaGroup Buffer (Debouncer for Telegram Albums)

**Files:**
- Create: `src/ugc/buffer.py`
- Test: `tests/ugc/test_buffer.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/ugc/test_buffer.py
import asyncio
import pytest
from src.ugc.buffer import MediaGroupBuffer

@pytest.mark.asyncio
async def test_media_group_buffer_aggregation():
    flushed_items = []

    async def on_flush(media_group_id: str, text: str, file_ids: list[str], metadata: dict):
        flushed_items.append((media_group_id, text, file_ids, metadata))

    buffer = MediaGroupBuffer(flush_delay_seconds=0.05, on_flush=on_flush)

    # 3 photos sent concurrently for the same media_group_id
    await buffer.add_item(
        media_group_id="group_99",
        caption="Авария на кольце",
        file_id="photo_1",
        metadata={"user_id": 123},
    )
    await buffer.add_item(
        media_group_id="group_99",
        caption="",
        file_id="photo_2",
        metadata={"user_id": 123},
    )
    await buffer.add_item(
        media_group_id="group_99",
        caption="",
        file_id="photo_3",
        metadata={"user_id": 123},
    )

    # Wait for debouncer to flush
    await asyncio.sleep(0.1)

    assert len(flushed_items) == 1
    mg_id, caption, files, meta = flushed_items[0]
    assert mg_id == "group_99"
    assert caption == "Авария на кольце"
    assert files == ["photo_1", "photo_2", "photo_3"]
    assert meta["user_id"] == 123
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/ugc/test_buffer.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.ugc.buffer'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/ugc/buffer.py
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional


class MediaGroupBuffer:
    """
    Debouncer that collects split Telegram media_group messages into a single album event.
    """

    def __init__(
        self,
        flush_delay_seconds: float = 1.5,
        on_flush: Optional[Callable[[str, str, List[str], Dict[str, Any]], Awaitable[None]]] = None,
    ):
        self.flush_delay = flush_delay_seconds
        self.on_flush = on_flush
        self._buffers: Dict[str, Dict[str, Any]] = {}
        self._timers: Dict[str, asyncio.TimerHandle] = {}

    async def add_item(
        self,
        media_group_id: str,
        caption: str,
        file_id: str,
        metadata: Dict[str, Any],
    ) -> None:
        """Register a single media piece belonging to an album group."""
        if media_group_id not in self._buffers:
            self._buffers[media_group_id] = {
                "caption": caption or "",
                "file_ids": [file_id],
                "metadata": metadata,
            }
        else:
            entry = self._buffers[media_group_id]
            entry["file_ids"].append(file_id)
            if not entry["caption"] and caption:
                entry["caption"] = caption

        # Reset or set timer
        if media_group_id in self._timers:
            self._timers[media_group_id].cancel()

        loop = asyncio.get_running_loop()
        self._timers[media_group_id] = loop.call_later(
            self.flush_delay,
            lambda mg_id=media_group_id: asyncio.create_task(self._flush(mg_id)),
        )

    async def _flush(self, media_group_id: str) -> None:
        """Flush the aggregated media group after quiet delay."""
        data = self._buffers.pop(media_group_id, None)
        self._timers.pop(media_group_id, None)
        if not data:
            return

        if self.on_flush:
            await self.on_flush(
                media_group_id,
                data["caption"],
                data["file_ids"],
                data["metadata"],
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/ugc/test_buffer.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ugc/buffer.py tests/ugc/test_buffer.py
git commit -m "feat(ugc): add asynchronous debouncing buffer for Telegram media groups"
```

---

### Task 4: AI Literary Editor (Newsroom Rewriter)

**Files:**
- Create: `src/ugc/editor.py`
- Test: `tests/ugc/test_editor.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/ugc/test_editor.py
import json
import pytest
from unittest.mock import AsyncMock
from src.ai_providers import AIProvider
from src.ugc.editor import UGCLiteraryEditor
from src.ugc.models import UGCEditedDraft

class MockAIProvider(AIProvider):
    async def chat_completion(self, messages, model, **kwargs) -> str:
        return json.dumps({
            "title": "В нагорной части города упало дерево",
            "lead": "Во дворе дома на улице Победы упавшее дерево повредило провода.",
            "body": "По сообщениям местных жителей, на участке временно нет света. Службы уведомлены.",
            "spam_or_inappropriate": False,
            "verification_warning": None,
        }, ensure_ascii=False)

@pytest.mark.asyncio
async def test_ugc_editor_rewrites_raw_text():
    provider = MockAIProvider()
    editor = UGCLiteraryEditor(provider=provider, model="test-model")

    raw_text = "кароче на победы дерево упало на провода света нету"
    draft = await editor.edit_submission(raw_text=raw_text, has_photo=True)

    assert isinstance(draft, UGCEditedDraft)
    assert draft.title == "В нагорной части города упало дерево"
    assert "провода" in draft.lead
    assert draft.spam_or_inappropriate is False
    assert "В нагорной части города упало дерево" in draft.format_post()

@pytest.mark.asyncio
async def test_ugc_editor_detects_spam():
    class SpamMockProvider(AIProvider):
        async def chat_completion(self, messages, model, **kwargs) -> str:
            return json.dumps({
                "title": "",
                "lead": "",
                "body": "",
                "spam_or_inappropriate": True,
                "rejection_reason": "Реклама канала и спам-ссылка",
            })

    editor = UGCLiteraryEditor(provider=SpamMockProvider(), model="test-model")
    draft = await editor.edit_submission(raw_text="подписывайтесь на крипту t.me/xxx")
    assert draft.spam_or_inappropriate is True
    assert draft.rejection_reason == "Реклама канала и спам-ссылка"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/ugc/test_editor.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.ugc.editor'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/ugc/editor.py
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from src.ai_providers import AIProvider
from src.ugc.models import UGCEditedDraft

logger = logging.getLogger(__name__)

UGC_EDITOR_SYSTEM_PROMPT = """Ты — профессиональный литературный редактор и фактчекер региональной городской редакции (Telebrief).
Твоя задача — принять произвольное, сумбурное или разговорное сообщение от местного жителя (очевидца) и переработать его в качественную, спокойную, объективную новостную заметку для городского Telegram-канала.

ПРАВИЛА И СТАНДАРТЫ:
1. СТИЛЬ И ТОН:
   - Спокойный, объективный, уважительный тон регионального ньюзрума.
   - Никакой паники, истерики, кликбейта, ругательств и базарного сленга.
   - Никаких политических лозунгов, оскорблений или эмоциональных оценок.

2. СТРОГИЙ ГРАНИЦЫ ФАКТОВ (EVIDENCE BOUNDARY):
   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО выдумывать факты, которых не было в сообщении: не придумывай причины («из-за халатности»), не придумывай неприбывшие службы, не выдумывай фамилии или цифры.
   - Обязательно сохраняй добросовестную атрибуцию очевидцев: «По сообщениям жителей», «По словам горожан», «Очевидцы сообщают».
   - Сохраняй точные ориентиры: улицы, номера домов, микрорайоны (АКЗ, Нагорная часть/Гора, Центр, Слободка, Колония и т.д.).

3. ФИЛЬТРАЦИЯ (СПАМ / НЕДОПУСТИМЫЙ КОНТЕНТ):
   - Если текст является спамом, рекламой товаров/услуг, сбором денег на карты, призывом к насилию или бессмысленным набором слов — установи spam_or_inappropriate = true и укажи rejection_reason.

ВЕРНИ ОТВЕТ СТРОГО В ФОРМАТЕ JSON:
{
  "title": "Четкий информативный заголовок (без точки на конце)",
  "lead": "Первое предложение-лид: кто/что/где произошло.",
  "body": "1-2 коротких абзаца подробностей с честной атрибуцией очевидцев.",
  "spam_or_inappropriate": false,
  "rejection_reason": null,
  "verification_warning": "Если есть сомнения или противоречия в сообщении"
}
"""


class UGCLiteraryEditor:
    """Transforms raw resident messages into well-crafted journalistic news briefs."""

    def __init__(self, provider: AIProvider, model: str = "minimax/minimax-m3:free:floor"):
        self.provider = provider
        self.model = model

    async def edit_submission(
        self,
        raw_text: str,
        has_photo: bool = False,
        photo_caption: Optional[str] = None,
    ) -> UGCEditedDraft:
        """Process user text through literary editor."""
        user_content = f"Сообщение от жителя:\n\"\"\"\n{raw_text}\n\"\"\""
        if has_photo:
            user_content += "\n(К сообщению прикреплено фото/видео от очевидца)."
        if photo_caption and photo_caption != raw_text:
            user_content += f"\nПодпись к фото: {photo_caption}"

        messages = [
            {"role": "system", "content": UGC_EDITOR_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

        try:
            response_text = await self.provider.chat_completion(
                messages=messages,
                model=self.model,
                temperature=0.2,
                max_tokens=1500,
            )
            # Clean possible markdown block markers
            cleaned = response_text.strip()
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```(?:json)?\n?", "", cleaned)
                cleaned = re.sub(r"\n?```$", "", cleaned)

            data = json.loads(cleaned)
            return UGCEditedDraft(
                title=data.get("title", "Сообщение от жителей").strip(),
                lead=data.get("lead", "").strip(),
                body=data.get("body", "").strip(),
                spam_or_inappropriate=bool(data.get("spam_or_inappropriate", False)),
                rejection_reason=data.get("rejection_reason"),
                verification_warning=data.get("verification_warning"),
            )
        except Exception as e:
            logger.error(f"UGC literary editor failed: {e}", exc_info=True)
            # Deterministic fallback draft to prevent loss of useful news
            clean_first = raw_text.strip().split("\n")[0][:80]
            return UGCEditedDraft(
                title=f"Сообщение от жителей: {clean_first}",
                lead="От горожан поступило сообщение о происшествии.",
                body=f"По сообщениям жителей: {raw_text}",
                verification_warning="Автоматическая редакция недоступна, сформирован базовый черновик.",
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/ugc/test_editor.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ugc/editor.py tests/ugc/test_editor.py
git commit -m "feat(ugc): implement AI literary editor with strict evidence boundary and JSON formatting"
```

---

### Task 5: Moderation Service & Admin Review Cards

**Files:**
- Create: `src/ugc/moderation.py`
- Test: `tests/ugc/test_moderation.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/ugc/test_moderation.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from telegram import InlineKeyboardMarkup
from src.ugc.models import UGCSubmission, UGCEditedDraft, UGCStatus
from src.ugc.moderation import UGCModerationService

def test_moderation_card_formatting():
    draft = UGCEditedDraft(
        title="На Восточном порыв водопровода",
        lead="В районе рынка произошла утечка воды.",
        body="По словам очевидцев, вода течет уже более часа.",
    )
    sub = UGCSubmission(
        id="sub-456",
        user_id=789,
        username="john_doe",
        first_name="Иван",
        raw_text="возле рынка трубу прорвало капец",
        draft=draft,
    )
    service = UGCModerationService(bot=MagicMock(), repo=MagicMock(), target_channel_id="@city")
    text, keyboard = service.build_admin_card(sub)

    assert "sub-456" in text
    assert "@john_doe" in text
    assert "На Восточном порыв водопровода" in text
    assert "возле рынка трубу прорвало капец" in text
    assert isinstance(keyboard, InlineKeyboardMarkup)
    # Check callback buttons exist
    button_callbacks = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
    assert "ugc_pub:sub-456" in button_callbacks
    assert "ugc_rej:sub-456" in button_callbacks
    assert "ugc_ask:sub-456" in button_callbacks
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/ugc/test_moderation.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.ugc.moderation'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/ugc/moderation.py
from __future__ import annotations

import logging
from typing import Optional, Tuple, Union

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
from telegram.constants import ParseMode

from src.ugc.models import UGCStatus, UGCSubmission
from src.ugc.repository import UGCRepository

logger = logging.getLogger(__name__)


class UGCModerationService:
    """Builds moderation cards and publishes approved submissions to Telegram."""

    def __init__(
        self,
        bot: Bot,
        repo: UGCRepository,
        target_channel_id: Union[int, str],
    ):
        self.bot = bot
        self.repo = repo
        self.target_channel_id = target_channel_id

    def build_admin_card(self, sub: UGCSubmission) -> Tuple[str, InlineKeyboardMarkup]:
        """Format the moderation review message with action buttons."""
        author = f"@{sub.username}" if sub.username else f"{sub.first_name} (ID: {sub.user_id})"
        lines = [
            f"📥 *НОВАЯ ПРЕДЛОЖКА* `[{sub.id}]`",
            f"👤 *Автор:* {author}",
            "",
            "📝 *Предложенный пост (AI-редактор):*",
            "━━━━━━━━━━━━━━━━━━━━━━",
            sub.draft.format_post() if sub.draft else "(Черновик отсутствует)",
            "━━━━━━━━━━━━━━━━━━━━━━",
            "",
            f"🔍 *Исходный текст:* _{sub.raw_text}_",
        ]
        if sub.draft and sub.draft.verification_warning:
            lines.extend(["", f"⚠️ *Внимание:* {sub.draft.verification_warning}"])

        text = "\n".join(lines)

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🚀 Опубликовать", callback_data=f"ugc_pub:{sub.id}"),
                    InlineKeyboardButton("💬 Уточнить", callback_data=f"ugc_ask:{sub.id}"),
                ],
                [
                    InlineKeyboardButton("🔄 Переписать", callback_data=f"ugc_rew:{sub.id}"),
                    InlineKeyboardButton("❌ Отклонить", callback_data=f"ugc_rej:{sub.id}"),
                ],
            ]
        )
        return text, keyboard

    async def send_to_moderation(self, admin_chat_id: Union[int, str], sub: UGCSubmission) -> int:
        """Send submission card to admin moderation chat."""
        text, keyboard = self.build_admin_card(sub)

        # If has photos, send first photo or album preview
        if sub.media_file_ids:
            if len(sub.media_file_ids) == 1:
                msg = await self.bot.send_photo(
                    chat_id=admin_chat_id,
                    photo=sub.media_file_ids[0],
                    caption=text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=keyboard,
                )
            else:
                # Send media group first, then control card
                media = [InputMediaPhoto(fid) for fid in sub.media_file_ids[:10]]
                await self.bot.send_media_group(chat_id=admin_chat_id, media=media)
                msg = await self.bot.send_message(
                    chat_id=admin_chat_id,
                    text=text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=keyboard,
                )
        else:
            msg = await self.bot.send_message(
                chat_id=admin_chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard,
            )

        await self.repo.update_status(
            submission_id=sub.id,
            status=UGCStatus.PENDING_REVIEW,
            admin_message_id=msg.message_id,
        )
        return msg.message_id

    async def publish_to_channel(self, sub: UGCSubmission) -> int:
        """Publish approved news item to the public channel."""
        if not sub.draft:
            raise ValueError(f"Submission {sub.id} has no draft to publish")

        post_text = sub.draft.format_post()

        if sub.media_file_ids:
            if len(sub.media_file_ids) == 1:
                pub_msg = await self.bot.send_photo(
                    chat_id=self.target_channel_id,
                    photo=sub.media_file_ids[0],
                    caption=post_text,
                    parse_mode=ParseMode.MARKDOWN,
                )
            else:
                media = [
                    InputMediaPhoto(
                        media=fid,
                        caption=post_text if idx == 0 else "",
                        parse_mode=ParseMode.MARKDOWN if idx == 0 else None,
                    )
                    for idx, fid in enumerate(sub.media_file_ids[:10])
                ]
                msgs = await self.bot.send_media_group(chat_id=self.target_channel_id, media=media)
                pub_msg = msgs[0]
        else:
            pub_msg = await self.bot.send_message(
                chat_id=self.target_channel_id,
                text=post_text,
                parse_mode=ParseMode.MARKDOWN,
            )

        await self.repo.update_status(
            submission_id=sub.id,
            status=UGCStatus.PUBLISHED,
            published_message_id=pub_msg.message_id,
        )
        return pub_msg.message_id
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/ugc/test_moderation.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ugc/moderation.py tests/ugc/test_moderation.py
git commit -m "feat(ugc): implement admin moderation review card and channel publisher"
```

---

### Task 6: Telegram Bot Handlers & Clarification Flow

**Files:**
- Create: `src/ugc/bot.py`
- Test: `tests/ugc/test_bot.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/ugc/test_bot.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from telegram import Update, User, Message, Chat
from src.ugc.bot import UGCBot
from src.ugc.config import UGCConfig

@pytest.mark.asyncio
async def test_bot_start_command():
    config = UGCConfig(
        bot_token="test_token",
        admin_chat_id=-1001,
        target_channel_id="@test_channel",
    )
    bot = UGCBot(config=config, editor=MagicMock(), repo=MagicMock(), moderation=MagicMock())
    
    update = MagicMock(spec=Update)
    user = MagicMock(spec=User, id=123, first_name="Иван")
    chat = MagicMock(spec=Chat, id=123, type="private")
    message = MagicMock(spec=Message, message_id=1, from_user=user, chat=chat)
    message.reply_text = AsyncMock()
    update.effective_user = user
    update.effective_chat = chat
    update.message = message

    await bot.handle_start(update, None)
    message.reply_text.assert_called_once()
    assert "предложить новость" in message.reply_text.call_args[0][0].lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/ugc/test_bot.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.ugc.bot'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/ugc/bot.py
from __future__ import annotations

import logging
import uuid
from typing import Dict, Optional

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.ugc.buffer import MediaGroupBuffer
from src.ugc.config import UGCConfig
from src.ugc.editor import UGCLiteraryEditor
from src.ugc.models import UGCStatus, UGCSubmission
from src.ugc.moderation import UGCModerationService
from src.ugc.repository import UGCRepository

logger = logging.getLogger(__name__)


class UGCBot:
    """Telegram bot application for crowdsourced news submissions."""

    def __init__(
        self,
        config: UGCConfig,
        editor: UGCLiteraryEditor,
        repo: UGCRepository,
        moderation: UGCModerationService,
    ):
        self.config = config
        self.editor = editor
        self.repo = repo
        self.moderation = moderation
        self.app: Optional[Application] = None
        self._media_buffer = MediaGroupBuffer(
            flush_delay_seconds=1.5,
            on_flush=self._handle_media_group_flush,
        )
        # Active clarification sessions: user_id -> submission_id
        self._awaiting_clarification: Dict[int, str] = {}

    def setup_application(self) -> Application:
        """Register Telegram handlers."""
        self.app = Application.builder().token(self.config.bot_token).build()

        self.app.add_handler(CommandHandler("start", self.handle_start))
        self.app.add_handler(CommandHandler("help", self.handle_help))

        # Media handlers
        self.app.add_handler(
            MessageHandler(filters.PHOTO | filters.VIDEO, self.handle_media_message)
        )
        # Text messages
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text_message)
        )
        # Inline button callbacks
        self.app.add_handler(CallbackQueryHandler(self.handle_callback))

        return self.app

    async def handle_start(self, update: Update, context: Optional[ContextTypes.DEFAULT_TYPE]) -> None:
        """Greet user and explain submission rules."""
        if not update.message:
            return
        welcome_text = (
            "👋 *Добро пожаловать в предложку городских новостей!*\n\n"
            "Здесь вы можете прислать новость, наблюдение или фото происшествия в городе.\n\n"
            "📌 *Как это работает:*\n"
            "1. Отправьте текст, фото или видео прямо в этот чат.\n"
            "2. Наш редактор оформит заметку в новостном формате.\n"
            "3. После проверки редактором новость выйдет в официальном канале.\n\n"
            "🔒 *Анонимность:* по умолчанию ваше имя в канале не публикуется."
        )
        await update.message.reply_text(welcome_text, parse_mode="Markdown")

    async def handle_help(self, update: Update, context: Optional[ContextTypes.DEFAULT_TYPE]) -> None:
        """Provide brief guidelines."""
        if not update.message:
            return
        help_text = (
            "ℹ️ *Памятка очевидца:*\n"
            "• Указывайте точный адрес, улицу или ориентир.\n"
            "• Прикрепляйте реальные фото или видео с места событий.\n"
            "• Не присылайте рекламу, коммерческие объявления и чужие ссылки."
        )
        await update.message.reply_text(help_text, parse_mode="Markdown")

    async def handle_text_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle standalone text reports or clarification replies."""
        msg = update.message
        if not msg or not msg.text or not update.effective_user:
            return

        user = update.effective_user

        # Check if user is replying to an editorial clarification question
        if user.id in self._awaiting_clarification:
            sub_id = self._awaiting_clarification.pop(user.id)
            sub = await self.repo.get_submission(sub_id)
            if sub:
                sub.raw_text += f"\n[Уточнение от автора]: {msg.text}"
                draft = await self.editor.edit_submission(sub.raw_text, has_photo=bool(sub.media_file_ids))
                sub.draft = draft
                await self.repo.save_submission(sub)
                await self.moderation.send_to_moderation(self.config.admin_chat_id, sub)
                await msg.reply_text("✅ Спасибо за уточнение! Дополнили новость и передали редактору.")
                return

        sub_id = str(uuid.uuid4())[:8]
        sub = UGCSubmission(
            id=sub_id,
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            raw_text=msg.text,
            media_type="none",
        )

        await msg.reply_text("⏳ Спасибо! Ваш репортаж принят и передан редактору...")
        draft = await self.editor.edit_submission(raw_text=msg.text, has_photo=False)
        sub.draft = draft

        await self.repo.save_submission(sub)
        await self.moderation.send_to_moderation(self.config.admin_chat_id, sub)

    async def handle_media_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle photo or video message (with or without media group)."""
        msg = update.message
        if not msg or not update.effective_user:
            return

        file_id = msg.photo[-1].file_id if msg.photo else (msg.video.file_id if msg.video else "")
        caption = msg.caption or ""

        if msg.media_group_id:
            await self._media_buffer.add_item(
                media_group_id=msg.media_group_id,
                caption=caption,
                file_id=file_id,
                metadata={
                    "user_id": update.effective_user.id,
                    "username": update.effective_user.username,
                    "first_name": update.effective_user.first_name,
                },
            )
            return

        # Single media item
        sub_id = str(uuid.uuid4())[:8]
        sub = UGCSubmission(
            id=sub_id,
            user_id=update.effective_user.id,
            username=update.effective_user.username,
            first_name=update.effective_user.first_name,
            raw_text=caption or "Фотография от очевидца с места событий",
            media_type="photo" if msg.photo else "video",
            media_file_ids=[file_id],
        )

        await msg.reply_text("⏳ Спасибо за фото! Редактор обрабатывает сообщение...")
        draft = await self.editor.edit_submission(raw_text=sub.raw_text, has_photo=True)
        sub.draft = draft

        await self.repo.save_submission(sub)
        await self.moderation.send_to_moderation(self.config.admin_chat_id, sub)

    async def _handle_media_group_flush(
        self, media_group_id: str, caption: str, file_ids: list[str], metadata: dict
    ) -> None:
        """Called when album finishes debouncing."""
        sub_id = str(uuid.uuid4())[:8]
        raw_text = caption or "Серия фотографий от очевидца с места событий"
        sub = UGCSubmission(
            id=sub_id,
            user_id=metadata["user_id"],
            username=metadata.get("username"),
            first_name=metadata.get("first_name", "Очевидец"),
            raw_text=raw_text,
            media_type="album",
            media_file_ids=file_ids,
        )

        draft = await self.editor.edit_submission(raw_text=raw_text, has_photo=True)
        sub.draft = draft

        await self.repo.save_submission(sub)
        await self.moderation.send_to_moderation(self.config.admin_chat_id, sub)

    async def handle_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle inline button actions from the moderation chat."""
        query = update.callback_query
        if not query or not query.data:
            return

        action, _, sub_id = query.data.partition(":")
        sub = await self.repo.get_submission(sub_id)
        if not sub:
            await query.answer("❌ Сообщение не найдено!", show_alert=True)
            return

        if action == "ugc_pub":
            await query.answer("Публикую в канал...")
            await self.moderation.publish_to_channel(sub)
            await query.edit_message_reply_markup(reply_markup=None)
            if query.message:
                await query.message.reply_text(f"✅ Новость `[{sub.id}]` успешно опубликована в канале!")
            # Notify author
            try:
                if self.app:
                    await self.app.bot.send_message(
                        chat_id=sub.user_id,
                        text="🎉 Ваша новость опубликована в канале! Благодарим за участие в жизни города.",
                    )
            except Exception as e:
                logger.warning(f"Failed to notify author {sub.user_id}: {e}")

        elif action == "ugc_rej":
            await query.answer("Отклонено")
            await self.repo.update_status(sub.id, UGCStatus.REJECTED)
            await query.edit_message_reply_markup(reply_markup=None)
            if query.message:
                await query.message.reply_text(f"❌ Новость `[{sub.id}]` отклонена редактором.")

        elif action == "ugc_ask":
            await query.answer("Запрос автору")
            self._awaiting_clarification[sub.user_id] = sub.id
            await self.repo.update_status(sub.id, UGCStatus.AWAITING_CLARIFICATION)
            try:
                if self.app:
                    await self.app.bot.send_message(
                        chat_id=sub.user_id,
                        text=(
                            "💬 Редакция просит уточнить детали по вашей новости:\n"
                            "«Подскажите, пожалуйста, точный адрес или ориентир происшествия?»\n\n"
                            "Напишите ответ прямо в этот чат."
                        ),
                    )
                if query.message:
                    await query.message.reply_text(
                        f"💬 Автору новости `[{sub.id}]` отправлен запрос на уточнение адреса."
                    )
            except Exception as e:
                logger.warning(f"Could not contact user {sub.user_id}: {e}")
                if query.message:
                    await query.message.reply_text(f"⚠️ Не удалось связаться с пользователем: {e}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/ugc/test_bot.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ugc/bot.py tests/ugc/test_bot.py
git commit -m "feat(ugc): implement Telegram bot handlers, album processing, and clarification routing"
```

---

### Task 7: Telebrief Core Ingestion Bridge & Standalone Runner

**Files:**
- Create: `src/ugc/runner.py`
- Test: `tests/ugc/test_runner.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/ugc/test_runner.py
import pytest
from unittest.mock import AsyncMock, patch
from src.ugc.runner import bridge_published_item_to_telebrief
from src.ugc.models import UGCSubmission, UGCEditedDraft

@pytest.mark.asyncio
async def test_bridge_published_item_to_telebrief():
    sub = UGCSubmission(
        id="sub-777",
        user_id=123,
        username="city_resident",
        first_name="Ольга",
        raw_text="На Нагорной выключили воду",
        draft=UGCEditedDraft(
            title="Перебои с водоснабжением в нагорной части",
            lead="Жители сообщают об отключении воды.",
            body="В нагорной части города отсутствует водоснабжение.",
        ),
        published_message_id=555,
    )
    mock_storage = AsyncMock()
    await bridge_published_item_to_telebrief(sub, storage=mock_storage, channel_name="Бердянск Предложка")
    mock_storage.save_messages.assert_called_once()
    saved = mock_storage.save_messages.call_args[0][0][0]
    assert saved.sender == "ugc_bot"
    assert "Перебои с водоснабжением" in saved.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/ugc/test_runner.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.ugc.runner'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/ugc/runner.py
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from src.ai_providers import AIProvider, OpenAIProvider
from src.collector import Message
from src.config_loader import Config
from src.storage import MessageStorage
from src.ugc.bot import UGCBot
from src.ugc.config import UGCConfig
from src.ugc.editor import UGCLiteraryEditor
from src.ugc.models import UGCSubmission
from src.ugc.moderation import UGCModerationService
from src.ugc.repository import UGCRepository

logger = logging.getLogger(__name__)


async def bridge_published_item_to_telebrief(
    sub: UGCSubmission,
    storage: MessageStorage,
    channel_name: str = "Предложка новостей",
) -> None:
    """Save an approved UGC news item into Telebrief's message storage for daily digest inclusion."""
    if not sub.draft:
        return

    full_text = sub.draft.format_post()
    message = Message(
        channel_name=channel_name,
        sender="ugc_bot",
        text=full_text,
        timestamp=datetime.now(timezone.utc),
        link=f"https://t.me/c/{sub.published_message_id}" if sub.published_message_id else "",
        has_media=bool(sub.media_file_ids),
        media_type=sub.media_type,
    )
    await storage.save_messages([message])
    logger.info(f"Bridged UGC submission [{sub.id}] into Telebrief storage")


async def run_ugc_bot_service(
    ugc_config: UGCConfig,
    ai_provider: AIProvider,
    storage: Optional[MessageStorage] = None,
) -> None:
    """Initialize and run UGC Telegram bot service."""
    repo = UGCRepository(db_path=ugc_config.db_path)
    await repo.init_db()

    editor = UGCLiteraryEditor(provider=ai_provider, model=ugc_config.model)

    # Temporary bot instance for moderation service
    from telegram import Bot
    telegram_bot = Bot(token=ugc_config.bot_token)
    moderation = UGCModerationService(
        bot=telegram_bot,
        repo=repo,
        target_channel_id=ugc_config.target_channel_id,
    )

    bot_app = UGCBot(
        config=ugc_config,
        editor=editor,
        repo=repo,
        moderation=moderation,
    )
    application = bot_app.setup_application()

    logger.info("🚀 Starting UGC News Bot service...")
    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    # Keep alive
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/ugc/test_runner.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ugc/runner.py tests/ugc/test_runner.py
git commit -m "feat(ugc): implement bridge to Telebrief storage and standalone UGC bot service runner"
```

---

### Task 8: Full End-to-End Suite Verification

**Files:**
- Test: `tests/ugc/`

- [ ] **Step 1: Run complete UGC test suite**

Run: `uv run pytest tests/ugc/ -v`
Expected: All tests PASS

- [ ] **Step 2: Run linter and formatting check**

Run: `uv run ruff check src/ugc tests/ugc`
Expected: 0 errors

- [ ] **Step 3: Final integration commit**

```bash
git commit --allow-empty -m "chore(ugc): complete test suite and code quality verification for UGC bot"
```
