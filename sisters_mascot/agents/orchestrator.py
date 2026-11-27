"""Multi-sister orchestrator engine led by Eldest Sister Aoi."""

from __future__ import annotations

import concurrent.futures
import json
import random
import re
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Sequence

from PyQt6.QtCore import QObject, pyqtSignal

from sisters_mascot.agents.base import is_playwright_tool
from sisters_mascot.agents.router import AGENT_IDS, AgentRouter, IsolatedTurnResult
from sisters_mascot.attachments import AttachmentItem, format_attachments_for_chat_display
from sisters_mascot.config import AppConfig, SisterAgentAssignment
from sisters_mascot.sisters_loader import SisterProfile

MAX_ORCHESTRATOR_TASKS: int = 6
MAX_DEPENDENCY_RESULT_CHARS: int = 3_500
MAX_SYNTHESIS_ITEM_CHARS: int = 4_000

_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL | re.IGNORECASE)


def _short_name(display_name: str) -> str:
    name = (display_name or "").strip()
    if "(" in name:
        name = name.split("(", 1)[0].strip()
    if "（" in name:
        name = name.split("（", 1)[0].strip()
    return name or display_name


def _clip_text(text: str, max_chars: int) -> str:
    clean = (text or "").strip()
    if len(clean) <= max_chars:
        return clean
    if max_chars <= 30:
        return clean[: max(1, max_chars - 1)] + "…"
    head = max_chars // 2 - 8
    tail = max_chars - head - 12
    return clean[:head].rstrip() + "\n…（中略）…\n" + clean[-tail:].lstrip()


@dataclass
class OrchestrationSubTask:
    """Single subtask delegated by Aoi to a specific sister and AI backend."""

    id: str
    sister_id: str
    title: str
    instruction: str
    depends_on: list[str] = field(default_factory=list)
    agent_id: str = ""
    model: str = ""
    fallback_note: str = ""
    dispatch_line: str = ""
    status: str = "pending"  # pending | running | done | error | skipped | cancelled
    output: str = ""
    error: str = ""
    elapsed: float = 0.0


@dataclass
class OrchestrationPlan:
    """Execution plan parsed from Aoi's planning phase output."""

    mode: str = "direct"  # "direct" | "delegate"
    aoi_opening: str = ""
    direct_reply: str = ""
    tasks: list[OrchestrationSubTask] = field(default_factory=list)
    raw_text: str = ""


def pick_transition_line(
    sister: SisterProfile,
    kind: str = "dispatch",
    last_line: str = "",
) -> str:
    """Pick a non-repeating character line for dispatch or return transitions."""
    if kind == "return":
        raw_pool = sister.return_lines or sister.dispatch_lines or []
    else:
        raw_pool = sister.dispatch_lines or ([sister.greeting] if sister.greeting else [])
    pool = [ln.strip() for ln in raw_pool if ln and ln.strip()]
    if not pool:
        short = _short_name(sister.display_name)
        if kind == "return":
            return f"姉妹たちの報告を統合いたします。"
        return f"{sister.rank_label}・{short}、出動いたします！"
    if len(pool) > 1 and last_line in pool:
        pool = [ln for ln in pool if ln != last_line]
    return random.choice(pool)


def get_delegatable_sisters(
    sisters: Sequence[SisterProfile],
    config: AppConfig,
) -> list[SisterProfile]:
    """Return ordered sisters eligible for task delegation (excluding orchestrator Aoi)."""
    assignments = getattr(config, "sister_assignments", {}) or {}
    enabled_list: list[SisterProfile] = []
    for sister in sorted(sisters, key=lambda s: s.order):
        if sister.id == "aoi":
            continue
        assignment = assignments.get(sister.id)
        if assignment is not None and not bool(getattr(assignment, "enabled", True)):
            continue
        enabled_list.append(sister)
    return enabled_list


def _is_agent_available(router: AgentRouter, agent_id: str) -> bool:
    try:
        return bool(router.get_adapter(agent_id).is_available())
    except Exception:
        return False


def resolve_sister_execution_target(
    config: AppConfig,
    router: AgentRouter,
    sister_id: str,
) -> tuple[str, str, str]:
    """Resolve `(agent_id, model, fallback_note)` for `sister_id` with availability fallback."""
    assignments = getattr(config, "sister_assignments", {}) or {}
    assignment = assignments.get(sister_id)
    explicit_agent = (getattr(assignment, "agent_id", "") or "").strip()
    explicit_model = (getattr(assignment, "model", "") or "").strip()

    chosen_agent = explicit_agent if explicit_agent in AGENT_IDS else config.agent_id
    if chosen_agent not in AGENT_IDS:
        chosen_agent = AGENT_IDS[0]

    default_model_for_agent = (config.models_by_agent.get(chosen_agent, "") or "").strip()
    chosen_model = explicit_model or default_model_for_agent

    if _is_agent_available(router, chosen_agent):
        return chosen_agent, chosen_model, ""

    candidates: list[str] = []
    if config.agent_id in AGENT_IDS:
        candidates.append(config.agent_id)
    for aid in AGENT_IDS:
        if aid not in candidates:
            candidates.append(aid)

    for cand in candidates:
        if _is_agent_available(router, cand):
            fb_model = (config.models_by_agent.get(cand, "") or "").strip()
            note = f"{chosen_agent} が未導入/利用不可のため {cand} へ自動フォールバックしました"
            return cand, fb_model, note

    return chosen_agent, chosen_model, ""


