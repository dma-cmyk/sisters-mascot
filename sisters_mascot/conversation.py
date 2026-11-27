"""Pure-Python conversation continuity, cross-agent handoff, compaction, and persistence."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from sisters_mascot import CONFIG_DIR

COMPACT_WARN_CHARS: int = 12_000
COMPACT_HIGH_CHARS: int = 24_000
COMPACT_WARN_TURNS: int = 14
COMPACT_HIGH_TURNS: int = 24
DEFAULT_AUTO_COMPACT_CHARS: int = 18_000
MAX_HANDOFF_CHARS: int = 12_000
MAX_SINGLE_TURN_CHARS: int = 2_400
MAX_COMPACT_INPUT_CHARS: int = 40_000
MAX_LOCAL_SUMMARY_CHARS: int = 2_000
MAX_SAVED_DISPLAY_MESSAGES: int = 80
CONVERSATION_FILE: Path = CONFIG_DIR / "conversation.json"

COMPACT_SYSTEM_PROMPT: str = (
    "あなたは会話コンテキストを整理・圧縮する要約アシスタントです。\n"
    "- ツール呼び出し、ファイル編集、コマンド実行、Webアクセスは一切行わないでください。\n"
    "- 前置きや挨拶は省き、引き継ぎ用の構造化Markdown要約のみを日本語で簡潔に出力してください。"
)


def _clip_text(text: str, max_chars: int) -> str:
    """Truncate long text while preserving both opening context and conclusion."""
    clean = (text or "").strip()
    if len(clean) <= max_chars:
        return clean
    if max_chars <= 30:
        return clean[: max(1, max_chars - 1)] + "…"
    head = max_chars // 2 - 8
    tail = max_chars - head - 12
    return clean[:head].rstrip() + "\n…（中略）…\n" + clean[-tail:].lstrip()


def format_char_count(chars: int) -> str:
    """Format character count concisely for the context status bar."""
    val = max(0, int(chars))
    if val < 1000:
        return f"{val}文字"
    return f"約{val / 1000:.1f}k文字"


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    """Single user or sister message stored in the shared conversation log."""

    role: str  # "user" | "sister"
    author: str
    text: str
    sister_id: str = ""
    agent_id: str = ""
    attachments: tuple[str, ...] = ()
    time: str = ""

    def char_length(self) -> int:
        return len(self.text) + sum(len(a) + 4 for a in self.attachments)

    def format_line(self, max_chars: int = MAX_SINGLE_TURN_CHARS) -> str:
        """Render a single turn cleanly for cross-agent handoff or compaction."""
        if self.role == "user":
            speaker = self.author.strip() or "美緒ちゃん"
        else:
            name = self.author.strip() or "シスター"
            speaker = f"{name} ({self.agent_id})" if self.agent_id else name

        parts: list[str] = []
        if self.attachments:
            parts.append(f"[添付: {', '.join(self.attachments)}]")
        if self.text.strip():
            parts.append(_clip_text(self.text, max_chars))
        body = "\n".join(parts) if parts else "（空メッセージ）"
        return f"[{speaker}] {body}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "author": self.author,
            "text": self.text,
            "sister_id": self.sister_id,
            "agent_id": self.agent_id,
            "attachments": list(self.attachments),
            "time": self.time,
        }

    @classmethod
    def from_dict(cls, data: Any) -> ConversationTurn | None:
        if not isinstance(data, dict):
            return None
        role = str(data.get("role") or "").strip()
        if role not in ("user", "sister"):
            return None
        raw_att = data.get("attachments")
        attachments = (
            tuple(str(a) for a in raw_att if str(a).strip())
            if isinstance(raw_att, list)
            else ()
        )
        return cls(
            role=role,
            author=str(data.get("author") or ("美緒ちゃん" if role == "user" else "シスター")),
            text=str(data.get("text") or ""),
            sister_id=str(data.get("sister_id") or ""),
            agent_id=str(data.get("agent_id") or ""),
            attachments=attachments,
            time=str(data.get("time") or ""),
        )


class ConversationState:
    """Tracks shared conversation history, per-session sync offsets, and compaction."""

    def __init__(
        self,
        *,
        turns: Sequence[ConversationTurn] = (),
        compacted_summary: str = "",
        summary_generation: int = 0,
        session_sync: dict[tuple[str, str], tuple[int, int]] | None = None,
        compacted_count: int = 0,
    ) -> None:
        self.turns: list[ConversationTurn] = list(turns)
        self.compacted_summary: str = (compacted_summary or "").strip()
        self.summary_generation: int = max(0, int(summary_generation))
        self.session_sync: dict[tuple[str, str], tuple[int, int]] = dict(session_sync or {})
        self.compacted_count: int = max(0, int(compacted_count))

    @property
    def active_chars(self) -> int:
        return len(self.compacted_summary) + sum(t.char_length() for t in self.turns)

    @property
    def active_turns(self) -> int:
        return len(self.turns)

    @property
    def can_compact(self) -> bool:
        return len(self.turns) >= 2 or (len(self.turns) >= 1 and self.active_chars >= 600)

    def context_level(
        self,
        warn_chars: int = COMPACT_WARN_CHARS,
        high_chars: int = COMPACT_HIGH_CHARS,
    ) -> str:
        """Return 'ok', 'warn', or 'high' based on current context volume."""
        chars = self.active_chars
        turns = len(self.turns)
        if chars >= high_chars or turns >= COMPACT_HIGH_TURNS:
            return "high"
        if chars >= warn_chars or turns >= COMPACT_WARN_TURNS:
            return "warn"
        return "ok"

    def format_char_count(self, chars: int | None = None) -> str:
        return format_char_count(self.active_chars if chars is None else chars)

    def add_user_turn(
        self,
        text: str,
        *,
        attachments: Sequence[str] = (),
        time_str: str = "",
    ) -> ConversationTurn:
        turn = ConversationTurn(
            role="user",
            author="美緒ちゃん",
            text=(text or "").strip(),
            attachments=tuple(str(a) for a in attachments if str(a).strip()),
            time=time_str,
        )
        self.turns.append(turn)
        return turn

    def add_sister_turn(
        self,
        text: str,
        *,
        author: str,
        sister_id: str,
        agent_id: str,
        time_str: str = "",
    ) -> ConversationTurn:
        turn = ConversationTurn(
            role="sister",
            author=(author or "シスター").strip(),
            text=(text or "").strip(),
            sister_id=sister_id,
            agent_id=agent_id,
            time=time_str,
        )
        self.turns.append(turn)
        return turn

    def mark_session_synced(
        self,
        agent_id: str,
        sister_id: str,
        *,
        summary_gen: int | None = None,
        turn_count: int | None = None,
    ) -> None:
        if summary_gen is not None and summary_gen != self.summary_generation:
            return
        count = len(self.turns) if turn_count is None else max(0, min(len(self.turns), turn_count))
        self.session_sync[(agent_id, sister_id)] = (self.summary_generation, count)

    def clear_session_sync(self, agent_id: str, sister_id: str) -> None:
        self.session_sync.pop((agent_id, sister_id), None)

    def apply_compaction(self, summary: str) -> tuple[int, int]:
        """Replace active turns with a compacted summary and reset session sync state."""
        before_chars = self.active_chars
        self.compacted_summary = (summary or "").strip()
        self.summary_generation += 1
        self.compacted_count += 1
        self.turns.clear()
        self.session_sync.clear()
        return before_chars, self.active_chars

    def reset(self) -> None:
        """Reset conversation history, summary, and sync offsets for a new chat."""
        self.turns.clear()
        self.compacted_summary = ""
        self.session_sync.clear()
        self.compacted_count = 0
        self.summary_generation += 1

    def _pack_recent_turns(
        self,
        turns: Sequence[ConversationTurn],
        budget_chars: int,
    ) -> tuple[list[str], int]:
        """Pack as many newest formatted turns as fit within `budget_chars`."""
        selected: list[str] = []
        used = 0
        for turn in reversed(turns):
            line = turn.format_line()
            cost = len(line) + 2
            if selected and used + cost > budget_chars:
                break
            if not selected and cost > budget_chars:
                line = _clip_text(line, max(200, budget_chars))
            selected.append(line)
            used += len(line) + 2
        selected.reverse()
        omitted = max(0, len(turns) - len(selected))
        return selected, omitted

    def build_handoff_context(
        self,
        agent_id: str,
        sister_id: str,
        *,
        has_session: bool,
    ) -> str:
        """Build context handoff block for switching sisters/agents or resuming safely."""
        if has_session:
            sync = self.session_sync.get((agent_id, sister_id))
            if sync is not None and sync[0] == self.summary_generation:
                synced_idx = min(sync[1], len(self.turns))
                if synced_idx >= len(self.turns):
                    return ""
                delta_turns = self.turns[synced_idx:]
                packed, omitted = self._pack_recent_turns(delta_turns, MAX_HANDOFF_CHARS)
                sections = ["【直前の会話の続き（他の姉妹・AIとのやり取りの共有）】"]
                if omitted:
                    sections.append(f"（途中の履歴 {omitted}件は長さ制限のため省略）")
                sections.extend(packed)
                body = "\n".join(sections)
                return f"<conversation_handoff>\n{body}\n</conversation_handoff>"

        if not self.compacted_summary and not self.turns:
            return ""

        sections = []
        remaining = MAX_HANDOFF_CHARS
        if self.compacted_summary:
            summary_block = (
                "【これまでの会話の要約（コンパクト済み）】\n"
                + _clip_text(self.compacted_summary, MAX_HANDOFF_CHARS // 2)
            )
            sections.append(summary_block)
            remaining = max(1_200, MAX_HANDOFF_CHARS - len(summary_block) - 64)

        if self.turns:
            packed, omitted = self._pack_recent_turns(self.turns, remaining)
            sections.append("【直近の会話履歴】")
            if omitted:
                sections.append(f"（古い履歴 {omitted}件は省略）")
            sections.extend(packed)

        body = "\n".join(sections)
        return f"<conversation_handoff>\n{body}\n</conversation_handoff>"

    def build_compaction_prompt(self) -> str:
        """Build an AI prompt that compresses the conversation into a compact handoff summary."""
        parts: list[str] = [
            "以下の美緒ちゃんとのこれまでの会話履歴を、次のAIエージェントや他の姉妹へ引き継ぐためにコンパクトに要約してください。",
            "",
            "出力フォーマット（500〜1200文字目安・余計な挨拶不要）:",
            "### 🎯 目的・進行中のタスク",
            "### ✅ 決定事項・完了事項",
            "### 📁 ファイルパス・コード・URL・重要情報",
            "### 🔜 未解決・次のステップ",
            "",
        ]
        budget = MAX_COMPACT_INPUT_CHARS
        if self.compacted_summary:
            prev = _clip_text(self.compacted_summary, 8_000)
            parts.append("【以前の要約】")
            parts.append(prev)
            parts.append("")
            budget = max(8_000, budget - len(prev) - 200)

        if self.turns:
            packed, omitted = self._pack_recent_turns(self.turns, budget)
            parts.append("【今回圧縮する会話履歴】")
            if omitted:
                parts.append(f"（先頭 {omitted}件は省略）")
            parts.extend(packed)

        return "\n".join(parts)

    def build_local_fallback_summary(self) -> str:
        """Create an instant deterministic summary without invoking an AI CLI."""
        bullets: list[str] = ["### 🗜️ 会話引き継ぎサマリ（ローカル要約）"]
        if self.compacted_summary:
            bullets.append(
                "**これまでの要約（抜粋）**:\n"
                + _clip_text(self.compacted_summary, 700)
            )

        user_items: list[str] = []
        sister_items: list[str] = []
        for turn in self.turns[-16:]:
            snippet = " ".join(turn.text.split())
            if turn.role == "user":
                att = f" [添付: {', '.join(turn.attachments)}]" if turn.attachments else ""
                line = f"- {turn.author}{att}: {_clip_text(snippet or '（添付のみ）', 140)}"
                user_items.append(line)
            else:
                tag = f"{turn.author} ({turn.agent_id})" if turn.agent_id else turn.author
                line = f"- {tag}: {_clip_text(snippet, 160)}"
                sister_items.append(line)

        if user_items:
            bullets.append("**美緒ちゃんのリクエスト履歴**:\n" + "\n".join(user_items[-8:]))
        if sister_items:
            bullets.append("**主な回答・作業メモ**:\n" + "\n".join(sister_items[-8:]))

        return _clip_text("\n\n".join(bullets), MAX_LOCAL_SUMMARY_CHARS)

    def to_dict(self) -> dict[str, Any]:
        sync_serialized: dict[str, list[int]] = {}
        for (agent_id, sister_id), (gen, idx) in self.session_sync.items():
            sync_serialized[f"{agent_id}:{sister_id}"] = [gen, idx]
        return {
            "turns": [t.to_dict() for t in self.turns],
            "compacted_summary": self.compacted_summary,
            "summary_generation": self.summary_generation,
            "session_sync": sync_serialized,
            "compacted_count": self.compacted_count,
        }

    @classmethod
    def from_dict(cls, data: Any) -> ConversationState:
        if not isinstance(data, dict):
            return cls()
        turns: list[ConversationTurn] = []
        raw_turns = data.get("turns")
        if isinstance(raw_turns, list):
            for entry in raw_turns:
                parsed = ConversationTurn.from_dict(entry)
                if parsed is not None:
                    turns.append(parsed)

        sync: dict[tuple[str, str], tuple[int, int]] = {}
        raw_sync = data.get("session_sync")
        if isinstance(raw_sync, dict):
            for key, val in raw_sync.items():
                if not isinstance(key, str) or ":" not in key:
                    continue
                if not isinstance(val, (list, tuple)) or len(val) != 2:
                    continue
                agent_id, sister_id = key.split(":", 1)
                try:
                    gen = int(val[0])
                    idx = max(0, min(len(turns), int(val[1])))
                except (TypeError, ValueError):
                    continue
                sync[(agent_id, sister_id)] = (gen, idx)

        try:
            summary_gen = int(data.get("summary_generation", 0))
        except (TypeError, ValueError):
            summary_gen = 0
        try:
            compacted_cnt = int(data.get("compacted_count", 0))
        except (TypeError, ValueError):
            compacted_cnt = 0

        return cls(
            turns=turns,
            compacted_summary=str(data.get("compacted_summary") or ""),
            summary_generation=summary_gen,
            session_sync=sync,
            compacted_count=compacted_cnt,
        )


def _conversation_path(path: Path | None) -> Path:
    return Path(path) if path is not None else CONVERSATION_FILE


def save_conversation(
    state: ConversationState,
    *,
    display_messages: Sequence[dict[str, Any]] = (),
    sessions: dict[str, str] | None = None,
    path: Path | None = None,
) -> Path:
    """Persist shared conversation state, display log, and CLI session IDs atomically."""
    target = _conversation_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "state": state.to_dict(),
        "display_messages": list(display_messages)[-MAX_SAVED_DISPLAY_MESSAGES:],
        "sessions": dict(sessions or {}),
    }
    raw = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    fd, tmp_name = tempfile.mkstemp(prefix=".conv-", suffix=".tmp", dir=str(target.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return target


def load_conversation(
    path: Path | None = None,
) -> tuple[ConversationState, list[dict[str, Any]], dict[str, str]]:
    """Load persisted conversation state, display messages, and CLI session IDs."""
    target = _conversation_path(path)
    if not target.is_file():
        return ConversationState(), [], {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ConversationState(), [], {}
    if not isinstance(data, dict):
        return ConversationState(), [], {}

    state = ConversationState.from_dict(data.get("state"))
    raw_msgs = data.get("display_messages")
    display_messages: list[dict[str, Any]] = []
    if isinstance(raw_msgs, list):
        for entry in raw_msgs[-MAX_SAVED_DISPLAY_MESSAGES:]:
            if isinstance(entry, dict) and entry.get("role"):
                display_messages.append(dict(entry))

    raw_sessions = data.get("sessions")
    sessions: dict[str, str] = {}
    if isinstance(raw_sessions, dict):
        for key, val in raw_sessions.items():
            if isinstance(key, str) and ":" in key and isinstance(val, str) and val.strip():
                sessions[key] = val.strip()

    return state, display_messages, sessions
