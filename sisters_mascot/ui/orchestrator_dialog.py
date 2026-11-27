"""Dialog for assigning per-sister AI CLI agents, models, and orchestrator options."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from sisters_mascot.agents.base import ModelChoice
from sisters_mascot.agents.router import AGENT_IDS, AgentRouter
from sisters_mascot.config import AppConfig, SisterAgentAssignment
from sisters_mascot.sisters_loader import SisterProfile
from sisters_mascot.ui.widgets import DARK_DIALOG_QSS

_DEFAULT_MODEL_LABEL = "（エージェント既定）"


def _short_name(display_name: str) -> str:
    name = (display_name or "").strip()
    if "(" in name:
        name = name.split("(", 1)[0].strip()
    if "（" in name:
        name = name.split("（", 1)[0].strip()
    return name or display_name


@dataclass
class _SisterRowControls:
    sister_id: str
    is_orchestrator: bool
    enabled_check: QCheckBox | None
    agent_combo: QComboBox
    model_combo: QComboBox


class OrchestratorTeamDialog(QDialog):
    """Dark-glass configuration dialog for Aoi's 17-sister orchestrator team."""

    def __init__(
        self,
        sisters: Sequence[SisterProfile],
        config: AppConfig,
        router: AgentRouter,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._sisters = sorted(sisters, key=lambda s: s.order)
        self._config = config
        self._router = router
        self._rows: dict[str, _SisterRowControls] = {}
        self._discovered_choices: dict[str, list[ModelChoice]] = {}
        self._updating_rows: bool = False

        self.setWindowTitle("👥 碧オーケストレーター・姉妹別AI編成")
        self.resize(740, 620)
        self.setStyleSheet(
            DARK_DIALOG_QSS
            + """
            QFrame#TeamTopCard {
                background-color: rgba(24, 30, 54, 215);
                border: 1px solid rgba(129, 140, 248, 0.35);
                border-radius: 12px;
            }
            QFrame.SisterRowCard {
                background-color: rgba(23, 28, 48, 185);
                border: 1px solid rgba(148, 163, 184, 0.16);
                border-radius: 9px;
            }
            QScrollArea {
                background: transparent;
                border: none;
            }
            """
        )

        self._load_cached_choices()
        self._build_ui()
        self._router.model_choices_discovered.connect(self._on_model_choices_discovered)
        self._kickoff_missing_discovery()

    def _load_cached_choices(self) -> None:
        for aid in AGENT_IDS:
            cached = self._router.get_cached_model_choices(aid)
            if cached is not None:
                self._discovered_choices[aid] = list(cached)

    def _kickoff_missing_discovery(self, *, force: bool = False) -> None:
        for aid in AGENT_IDS:
            if force or aid not in self._discovered_choices:
                self._router.refresh_models_async(aid, force=force)

    def _agent_label(self, agent_id: str) -> str:
        adapter = self._router.get_adapter(agent_id)
        disp = getattr(getattr(adapter, "spec", None), "display_name", agent_id) or agent_id
        avail = adapter.is_available() if hasattr(adapter, "is_available") else True
        return disp if avail else f"{disp} (未導入)"

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 12)
        outer.setSpacing(10)

        intro = QLabel(
            "長女・碧がタスクの指揮者（オーケストレーター）となり、専門の姉妹へ担当を振り分けます。\n"
            "各姉妹に「どの AI エージェント」と「どのモデル」を使わせるか個別に指定できます。"
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #cbd5e1; font-size: 12px;")
        outer.addWidget(intro)

        # Top card: Aoi + execution options + bulk controls
        top_card = QFrame(self)
        top_card.setObjectName("TeamTopCard")
        top_lay = QVBoxLayout(top_card)
        top_lay.setContentsMargins(12, 10, 12, 10)
        top_lay.setSpacing(8)

        aoi = next((s for s in self._sisters if s.id == "aoi"), self._sisters[0])
        top_lay.addWidget(self._create_sister_row(aoi, is_orchestrator=True, parent=top_card))

        opts_row = QHBoxLayout()
        opts_row.setSpacing(12)

        par_lbl = QLabel("同時実行数:", top_card)
        par_lbl.setStyleSheet("color: #cbd5e1; font-size: 11.5px;")
        self._max_parallel_combo = QComboBox(top_card)
        for val, text in (
            (1, "1 (順番に実行)"),
            (2, "2 (バランス・おすすめ)"),
            (3, "3 (高速)"),
            (4, "4 (最大並列)"),
        ):
            self._max_parallel_combo.addItem(text, val)
        cur_p = max(1, min(4, int(getattr(self._config, "orchestrator_max_parallel", 2) or 2)))
        p_idx = self._max_parallel_combo.findData(cur_p)
        if p_idx >= 0:
            self._max_parallel_combo.setCurrentIndex(p_idx)

        opts_row.addWidget(par_lbl)
        opts_row.addWidget(self._max_parallel_combo)

        self._confirm_plan_check = QCheckBox("実行前に碧の計画を確認してから動かす", top_card)
        self._confirm_plan_check.setChecked(bool(getattr(self._config, "orchestrator_confirm_plan", False)))
        opts_row.addWidget(self._confirm_plan_check, 1)
        top_lay.addLayout(opts_row)

        opts_row2 = QHBoxLayout()
        opts_row2.setSpacing(8)
        self._switch_mascot_check = QCheckBox("作業中の担当姉妹にマスコットを切り替える（セリフ・演出付き）", top_card)
        self._switch_mascot_check.setChecked(bool(getattr(self._config, "orchestrator_switch_mascot", True)))
        opts_row2.addWidget(self._switch_mascot_check, 1)

        refresh_btn = QPushButton("↻ 全AIモデル再取得", top_card)
        refresh_btn.clicked.connect(lambda: self._kickoff_missing_discovery(force=True))
        enable_all_btn = QPushButton("全員参加 ON", top_card)
        enable_all_btn.clicked.connect(self._enable_all_sisters)
        reset_all_btn = QPushButton("全員を既定AIへ戻す", top_card)
        reset_all_btn.clicked.connect(self._reset_all_to_default)
        opts_row2.addWidget(refresh_btn)
        opts_row2.addWidget(enable_all_btn)
        opts_row2.addWidget(reset_all_btn)
        top_lay.addLayout(opts_row2)

        outer.addWidget(top_card)

        # Column header
        hdr_row = QHBoxLayout()
        hdr_row.setContentsMargins(8, 0, 14, 0)
        hdr_row.setSpacing(8)
        for text, stretch, width in (
            ("参加 / 姉妹・役割", 1, 0),
            ("AIエージェント", 0, 175),
            ("モデル", 0, 230),
        ):
            lbl = QLabel(text, self)
            lbl.setStyleSheet("color: #94a3b8; font-size: 11px; font-weight: 700;")
            if width:
                lbl.setFixedWidth(width)
            hdr_row.addWidget(lbl, stretch)
        outer.addLayout(hdr_row)

        # Scrollable 16-sister roster
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        roster_body = QWidget(scroll)
        roster_lay = QVBoxLayout(roster_body)
        roster_lay.setContentsMargins(0, 0, 4, 4)
        roster_lay.setSpacing(6)

        for sister in self._sisters:
            if sister.id == "aoi":
                continue
            row_card = QFrame(roster_body)
            row_card.setProperty("class", "SisterRowCard")
            row_lay = QVBoxLayout(row_card)
            row_lay.setContentsMargins(8, 6, 8, 6)
            row_lay.addWidget(self._create_sister_row(sister, is_orchestrator=False, parent=row_card))
            roster_lay.addWidget(row_card)

        roster_lay.addStretch(1)
        scroll.setWidget(roster_body)
        outer.addWidget(scroll, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _create_sister_row(
        self,
        sister: SisterProfile,
        *,
        is_orchestrator: bool,
        parent: QWidget,
    ) -> QWidget:
        row_widget = QWidget(parent)
        lay = QHBoxLayout(row_widget)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        assignments = getattr(self._config, "sister_assignments", {}) or {}
        assignment = assignments.get(sister.id)

        enabled_check: QCheckBox | None = None
        if is_orchestrator:
            badge = QLabel("🎼 統括", row_widget)
            badge.setFixedWidth(56)
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setStyleSheet(
                "background-color: rgba(79, 70, 229, 0.38); color: #e0e7ff; "
                "border: 1px solid rgba(165, 180, 252, 0.5); border-radius: 6px; "
                "font-size: 10.5px; font-weight: 700; padding: 2px 4px;"
            )
            lay.addWidget(badge)
        else:
            enabled_check = QCheckBox(row_widget)
            enabled_check.setChecked(bool(assignment.enabled) if assignment is not None else True)
            enabled_check.setToolTip(f"{_short_name(sister.display_name)}をオーケストレーターの委譲先にする")
            lay.addWidget(enabled_check)

        dot = QLabel(row_widget)
        dot.setFixedSize(10, 10)
        dot.setStyleSheet(
            f"background-color: {sister.accent_color}; border-radius: 5px; border: none;"
        )
        lay.addWidget(dot)

        info_box = QVBoxLayout()
        info_box.setContentsMargins(0, 0, 0, 0)
        info_box.setSpacing(1)
        name_suffix = "（オーケストレーター）" if is_orchestrator else ""
        name_lbl = QLabel(f"{sister.rank_label} · {_short_name(sister.display_name)}{name_suffix}", row_widget)
        name_lbl.setStyleSheet("font-weight: 700; font-size: 12px; color: #f8fafc; background: transparent;")
        role_lbl = QLabel(sister.role, row_widget)
        role_lbl.setStyleSheet("font-size: 10.5px; color: #94a3b8; background: transparent;")
        info_box.addWidget(name_lbl)
        info_box.addWidget(role_lbl)
        lay.addLayout(info_box, 1)

        agent_combo = QComboBox(row_widget)
        agent_combo.setFixedWidth(175)
        agent_combo.addItem(f"既定 ({self._config.agent_id})", "")
        for aid in AGENT_IDS:
            agent_combo.addItem(self._agent_label(aid), aid)
        saved_aid = (assignment.agent_id if assignment is not None else "") or ""
        a_idx = agent_combo.findData(saved_aid)
        agent_combo.setCurrentIndex(a_idx if a_idx >= 0 else 0)

        model_combo = QComboBox(row_widget)
        model_combo.setFixedWidth(230)
        model_combo.setEditable(True)
        model_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        if model_combo.lineEdit() is not None:
            model_combo.lineEdit().setPlaceholderText(_DEFAULT_MODEL_LABEL)

        controls = _SisterRowControls(
            sister_id=sister.id,
            is_orchestrator=is_orchestrator,
            enabled_check=enabled_check,
            agent_combo=agent_combo,
            model_combo=model_combo,
        )
        self._rows[sister.id] = controls

        saved_model = (assignment.model if assignment is not None else "") or ""
        self._populate_row_models(sister.id, preserve_model=saved_model)
        agent_combo.currentIndexChanged.connect(
            lambda _idx, sid=sister.id: self._on_row_agent_changed(sid)
        )

        lay.addWidget(agent_combo)
        lay.addWidget(model_combo)
        return row_widget

    def _effective_agent_for_row(self, ctrl: _SisterRowControls) -> str:
        chosen = str(ctrl.agent_combo.currentData() or "").strip()
        return chosen if chosen in AGENT_IDS else self._config.agent_id

    def _current_row_model_value(self, ctrl: _SisterRowControls) -> str:
        combo = ctrl.model_combo
        text = combo.currentText().strip()
        if not text or text == _DEFAULT_MODEL_LABEL:
            return ""
        idx = combo.currentIndex()
        if idx >= 0 and combo.itemText(idx).strip() == text:
            data = combo.itemData(idx)
            if data is not None:
                return str(data).strip()
        for i in range(combo.count()):
            if combo.itemText(i).strip() == text:
                data = combo.itemData(i)
                if data is not None:
                    return str(data).strip()
        return text

    def _populate_row_models(
        self,
        sister_id: str,
        *,
        preserve_model: str | None = None,
    ) -> None:
        ctrl = self._rows.get(sister_id)
        if ctrl is None:
            return
        target_model = (
            self._current_row_model_value(ctrl)
            if preserve_model is None
            else preserve_model.strip()
        )
        effective_agent = self._effective_agent_for_row(ctrl)
        combo = ctrl.model_combo

        self._updating_rows = True
        try:
            combo.clear()
            combo.addItem(_DEFAULT_MODEL_LABEL, "")

            choices = self._discovered_choices.get(effective_agent)
            if choices is None:
                cached = self._router.get_cached_model_choices(effective_agent)
                if cached is not None:
                    choices = list(cached)
                    self._discovered_choices[effective_agent] = choices

            seen: set[str] = set()
            if choices:
                for choice in choices:
                    cid = (choice.id or "").strip()
                    if not cid or cid in seen:
                        continue
                    seen.add(cid)
                    combo.addItem((choice.label or cid).strip(), cid)
            else:
                adapter = self._router.get_adapter(effective_agent)
                defaults = list(getattr(getattr(adapter, "spec", None), "default_models", []) or [])
                for m in defaults:
                    m_clean = (m or "").strip()
                    if m_clean and m_clean not in seen:
                        seen.add(m_clean)
                        combo.addItem(m_clean, m_clean)

            if target_model:
                idx = combo.findData(target_model)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
                else:
                    combo.addItem(target_model, target_model)
                    combo.setCurrentIndex(combo.count() - 1)
                    combo.setEditText(target_model)
            else:
                combo.setCurrentIndex(0)
                combo.setEditText("")
        finally:
            self._updating_rows = False

    def _on_row_agent_changed(self, sister_id: str) -> None:
        if self._updating_rows:
            return
        ctrl = self._rows.get(sister_id)
        if ctrl is None:
            return
        effective_agent = self._effective_agent_for_row(ctrl)
        prev_model = self._current_row_model_value(ctrl)
        # Keep model only if the new agent explicitly lists it; otherwise inherit agent default.
        known_ids = {c.id for c in self._discovered_choices.get(effective_agent, [])}
        if not known_ids:
            adapter = self._router.get_adapter(effective_agent)
            known_ids = set(getattr(getattr(adapter, "spec", None), "default_models", []) or [])
        kept = prev_model if prev_model and prev_model in known_ids else ""
        self._populate_row_models(sister_id, preserve_model=kept)
        if effective_agent not in self._discovered_choices:
            self._router.refresh_models_async(effective_agent)

    def _on_model_choices_discovered(self, agent_id: str, choices: list) -> None:
        normalized: list[ModelChoice] = []
        for entry in choices:
            if isinstance(entry, ModelChoice):
                cid = (entry.id or "").strip()
                if cid:
                    normalized.append(ModelChoice(id=cid, label=(entry.label or cid).strip()))
            else:
                cid = str(entry).strip()
                if cid:
                    normalized.append(ModelChoice(id=cid, label=cid))
        self._discovered_choices[agent_id] = normalized
        for sid, ctrl in self._rows.items():
            if self._effective_agent_for_row(ctrl) == agent_id:
                self._populate_row_models(sid)

    def _enable_all_sisters(self) -> None:
        for ctrl in self._rows.values():
            if ctrl.enabled_check is not None:
                ctrl.enabled_check.setChecked(True)

    def _reset_all_to_default(self) -> None:
        self._updating_rows = True
        try:
            for ctrl in self._rows.values():
                ctrl.agent_combo.setCurrentIndex(0)
        finally:
            self._updating_rows = False
        for sid in list(self._rows.keys()):
            self._populate_row_models(sid, preserve_model="")

    def apply_to_config(self) -> None:
        """Write dialog selections back into `self._config`."""
        self._config.orchestrator_max_parallel = int(self._max_parallel_combo.currentData() or 2)
        self._config.orchestrator_confirm_plan = self._confirm_plan_check.isChecked()
        self._config.orchestrator_switch_mascot = self._switch_mascot_check.isChecked()

        updated: dict[str, SisterAgentAssignment] = {}
        for sid, ctrl in self._rows.items():
            aid = str(ctrl.agent_combo.currentData() or "").strip()
            if aid not in AGENT_IDS:
                aid = ""
            model = self._current_row_model_value(ctrl)
            enabled = True if ctrl.enabled_check is None else bool(ctrl.enabled_check.isChecked())
            if aid or model or not enabled:
                updated[sid] = SisterAgentAssignment(
                    agent_id=aid,
                    model=model,
                    enabled=enabled,
                )
        self._config.sister_assignments = updated

    def _on_save(self) -> None:
        self.apply_to_config()
        self.accept()

    def _disconnect_router(self) -> None:
        try:
            self._router.model_choices_discovered.disconnect(self._on_model_choices_discovered)
        except (TypeError, RuntimeError):
            pass

    def done(self, result: int) -> None:
        self._disconnect_router()
        super().done(result)

    def closeEvent(self, event) -> None:  # noqa: ANN001
        self._disconnect_router()
        super().closeEvent(event)