def _extract_json_dict(raw_text: str) -> dict[str, Any] | None:
    text = (raw_text or "").strip()
    if not text:
        return None

    for match in _FENCED_JSON_RE.finditer(text):
        candidate = match.group(1).strip()
        try:
            loaded = json.loads(candidate)
            if isinstance(loaded, dict):
                return loaded
        except json.JSONDecodeError:
            continue

    try:
        loaded = json.loads(text)
        if isinstance(loaded, dict):
            return loaded
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            loaded = json.loads(text[start : end + 1])
            if isinstance(loaded, dict):
                return loaded
        except json.JSONDecodeError:
            pass
    return None


def _break_dependency_cycles(tasks: list[OrchestrationSubTask]) -> None:
    """Sanitize depends_on lists in-place to strip unknown IDs, self-links, and cycles."""
    task_map = {t.id: t for t in tasks}
    state: dict[str, int] = {}  # 0=unvisited, 1=visiting, 2=visited

    def _visit(tid: str) -> None:
        state[tid] = 1
        task = task_map[tid]
        clean_deps: list[str] = []
        for dep in task.depends_on:
            if dep not in task_map or dep == tid or dep in clean_deps:
                continue
            st = state.get(dep, 0)
            if st == 1:
                continue
            if st == 0:
                _visit(dep)
            clean_deps.append(dep)
        task.depends_on = clean_deps
        state[tid] = 2

    for task in tasks:
        if state.get(task.id, 0) == 0:
            _visit(task.id)


def parse_orchestration_plan(
    raw_text: str,
    enabled_sisters: Sequence[SisterProfile],
) -> OrchestrationPlan:
    """Parse Aoi's planning output into a cycle-free OrchestrationPlan."""
    parsed = _extract_json_dict(raw_text)
    if parsed is None:
        return OrchestrationPlan(
            mode="direct",
            direct_reply=(raw_text or "").strip(),
            raw_text=raw_text or "",
        )

    alias_map: dict[str, SisterProfile] = {}
    for sister in enabled_sisters:
        if sister.id == "aoi":
            continue
        alias_map[sister.id.lower()] = sister
        if sister.romaji:
            alias_map[sister.romaji.lower()] = sister
        short = _short_name(sister.display_name)
        if short:
            alias_map[short] = sister
        if sister.display_name:
            alias_map[sister.display_name] = sister

    mode = str(parsed.get("mode") or "").strip().lower()
    aoi_opening = str(parsed.get("aoi_opening") or "").strip()
    direct_reply = str(parsed.get("direct_reply") or "").strip()

    tasks: list[OrchestrationSubTask] = []
    used_ids: set[str] = set()
    raw_tasks = parsed.get("tasks")
    if isinstance(raw_tasks, list):
        seq = 1
        for entry in raw_tasks:
            if len(tasks) >= MAX_ORCHESTRATOR_TASKS:
                break
            if not isinstance(entry, dict):
                continue
            raw_sid = str(entry.get("sister_id") or entry.get("sister") or "").strip()
            sister = alias_map.get(raw_sid.lower()) or alias_map.get(raw_sid)
            if sister is None:
                continue

            instruction = str(entry.get("instruction") or entry.get("task") or "").strip()
            if not instruction:
                continue

            title = str(entry.get("title") or "").strip() or f"{_short_name(sister.display_name)}の担当"
            cand_id = str(entry.get("id") or "").strip()
            if not cand_id or cand_id in used_ids:
                while f"t{seq}" in used_ids:
                    seq += 1
                cand_id = f"t{seq}"
                seq += 1
            used_ids.add(cand_id)

            raw_deps = entry.get("depends_on")
            deps: list[str] = []
            if isinstance(raw_deps, list):
                deps = [str(d).strip() for d in raw_deps if str(d).strip()]
            elif isinstance(raw_deps, str) and raw_deps.strip():
                deps = [raw_deps.strip()]

            tasks.append(
                OrchestrationSubTask(
                    id=cand_id,
                    sister_id=sister.id,
                    title=title,
                    instruction=instruction,
                    depends_on=deps,
                )
            )

    _break_dependency_cycles(tasks)

    if mode == "direct" or not tasks:
        fallback_reply = direct_reply or aoi_opening or (raw_text or "").strip()
        return OrchestrationPlan(
            mode="direct",
            aoi_opening=aoi_opening,
            direct_reply=fallback_reply,
            tasks=[],
            raw_text=raw_text or "",
        )

    return OrchestrationPlan(
        mode="delegate",
        aoi_opening=aoi_opening,
        direct_reply=direct_reply,
        tasks=tasks,
        raw_text=raw_text or "",
    )


def build_planning_prompts(
    prompt: str,
    *,
    aoi: SisterProfile,
    delegatable_sisters: Sequence[SisterProfile],
    resolved_targets: dict[str, tuple[str, str]],
    attachments: Sequence[AttachmentItem] = (),
    handoff_context: str = "",
) -> tuple[str, str]:
    """Build `(system_prompt, user_prompt)` for Aoi's planning phase."""
    sys_prompt = (
        f"{aoi.persona_prompt}\n\n"
        "【オーケストレーター計画フェーズの厳格ルール】\n"
        "- あなたは現在、シスターズの長女・碧としてチーム編成と作業計画（JSON）を立案しています。\n"
        "- この計画フェーズでは、ツール呼び出し・ファイル編集・コマンド実行・Webアクセスを一切行わないでください。\n"
        "- 雑談・挨拶・単純な質問であれば `\"mode\": \"direct\"` を選び、`direct_reply` に温かく簡潔な回答を入れてください。\n"
        "- 調査・コード実装・設計・品質検証・セキュリティ監査・文書化などの実作業や複合タスクは `\"mode\": \"delegate\"` を選び、専門に合った姉妹へ1〜6件のサブタスクを割り振ってください。\n"
        "- 通常は1〜3名程度の最小限の適任姉妹に絞り、単純な動作確認や軽い質問で不要に多数の姉妹へ分割しすぎないでください。\n"
        "- 互いに独立した作業は `depends_on: []` にして並列実行させ、前段の結果を受ける作業（検証・最終レビュー・まとめなど）には `depends_on` に対象タスクの `id` を指定してください。\n"
        "- 碧自身の文章では三点リーダー（... や ⋯ や …）を使用しないでください。\n"
        "- 出力は必ず下記のJSONオブジェクト1つのみ（```json コードブロック内）にしてください。"
    )

    roster_lines: list[str] = []
    for sister in delegatable_sisters:
        aid, mdl = resolved_targets.get(sister.id, ("", ""))
        model_disp = mdl or "既定モデル"
        roster_lines.append(
            f"- id=`{sister.id}` | {sister.rank_label}・{_short_name(sister.display_name)} | "
            f"職能: {sister.role} | 使用AI: `{aid}` (`{model_disp}`)"
        )

    att_lines = ""
    if attachments:
        items_meta = format_attachments_for_chat_display(attachments)
        att_desc = ", ".join(
            f"{it['icon']} {it['name']} ({it['path']})" for it in items_meta
        )
        att_lines = f"\n\n【添付あり（各担当姉妹へ自動引き継ぎされます）】\n{att_desc}"

    handoff_block = f"{handoff_context.strip()}\n\n" if handoff_context.strip() else ""

    user_prompt = (
        f"{handoff_block}"
        "【招集可能なシスターズ一覧】\n"
        + ("\n".join(roster_lines) if roster_lines else "（現在委譲できる妹がいないため direct モードで回答してください）")
        + f"{att_lines}\n\n"
        "【美緒ちゃんのリクエスト】\n"
        f"{prompt.strip() or '添付されたファイル・フォルダを確認して対応してください。'}\n\n"
        "【出力JSONフォーマット】\n"
        "```json\n"
        "{\n"
        '  "mode": "delegate または direct",\n'
        '  "aoi_opening": "美緒ちゃんと姉妹たちへの指揮宣言（日本語2文程度、三点リーダー禁止）",\n'
        '  "direct_reply": "modeがdirectのときのみ使う最終回答本文",\n'
        '  "tasks": [\n'
        "    {\n"
        '      "id": "t1",\n'
        '      "sister_id": "一覧にあるid（例: ruri, hiiro, akari 等。aoi不可）",\n'
        '      "title": "短いタスク見出し（20文字以内）",\n'
        '      "instruction": "その姉妹に渡す具体的で過不足ない指示",\n'
        '      "depends_on": []\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "```"
    )
    return sys_prompt, user_prompt


def build_worker_prompts(
    router: AgentRouter,
    sister: SisterProfile,
    task: OrchestrationSubTask,
    *,
    original_prompt: str,
    dep_results: Sequence[tuple[OrchestrationSubTask, SisterProfile | None]],
    handoff_context: str = "",
) -> tuple[str, str]:
    """Build `(system_prompt, user_prompt)` for a delegated sister's execution turn."""
    base_persona = router._build_persona_context(sister)
    sys_prompt = (
        f"{base_persona}\n\n"
        "【シスターズ・オーケストレーター連携ルール】\n"
        "- あなたは現在、長女・碧の指揮のもとチームで分担された担当タスクを実行しています。\n"
        "- このターンは非対話の1回完結実行です。「引き受けました」「これから確認・作成します」のような着手宣言だけで絶対に終わらせず、この1回の応答の中で必要な調査・作成・検証をすべて完了させ、具体的な成果物・結論そのものを返してください。\n"
        "- 他の姉妹と重複する長い前置きや冗長な表の羅列は避け、要点を簡潔かつ構造化して報告してください。"
    )

    sections: list[str] = []
    if handoff_context.strip():
        sections.append(handoff_context.strip())

    sections.append(
        "【美緒ちゃんの元のリクエスト】\n"
        + (original_prompt.strip() or "添付されたファイル・フォルダへの対応")
    )
    sections.append(
        "【長女・碧からあなたへの担当指示】\n"
        f"- タスクID: {task.id}\n"
        f"- 担当タスク: {task.title}\n"
        f"- 指示内容:\n{task.instruction}"
    )

    if dep_results:
        dep_lines: list[str] = ["<dependency_results>", "【先行タスクを担当した姉妹の報告】"]
        for dep_task, dep_sister in dep_results:
            dep_name = _short_name(dep_sister.display_name) if dep_sister else dep_task.sister_id
            body = _clip_text(dep_task.output or dep_task.error or "（出力なし）", MAX_DEPENDENCY_RESULT_CHARS)
            dep_lines.append(
                f"### [{dep_task.id}] {dep_name} ({dep_task.agent_id}) — {dep_task.title}\n{body}"
            )
        dep_lines.append("</dependency_results>")
        sections.append("\n".join(dep_lines))

    return sys_prompt, "\n\n".join(sections)


def build_synthesis_prompts(
    router: AgentRouter,
    aoi: SisterProfile,
    original_prompt: str,
    plan: OrchestrationPlan,
    sister_map: dict[str, SisterProfile],
    *,
    handoff_context: str = "",
) -> tuple[str, str]:
    """Build `(system_prompt, user_prompt)` for Aoi's final synthesis turn."""
    base_persona = router._build_persona_context(aoi)
    sys_prompt = (
        f"{base_persona}\n\n"
        "【オーケストレーター統合報告フェーズ】\n"
        "- あなたは長女・碧です。各姉妹が提出した成果と失敗状況を踏まえ、美緒ちゃんへわかりやすく美しい最終報告に統合してください。\n"
        "- この統合フェーズでは追加のツール呼び出し・コマンド実行は行わないでください。\n"
        "- 失敗したタスクやスキップされたタスクがある場合は隠さず正直に伝え、確認できた範囲の成果を整理してください。\n"
        "- 存在しない内部スキル（`audit` 等）や `RUBRIC.md` / `Trace ID` などの難しい内部用語を並べて美緒ちゃんを困惑させず、何ができて何がまだか・次にどうすると良いかをやさしい言葉でまとめてください。\n"
        "- 三点リーダー（... や ⋯ や …）は使用しないでください。"
    )

    parts: list[str] = []
    if handoff_context.strip():
        parts.append(handoff_context.strip())

    parts.append(
        "【美緒ちゃんの元のリクエスト】\n"
        + (original_prompt.strip() or "添付されたファイル・フォルダへの対応")
    )
    if plan.aoi_opening:
        parts.append(f"【碧の当初方針】\n{plan.aoi_opening}")

    parts.append("【各姉妹からの作業結果】")
    for task in plan.tasks:
        sister = sister_map.get(task.sister_id)
        sname = _short_name(sister.display_name) if sister else task.sister_id
        rank = sister.rank_label if sister else ""
        header = (
            f"### [{task.id}] {rank} {sname} ({task.agent_id}"
            + (f" / {task.model}" if task.model else "")
            + f") — {task.title} [{task.status}, {task.elapsed:.1f}s]"
        )
        if task.status == "done":
            body = _clip_text(task.output or "（完了・本文なし）", MAX_SYNTHESIS_ITEM_CHARS)
        elif task.error:
            partial = (
                f"\n\n途中出力:\n{_clip_text(task.output, 800)}"
                if task.output
                else ""
            )
            body = f"エラー/未完了: {task.error}{partial}"
        else:
            body = f"状態: {task.status}"
        parts.append(f"{header}\n{body}")

    parts.append("上記の各姉妹の報告を踏まえて、美緒ちゃんへの最終まとめをお願いします。")
    return sys_prompt, "\n\n".join(parts)


def build_fallback_synthesis_text(plan: OrchestrationPlan, sister_map: dict[str, SisterProfile] | None = None) -> str:
    """Deterministic fallback Markdown report used if Aoi's synthesis call fails."""
    lines: list[str] = []
    if plan.aoi_opening:
        lines.append(plan.aoi_opening)
    else:
        lines.append("各担当シスターズからの報告を取りまとめました。")

    for task in plan.tasks:
        sister = sister_map.get(task.sister_id) if sister_map else None
        sname = _short_name(sister.display_name) if sister else task.sister_id
        icon = {"done": "✅", "error": "❌", "skipped": "⏭️", "cancelled": "⏹"}.get(task.status, "•")
        model_tag = f"{task.agent_id} / {task.model}" if task.model else task.agent_id
        lines.append(f"\n### {icon} {sname} ({model_tag}) — {task.title}")
        if task.output:
            lines.append(_clip_text(task.output, 2_000))
        if task.error:
            lines.append(f"⚠️ {task.error}")
    return "\n".join(lines).strip()


class OrchestratorEngine(QObject):
    """Coordinates Aoi's 3-phase multi-sister AI orchestration in background threads."""

    phase_changed = pyqtSignal(str, str)  # (phase, status_text)
    active_sister_changed = pyqtSignal(str, str, str, str, str)
    plan_ready = pyqtSignal(object, bool)  # (OrchestrationPlan, needs_confirmation)
    task_started = pyqtSignal(object)  # OrchestrationSubTask snapshot
    task_updated = pyqtSignal(str, str)  # (task_id, status_text)
    task_finished = pyqtSignal(object)  # OrchestrationSubTask snapshot
    synthesis_delta = pyqtSignal(str)
    synthesis_replace = pyqtSignal(str)
    orchestration_finished = pyqtSignal(str, object)  # (final_text, OrchestrationPlan)
    orchestration_cancelled = pyqtSignal(str)
    orchestration_error = pyqtSignal(str)

    def __init__(
        self,
        router: AgentRouter,
        sisters: Sequence[SisterProfile],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._router = router
        self._sisters: list[SisterProfile] = sorted(sisters, key=lambda s: s.order)
        self._sister_map: dict[str, SisterProfile] = {s.id: s for s in self._sisters}
        self._lock = threading.Lock()
        self._running: bool = False
        self._phase: str = "idle"  # idle | planning | awaiting_approval | executing | synthesizing
        self._cancel_event = threading.Event()
        self._pending_context: dict[str, Any] | None = None
        self._last_lines: dict[str, str] = {}

    def is_running(self) -> bool:
        """Return True while planning, awaiting confirmation, executing, or synthesizing."""
        with self._lock:
            return self._running

    def is_awaiting_approval(self) -> bool:
        """Return True if Aoi's plan is paused waiting for the user's go-ahead."""
        with self._lock:
            return self._running and self._phase == "awaiting_approval"

    def _next_line(self, sister: SisterProfile, kind: str = "dispatch") -> str:
        key = f"{sister.id}:{kind}"
        with self._lock:
            last = self._last_lines.get(key, "")
        chosen = pick_transition_line(sister, kind=kind, last_line=last)
        with self._lock:
            self._last_lines[key] = chosen
        return chosen

    def _snapshot_config(self, config: AppConfig) -> AppConfig:
        assignments = {
            sid: SisterAgentAssignment(
                agent_id=a.agent_id,
                model=a.model,
                enabled=a.enabled,
            )
            for sid, a in (config.sister_assignments or {}).items()
        }
        return AppConfig(
            sister_id=config.sister_id,
            agent_id=config.agent_id,
            models_by_agent=dict(config.models_by_agent),
            orchestrator_mode=bool(config.orchestrator_mode),
            orchestrator_confirm_plan=bool(config.orchestrator_confirm_plan),
            orchestrator_max_parallel=max(1, min(4, int(config.orchestrator_max_parallel))),
            orchestrator_switch_mascot=bool(config.orchestrator_switch_mascot),
            sister_assignments=assignments,
            working_dir=config.working_dir,
            auto_approve=bool(config.auto_approve),
        )

    def start(
        self,
        prompt: str,
        *,
        config: AppConfig,
        attachments: Sequence[AttachmentItem] = (),
        handoff_context: str = "",
    ) -> bool:
        """Begin Phase 1 orchestration in a background thread."""
        with self._lock:
            if self._running:
                self.orchestration_error.emit(
                    "現在オーケストレーションを実行中です。完了または停止をお待ちください。"
                )
                return False
            self._running = True
            self._phase = "planning"
            self._cancel_event = threading.Event()
            self._pending_context = None

        ctx = {
            "prompt": (prompt or "").strip(),
            "config": self._snapshot_config(config),
            "attachments": tuple(attachments),
            "handoff": (handoff_context or "").strip(),
            "cancel_event": self._cancel_event,
        }
        worker = threading.Thread(target=self._run_planning, args=(ctx,), daemon=True)
        worker.start()
        return True

    def approve_pending_plan(self) -> bool:
        """Resume execution for a plan that was awaiting user approval."""
        with self._lock:
            if not self._running or self._phase != "awaiting_approval" or not self._pending_context:
                return False
            ctx = self._pending_context
            self._pending_context = None
            self._phase = "executing"

        worker = threading.Thread(
            target=self._run_execution_and_synthesis,
            args=(ctx,),
            daemon=True,
        )
        worker.start()
        return True

    def reject_pending_plan(self) -> None:
        """Cancel a plan that is waiting for user approval."""
        with self._lock:
            if self._phase != "awaiting_approval":
                return
            self._pending_context = None
            self._running = False
            self._phase = "idle"
            self._cancel_event.set()
        self.phase_changed.emit("idle", "")
        self.orchestration_cancelled.emit("碧の計画をキャンセルしました。")

    def cancel(self) -> None:
        """Cancel any active planning, worker execution, approval wait, or synthesis."""
        was_awaiting = False
        with self._lock:
            self._cancel_event.set()
            if self._phase == "awaiting_approval":
                was_awaiting = True
                self._pending_context = None
                self._running = False
                self._phase = "idle"

        if was_awaiting:
            self.phase_changed.emit("idle", "")
            self.orchestration_cancelled.emit("碧の計画をキャンセルしました。")
            return

        self._router.cancel()

    def _finish_idle(self) -> None:
        with self._lock:
            self._running = False
            self._phase = "idle"
            self._pending_context = None
        self.phase_changed.emit("idle", "")

    def _run_planning(self, ctx: dict[str, Any]) -> None:
        cfg: AppConfig = ctx["config"]
        cancel_event: threading.Event = ctx["cancel_event"]
        attachments: tuple[AttachmentItem, ...] = ctx["attachments"]
        prompt: str = ctx["prompt"]
        handoff: str = ctx["handoff"]
        cwd = (cfg.working_dir or str(Path.home())).strip() or str(Path.home())

        aoi = self._sister_map.get("aoi") or self._sisters[0]
        try:
            aoi_agent, aoi_model, aoi_fb = resolve_sister_execution_target(cfg, self._router, aoi.id)
            if aoi_fb:
                self._router._emit_activity("warn", f"[碧·計画] ⚠️ {aoi_fb}")

            aoi_cmd_line = self._next_line(aoi, kind="dispatch")
            self.phase_changed.emit("planning", "🎼 碧姉さんが担当計画を立案中…")
            self.active_sister_changed.emit(
                aoi.id,
                "command",
                "thinking",
                "🎼 計画立案中…",
                aoi_cmd_line,
            )

            delegatable = get_delegatable_sisters(self._sisters, cfg)
            resolved_targets: dict[str, tuple[str, str]] = {}
            for s in delegatable:
                s_agent, s_model, _ = resolve_sister_execution_target(cfg, self._router, s.id)
                resolved_targets[s.id] = (s_agent, s_model)

            plan_sys, plan_user = build_planning_prompts(
                prompt,
                aoi=aoi,
                delegatable_sisters=delegatable,
                resolved_targets=resolved_targets,
                attachments=attachments,
                handoff_context=handoff,
            )
            plan_res: IsolatedTurnResult = self._router.run_isolated_turn(
                aoi_agent,
                plan_user,
                system_prompt=plan_sys,
                model=aoi_model,
                cwd=cwd,
                auto_approve=False,
                attachments=(),
                cancel_event=cancel_event,
                log_prefix=f"[碧·計画 / {aoi_agent}:{aoi_model or '既定'}] ",
            )

            if plan_res.cancelled or cancel_event.is_set():
                self._finish_idle()
                self.orchestration_cancelled.emit("碧の計画立案を中断しました。")
                return

            if plan_res.error and not plan_res.text:
                self._finish_idle()
                self.orchestration_error.emit(f"碧の計画立案でエラーが発生しました: {plan_res.error}")
                return

            plan = parse_orchestration_plan(plan_res.text, delegatable)
            for task in plan.tasks:
                t_agent, t_model, t_fb = resolve_sister_execution_target(cfg, self._router, task.sister_id)
                task.agent_id = t_agent
                task.model = t_model
                task.fallback_note = t_fb
                target_sister = self._sister_map.get(task.sister_id)
                if target_sister is not None:
                    task.dispatch_line = self._next_line(target_sister, kind="dispatch")
                if t_fb:
                    t_name = _short_name(target_sister.display_name) if target_sister else task.sister_id
                    self._router._emit_activity("warn", f"[{t_name}] ⚠️ {t_fb}")

            if plan.mode == "direct" or not plan.tasks:
                self._finish_idle()
                self.orchestration_finished.emit(plan.direct_reply or plan_res.text, plan)
                return

            plan_snapshot = OrchestrationPlan(
                mode=plan.mode,
                aoi_opening=plan.aoi_opening,
                direct_reply=plan.direct_reply,
                tasks=[replace(t, depends_on=list(t.depends_on)) for t in plan.tasks],
                raw_text=plan.raw_text,
            )
            ctx["plan"] = plan
            confirm_plan = bool(cfg.orchestrator_confirm_plan)

            if confirm_plan:
                with self._lock:
                    if cancel_event.is_set():
                        self._running = False
                        self._phase = "idle"
                        cancelled_early = True
                    else:
                        self._phase = "awaiting_approval"
                        self._pending_context = ctx
                        cancelled_early = False
                if cancelled_early:
                    self.phase_changed.emit("idle", "")
                    self.orchestration_cancelled.emit("碧の計画を中断しました。")
                    return
                self.plan_ready.emit(plan_snapshot, True)
                self.phase_changed.emit("awaiting_approval", "🎼 碧の計画を確認してください（実行待ち）")
                return

            with self._lock:
                self._phase = "executing"
            self.plan_ready.emit(plan_snapshot, False)
        except Exception as exc:
            self._finish_idle()
            self.orchestration_error.emit(f"オーケストレーター計画中に予期しないエラー: {exc}")
            return

        self._run_execution_and_synthesis(ctx)

    def _run_execution_and_synthesis(self, ctx: dict[str, Any]) -> None:
        cfg: AppConfig = ctx["config"]
        cancel_event: threading.Event = ctx["cancel_event"]
        attachments: tuple[AttachmentItem, ...] = ctx["attachments"]
        prompt: str = ctx["prompt"]
        handoff: str = ctx["handoff"]
        plan: OrchestrationPlan = ctx["plan"]
        cwd = (cfg.working_dir or str(Path.home())).strip() or str(Path.home())
        aoi = self._sister_map.get("aoi") or self._sisters[0]
        max_workers = max(1, min(4, int(cfg.orchestrator_max_parallel)))

        try:
            self.phase_changed.emit("executing", f"🤝 シスターズが担当作業を実行中… (0/{len(plan.tasks)})")
            tasks_by_id = {t.id: t for t in plan.tasks}
            pending: list[OrchestrationSubTask] = list(plan.tasks)
            running_futures: dict[concurrent.futures.Future[IsolatedTurnResult], OrchestrationSubTask] = {}
            completed_count = 0

            def _run_single_task(task: OrchestrationSubTask) -> IsolatedTurnResult:
                sister = self._sister_map[task.sister_id]
                short = _short_name(sister.display_name)
                dep_results = [
                    (tasks_by_id[dep_id], self._sister_map.get(tasks_by_id[dep_id].sister_id))
                    for dep_id in task.depends_on
                    if dep_id in tasks_by_id
                ]
                sys_p, user_p = build_worker_prompts(
                    self._router,
                    sister,
                    task,
                    original_prompt=prompt,
                    dep_results=dep_results,
                    handoff_context=handoff,
                )

                def _on_status(msg: str) -> None:
                    self.task_updated.emit(task.id, msg)
                    kind = "tool" if ("chrome" in msg.lower() or "playwright" in msg.lower() or "🌐" in msg) else "thinking"
                    self.active_sister_changed.emit(task.sister_id, "update", kind, f"{short}: {msg}", "")

                def _on_tool(tool_name: str, msg: str) -> None:
                    self.task_updated.emit(task.id, msg)
                    kind = "tool" if is_playwright_tool(tool_name) or "🌐" in msg else "thinking"
                    self.active_sister_changed.emit(task.sister_id, "update", kind, f"{short}: {msg}", "")

                prefix = f"[{short} / {task.agent_id}:{task.model or '既定'}] "
                return self._router.run_isolated_turn(
                    task.agent_id,
                    user_p,
                    system_prompt=sys_p,
                    model=task.model,
                    cwd=cwd,
                    auto_approve=bool(cfg.auto_approve),
                    attachments=attachments,
                    cancel_event=cancel_event,
                    log_prefix=prefix,
                    on_status=_on_status,
                    on_tool=_on_tool,
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                while pending or running_futures:
                    if cancel_event.is_set():
                        # Skip any tasks that have not even started yet
                        while pending:
                            unstarted = pending.pop(0)
                            unstarted.status = "cancelled"
                            unstarted.error = "ユーザーにより中断されました"
                            self.task_finished.emit(replace(unstarted))
                        break

                    # Check dependency skips or ready launches
                    next_pending: list[OrchestrationSubTask] = []
                    for task in pending:
                        dep_objs = [tasks_by_id[d] for d in task.depends_on if d in tasks_by_id]
                        if any(d.status in ("error", "skipped", "cancelled") for d in dep_objs):
                            failed_names = ", ".join(
                                _short_name(self._sister_map[d.sister_id].display_name)
                                if d.sister_id in self._sister_map
                                else d.sister_id
                                for d in dep_objs
                                if d.status in ("error", "skipped", "cancelled")
                            )
                            task.status = "skipped"
                            task.error = f"前提タスク（{failed_names}）が完了しなかったためスキップしました"
                            completed_count += 1
                            self.task_finished.emit(replace(task))
                            continue

                        all_deps_done = all(d.status == "done" for d in dep_objs)
                        if all_deps_done and len(running_futures) < max_workers:
                            task.status = "running"
                            sister = self._sister_map[task.sister_id]
                            short = _short_name(sister.display_name)
                            if not task.dispatch_line:
                                task.dispatch_line = self._next_line(sister, kind="dispatch")
                            snap = replace(task, depends_on=list(task.depends_on))
                            self.task_started.emit(snap)
                            self.active_sister_changed.emit(
                                sister.id,
                                "summon",
                                "thinking",
                                f"🎼 {short}: {task.title}",
                                task.dispatch_line,
                            )
                            fut = executor.submit(_run_single_task, task)
                            running_futures[fut] = task
                        else:
                            next_pending.append(task)

                    pending = next_pending
                    if not running_futures:
                        if pending:
                            # Defensive guard: break cleanly if unexpected unschedulable state occurs.
                            for orphan in pending:
                                orphan.status = "skipped"
                                orphan.error = "実行順序を解決できなかったためスキップしました"
                                self.task_finished.emit(replace(orphan))
                        break

                    done_futs, _ = concurrent.futures.wait(
                        list(running_futures.keys()),
                        timeout=0.2,
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )
                    for fut in done_futs:
                        task = running_futures.pop(fut)
                        try:
                            res = fut.result()
                        except Exception as exc:
                            task.status = "error"
                            task.error = str(exc)
                        else:
                            task.output = res.text
                            task.elapsed = res.elapsed
                            if res.cancelled or cancel_event.is_set():
                                task.status = "cancelled"
                                task.error = "ユーザーにより中断されました"
                            elif res.error:
                                task.status = "error"
                                task.error = res.error
                            else:
                                task.status = "done"
                        completed_count += 1
                        self.task_finished.emit(replace(task, depends_on=list(task.depends_on)))
                        self.phase_changed.emit(
                            "executing",
                            f"🤝 シスターズが担当作業を実行中… ({completed_count}/{len(plan.tasks)})",
                        )

            if cancel_event.is_set():
                self._finish_idle()
                self.orchestration_cancelled.emit(build_fallback_synthesis_text(plan, self._sister_map))
                return

            # Phase 3: Synthesis by Aoi
            with self._lock:
                self._phase = "synthesizing"
            aoi_ret_line = self._next_line(aoi, kind="return")
            self.phase_changed.emit("synthesizing", "💠 碧姉さんが報告を統合中…")
            self.active_sister_changed.emit(
                aoi.id,
                "return",
                "thinking",
                "✨ 最終報告を統合中…",
                aoi_ret_line,
            )

            aoi_agent, aoi_model, _ = resolve_sister_execution_target(cfg, self._router, aoi.id)
            syn_sys, syn_user = build_synthesis_prompts(
                self._router,
                aoi,
                prompt,
                plan,
                self._sister_map,
                handoff_context=handoff,
            )
            syn_res = self._router.run_isolated_turn(
                aoi_agent,
                syn_user,
                system_prompt=syn_sys,
                model=aoi_model,
                cwd=cwd,
                auto_approve=False,
                attachments=(),
                cancel_event=cancel_event,
                log_prefix=f"[碧·統合 / {aoi_agent}:{aoi_model or '既定'}] ",
                on_delta=self.synthesis_delta.emit,
                on_replace=self.synthesis_replace.emit,
            )

            if syn_res.cancelled or cancel_event.is_set():
                self._finish_idle()
                self.orchestration_cancelled.emit(build_fallback_synthesis_text(plan, self._sister_map))
                return

            final_text = (syn_res.text or "").strip()
            if syn_res.error or not final_text:
                fallback_body = build_fallback_synthesis_text(plan, self._sister_map)
                if syn_res.error:
                    final_text = (
                        f"（注: 碧の統合サマリ生成中にエラーが発生したため、各姉妹の結果をそのまままとめました: {syn_res.error}）\n\n"
                        + fallback_body
                    )
                else:
                    final_text = fallback_body

            self._finish_idle()
            self.orchestration_finished.emit(final_text, plan)
        except Exception as exc:
            self._finish_idle()
            self.orchestration_error.emit(f"オーケストレーター実行中に予期しないエラー: {exc}")
