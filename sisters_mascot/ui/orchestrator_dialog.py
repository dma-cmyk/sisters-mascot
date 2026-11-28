"""Dialog for assigning per-sister AI CLI agents, models, and orchestrator options."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPainter
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from sisters_mascot.agents.base import (
    EFFORT_DISPLAY_LABELS,
    EFFORT_SHORT_LABELS,
    ModelChoice,
)
from sisters_mascot.agents.router import AGENT_IDS, AgentRouter
from sisters_mascot.config import AppConfig, SisterAgentAssignment, normalize_effort
from sisters_mascot.sisters_loader import SisterProfile
from sisters_mascot.ui.widgets import (
    DARK_DIALOG_QSS,
    DarkCheckBoxStyle,
    apply_dark_checkbox_style,
)

_DEFAULT_MODEL_LABEL = "（エージェント既定）"
_DEFAULT_EFFORT_LABEL = "（既定）"
_AGENT_COMBO_WIDTH = 165
_MODEL_COMBO_WIDTH = 230
_EFFORT_COMBO_WIDTH = 112


def _short_name(display_name: str) -> str:
    name = (display_name or "").strip()
    if "(" in name:
        name = name.split("(", 1)[0].strip()
    if "（" in name:
        name = name.split("（", 1)[0].strip()
    return name or display_name


class _ElidedLabel(QLabel):
    """Single-line label that elides overflow text without pushing sibling controls."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._full_text = text
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)

    def setText(self, text: str) -> None:  # noqa: N802
        self._full_text = text
        super().setText(text)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: ANN001
        _ = event
        painter = QPainter(self)
        painter.setFont(self.font())
        painter.setPen(self.palette().color(self.foregroundRole()))
        elided = self.fontMetrics().elidedText(
            self._full_text,
            Qt.TextElideMode.ElideRight,
            max(1, self.width()),
        )
        painter.drawText(
            self.rect(),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            elided,
        )


@dataclass
class _SisterRowControls:
    sister_id: str
    is_orchestrator: bool
    enabled_check: QCheckBox | None
    agent_combo: QComboBox
    model_combo: QComboBox
    effort_combo: QComboBox
    card: QFrame | None = None
    name_lbl: QLabel | None = None
    role_lbl: QLabel | None = None
    effort_supported: bool = True


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
        self._checkbox_style = DarkCheckBoxStyle()

        self.setWindowTitle("👥 碧オーケストレーター・姉妹別AI編成")
        self.resize(960, 680)
        self.setMinimumSize(860, 520)
        self.setStyleSheet(
            DARK_DIALOG_QSS
            + """
            QDialog {
                background-color: #0f1322;
            }
            QFrame#TeamTopCard {
                background-color: #171d35;
                border: 1px solid rgba(129, 140, 248, 0.38);
                border-radius: 12px;
            }
            QFrame#SisterRowCard {
                background-color: #161c32;
                border: 1px solid rgba(148, 163, 184, 0.16);
                border-radius: 10px;
            }
            QFrame#SisterRowCard:hover {
                border-color: rgba(129, 140, 248, 0.48);
                background-color: #1b223d;
            }
            QFrame#SisterRowCard[inactive="true"] {
                background-color: #121626;
                border-color: rgba(148, 163, 184, 0.08);
            }
            QFrame#CardDivider {
                background-color: rgba(148, 163, 184, 0.16);
                border: none;
            }
            QScrollArea#RosterScroll, QWidget#RosterBody {
                background-color: #0f1322;
                border: none;
            }
            QPushButton#BulkActionBtn {
                background-color: #1e2540;
                border: 1px solid rgba(148, 163, 184, 0.22);
                border-radius: 7px;
                padding: 4px 10px;
                font-size: 11.5px;
                color: #e2e8f0;
            }
            QPushButton#BulkActionBtn:hover {
                background-color: #2a3357;
                border-color: rgba(165, 180, 252, 0.55);
            }
            QPushButton#PrimarySaveBtn {
                background-color: #4f46e5;
                color: #ffffff;
                border: 1px solid #818cf8;
                border-radius: 8px;
                padding: 7px 16px;
                font-size: 12.5px;
                font-weight: 700;
            }
            QPushButton#PrimarySaveBtn:hover {
                background-color: #6366f1;
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
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(10)

        # Title header + active participant pill
        header_row = QHBoxLayout()
        header_row.setSpacing(10)
        title_box = QVBoxLayout()
        title_box.setContentsMargins(0, 0, 0, 0)
        title_box.setSpacing(2)

        title_lbl = QLabel("✨ 碧オーケストレーター · 姉妹別AI編成", self)
        title_lbl.setStyleSheet("font-size: 15px; font-weight: 700; color: #f8fafc; background: transparent;")
        subtitle_lbl = QLabel(
            "長女・碧が指揮者となり、参加中の姉妹へ個別AI・モデルで分担指揮します。",
            self,
        )
        subtitle_lbl.setStyleSheet("font-size: 11.5px; color: #94a3b8; background: transparent;")
        title_box.addWidget(title_lbl)
        title_box.addWidget(subtitle_lbl)
        header_row.addLayout(title_box, 1)

        self._active_count_lbl = QLabel("", self)
        self._active_count_lbl.setStyleSheet(
            "background-color: rgba(79, 70, 229, 0.34); color: #e0e7ff; "
            "border: 1px solid rgba(129, 140, 248, 0.55); border-radius: 11px; "
            "padding: 4px 11px; font-size: 11.5px; font-weight: 700;"
        )
        header_row.addWidget(self._active_count_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
        outer.addLayout(header_row)

        # Top card: Aoi + execution options
        top_card = QFrame(self)
        top_card.setObjectName("TeamTopCard")
        top_lay = QVBoxLayout(top_card)
        top_lay.setContentsMargins(12, 10, 12, 10)
        top_lay.setSpacing(9)

        aoi = next((s for s in self._sisters if s.id == "aoi"), self._sisters[0])
        top_lay.addWidget(self._create_sister_row(aoi, is_orchestrator=True, parent=top_card))

        divider = QFrame(top_card)
        divider.setObjectName("CardDivider")
        divider.setFixedHeight(1)
        top_lay.addWidget(divider)

        opts_row1 = QHBoxLayout()
        opts_row1.setSpacing(10)

        par_lbl = QLabel("同時実行数:", top_card)
        par_lbl.setStyleSheet("color: #cbd5e1; font-size: 11.5px; background: transparent;")
        self._max_parallel_combo = QComboBox(top_card)
        self._max_parallel_combo.setFixedWidth(185)
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

        self._confirm_plan_check = QCheckBox("実行前に碧の計画を確認してから動かす", top_card)
        self._confirm_plan_check.setChecked(bool(getattr(self._config, "orchestrator_confirm_plan", False)))
        opts_row1.addWidget(par_lbl)
        opts_row1.addWidget(self._max_parallel_combo)
        opts_row1.addSpacing(8)
        opts_row1.addWidget(self._confirm_plan_check, 1)
        top_lay.addLayout(opts_row1)

        opts_row2 = QHBoxLayout()
        opts_row2.setSpacing(8)
        self._switch_mascot_check = QCheckBox(
            "作業中の担当姉妹にマスコットを切り替える（セリフ・演出付き）",
            top_card,
        )
        self._switch_mascot_check.setChecked(bool(getattr(self._config, "orchestrator_switch_mascot", True)))
        opts_row2.addWidget(self._switch_mascot_check, 1)
        top_lay.addLayout(opts_row2)

        outer.addWidget(top_card)

        # Roster toolbar + aligned column headers
        worker_count = sum(1 for s in self._sisters if s.id != "aoi")
        toolbar_row = QHBoxLayout()
        toolbar_row.setContentsMargins(2, 2, 2, 0)
        toolbar_row.setSpacing(6)

        sec_lbl = QLabel(f"👥 担当シスターズ（{worker_count}名）", self)
        sec_lbl.setStyleSheet("font-size: 12.5px; font-weight: 700; color: #e2e8f0; background: transparent;")
        toolbar_row.addWidget(sec_lbl)
        toolbar_row.addStretch(1)

        for label, handler in (
            ("↻ モデル再取得", lambda: self._kickoff_missing_discovery(force=True)),
            ("全員 ON", self._enable_all_sisters),
            ("全員 OFF", self._disable_all_sisters),
            ("既定AIへ戻す", self._reset_all_to_default),
        ):
            btn = QPushButton(label, self)
            btn.setObjectName("BulkActionBtn")
            btn.clicked.connect(handler)
            toolbar_row.addWidget(btn)
        outer.addLayout(toolbar_row)

        hdr_row = QHBoxLayout()
        hdr_row.setContentsMargins(12, 2, 20, 0)
        hdr_row.setSpacing(10)
        for text, stretch, width in (
            ("参加 / 姉妹 · 役割", 1, 0),
            ("AIエージェント", 0, _AGENT_COMBO_WIDTH),
            ("モデル", 0, _MODEL_COMBO_WIDTH),
            ("エフォート", 0, _EFFORT_COMBO_WIDTH),
        ):
            lbl = QLabel(text, self)
            lbl.setStyleSheet("color: #94a3b8; font-size: 11px; font-weight: 700; background: transparent;")
            if width:
                lbl.setFixedWidth(width)
            hdr_row.addWidget(lbl, stretch)
        outer.addLayout(hdr_row)

        # Scrollable 16-sister roster
        scroll = QScrollArea(self)
        scroll.setObjectName("RosterScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.viewport().setStyleSheet("background-color: #0f1322;")

        roster_body = QWidget(scroll)
        roster_body.setObjectName("RosterBody")
        roster_body.setStyleSheet("background-color: #0f1322;")
        roster_lay = QVBoxLayout(roster_body)
        roster_lay.setContentsMargins(0, 0, 8, 6)
        roster_lay.setSpacing(6)

        for sister in self._sisters:
            if sister.id == "aoi":
                continue
            row_card = QFrame(roster_body)
            row_card.setObjectName("SisterRowCard")
            row_lay = QVBoxLayout(row_card)
            row_lay.setContentsMargins(10, 8, 10, 8)
            row_lay.addWidget(
                self._create_sister_row(sister, is_orchestrator=False, parent=row_card, card=row_card)
            )
            roster_lay.addWidget(row_card)

        roster_lay.addStretch(1)
        scroll.setWidget(roster_body)
        outer.addWidget(scroll, 1)

        # Footer action bar
        footer_row = QHBoxLayout()
        footer_row.setSpacing(10)
        note_lbl = QLabel("※「既定」を選ぶとパネル上部の選択AI・モデルを引き継ぎます", self)
        note_lbl.setStyleSheet("color: #94a3b8; font-size: 11px; background: transparent;")
        footer_row.addWidget(note_lbl, 1)

        cancel_btn = QPushButton("キャンセル", self)
        cancel_btn.clicked.connect(self.reject)
        save_btn = QPushButton("💾 設定を保存", self)
        save_btn.setObjectName("PrimarySaveBtn")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._on_save)
        footer_row.addWidget(cancel_btn)
        footer_row.addWidget(save_btn)
        outer.addLayout(footer_row)

        apply_dark_checkbox_style(self, self._checkbox_style)
        self._update_active_count()

    def _create_sister_row(
        self,
        sister: SisterProfile,
        *,
        is_orchestrator: bool,
        parent: QWidget,
        card: QFrame | None = None,
    ) -> QWidget:
        row_widget = QWidget(parent)
        row_widget.setStyleSheet("background: transparent;")
        lay = QHBoxLayout(row_widget)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        assignments = getattr(self._config, "sister_assignments", {}) or {}
        assignment = assignments.get(sister.id)
        is_enabled = True if is_orchestrator else (bool(assignment.enabled) if assignment is not None else True)

        enabled_check: QCheckBox | None = None
        if is_orchestrator:
            badge = QLabel("👑 統括", row_widget)
            badge.setFixedWidth(62)
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setStyleSheet(
                "background-color: rgba(79, 70, 229, 0.55); color: #ffffff; "
                "border: 1px solid #818cf8; border-radius: 7px; "
                "font-size: 11px; font-weight: 700; padding: 3px 6px;"
            )
            lay.addWidget(badge)
        else:
            enabled_check = QCheckBox(row_widget)
            enabled_check.setChecked(is_enabled)
            enabled_check.setToolTip(f"{_short_name(sister.display_name)}をオーケストレーターの委譲先にする")
            lay.addWidget(enabled_check)

        accent_bar = QFrame(row_widget)
        accent_bar.setFixedSize(4, 32)
        accent_bar.setStyleSheet(
            f"background-color: {sister.accent_color}; border-radius: 2px; border: none;"
        )
        lay.addWidget(accent_bar)

        info_box = QVBoxLayout()
        info_box.setContentsMargins(0, 0, 0, 0)
        info_box.setSpacing(2)
        name_suffix = "（オーケストレーター）" if is_orchestrator else ""
        name_lbl = _ElidedLabel(
            f"{sister.rank_label} · {_short_name(sister.display_name)}{name_suffix}",
            row_widget,
        )
        role_lbl = _ElidedLabel(sister.role, row_widget)
        role_lbl.setToolTip(sister.role)
        role_lbl.setStyleSheet("font-size: 10.5px; color: #94a3b8; background: transparent;")
        info_box.addWidget(name_lbl)
        info_box.addWidget(role_lbl)
        lay.addLayout(info_box, 1)

        agent_combo = QComboBox(row_widget)
        agent_combo.setFixedWidth(_AGENT_COMBO_WIDTH)
        agent_combo.addItem(f"既定 ({self._config.agent_id})", "")
        for aid in AGENT_IDS:
            agent_combo.addItem(self._agent_label(aid), aid)
        saved_aid = (assignment.agent_id if assignment is not None else "") or ""
        a_idx = agent_combo.findData(saved_aid)
        agent_combo.setCurrentIndex(a_idx if a_idx >= 0 else 0)

        model_combo = QComboBox(row_widget)
        model_combo.setFixedWidth(_MODEL_COMBO_WIDTH)
        model_combo.setEditable(True)
        model_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        model_combo.view().setMinimumWidth(340)
        if model_combo.lineEdit() is not None:
            model_combo.lineEdit().setPlaceholderText(_DEFAULT_MODEL_LABEL)

        effort_combo = QComboBox(row_widget)
        effort_combo.setFixedWidth(_EFFORT_COMBO_WIDTH)
        effort_combo.setToolTip("思考エフォート（推論の深さ）")
        effort_combo.view().setMinimumWidth(165)

        controls = _SisterRowControls(
            sister_id=sister.id,
            is_orchestrator=is_orchestrator,
            enabled_check=enabled_check,
            agent_combo=agent_combo,
            model_combo=model_combo,
            effort_combo=effort_combo,
            card=card,
            name_lbl=name_lbl,
            role_lbl=role_lbl,
        )
        self._rows[sister.id] = controls

        saved_model = (assignment.model if assignment is not None else "") or ""
        saved_effort = normalize_effort(getattr(assignment, "effort", "") if assignment is not None else "")
        self._populate_row_models(sister.id, preserve_model=saved_model)
        self._populate_row_efforts(sister.id, preserve_effort=saved_effort)
        agent_combo.currentIndexChanged.connect(
            lambda _idx, sid=sister.id: self._on_row_agent_changed(sid)
        )
        model_combo.currentIndexChanged.connect(
            lambda _idx, sid=sister.id, mc=model_combo: self._on_row_model_changed(sid, mc)
        )
        if enabled_check is not None:
            enabled_check.toggled.connect(
                lambda checked, sid=sister.id: self._on_row_enabled_toggled(sid, checked)
            )
        self._apply_row_visual_state(controls, is_enabled)

        lay.addWidget(agent_combo)
        lay.addWidget(model_combo)
        lay.addWidget(effort_combo)
        return row_widget

    @staticmethod
    def _sync_model_combo_display(combo: QComboBox) -> None:
        if combo.lineEdit() is not None:
            combo.lineEdit().setCursorPosition(0)
            combo.lineEdit().deselect()
        combo.setToolTip(combo.currentText().strip() or _DEFAULT_MODEL_LABEL)

    def _apply_row_visual_state(self, ctrl: _SisterRowControls, checked: bool) -> None:
        ctrl.agent_combo.setEnabled(checked)
        ctrl.model_combo.setEnabled(checked)
        ctrl.effort_combo.setEnabled(checked and ctrl.effort_supported)
        if ctrl.name_lbl is not None:
            color = "#f8fafc" if checked else "#64748b"
            ctrl.name_lbl.setStyleSheet(
                f"font-weight: 700; font-size: 12px; color: {color}; background: transparent;"
            )
        if ctrl.role_lbl is not None:
            r_color = "#94a3b8" if checked else "#475569"
            ctrl.role_lbl.setStyleSheet(f"font-size: 10.5px; color: {r_color}; background: transparent;")
        if ctrl.card is not None:
            ctrl.card.setProperty("inactive", not checked)
            ctrl.card.style().unpolish(ctrl.card)
            ctrl.card.style().polish(ctrl.card)

    def _on_row_enabled_toggled(self, sister_id: str, checked: bool) -> None:
        ctrl = self._rows.get(sister_id)
        if ctrl is not None:
            self._apply_row_visual_state(ctrl, checked)
        self._update_active_count()

    def _update_active_count(self) -> None:
        worker_rows = [ctrl for ctrl in self._rows.values() if not ctrl.is_orchestrator]
        total = len(worker_rows)
        active = sum(
            1
            for ctrl in worker_rows
            if ctrl.enabled_check is None or ctrl.enabled_check.isChecked()
        )
        self._active_count_lbl.setText(f"参加中 {active} / {total}名")

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
            self._sync_model_combo_display(combo)
        finally:
            self._updating_rows = False

    def _populate_row_efforts(
        self,
        sister_id: str,
        *,
        preserve_effort: str | None = None,
    ) -> None:
        ctrl = self._rows.get(sister_id)
        if ctrl is None:
            return
        target_effort = normalize_effort(
            ctrl.effort_combo.currentData() if preserve_effort is None else preserve_effort
        )
        effective_agent = self._effective_agent_for_row(ctrl)
        effective_model = self._current_row_model_value(ctrl) or (
            (self._config.models_by_agent.get(effective_agent, "") or "").strip()
        )
        adapter = self._router.get_adapter(effective_agent)
        levels = list(adapter.supported_efforts(effective_model))
        checked_state = ctrl.enabled_check.isChecked() if ctrl.enabled_check is not None else True

        combo = ctrl.effort_combo
        self._updating_rows = True
        try:
            combo.clear()
            if not levels:
                ctrl.effort_supported = False
                is_oc_empty = effective_agent == "opencode" and not effective_model
                label = "（モデル指定要）" if is_oc_empty else "（非対応）"
                combo.addItem(label, "")
                combo.setCurrentIndex(0)
            else:
                ctrl.effort_supported = True
                hint = adapter.default_effort_hint(effective_model)
                def_label = (
                    f"（既定: {EFFORT_SHORT_LABELS.get(hint, hint)}）"
                    if hint
                    else _DEFAULT_EFFORT_LABEL
                )
                combo.addItem(def_label, "")
                for lv in levels:
                    combo.addItem(EFFORT_DISPLAY_LABELS.get(lv, lv), lv)
                if target_effort and target_effort in levels:
                    idx = combo.findData(target_effort)
                    combo.setCurrentIndex(idx if idx >= 0 else 0)
                else:
                    combo.setCurrentIndex(0)
        finally:
            self._updating_rows = False
        combo.setEnabled(checked_state and ctrl.effort_supported)

    def _on_row_model_changed(self, sister_id: str, combo: QComboBox) -> None:
        self._sync_model_combo_display(combo)
        if not self._updating_rows:
            self._populate_row_efforts(sister_id)

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
        self._populate_row_efforts(sister_id)
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
                self._populate_row_efforts(sid)

    def _enable_all_sisters(self) -> None:
        for ctrl in self._rows.values():
            if ctrl.enabled_check is not None:
                ctrl.enabled_check.setChecked(True)

    def _disable_all_sisters(self) -> None:
        for ctrl in self._rows.values():
            if ctrl.enabled_check is not None:
                ctrl.enabled_check.setChecked(False)

    def _reset_all_to_default(self) -> None:
        self._updating_rows = True
        try:
            for ctrl in self._rows.values():
                ctrl.agent_combo.setCurrentIndex(0)
        finally:
            self._updating_rows = False
        for sid in list(self._rows.keys()):
            self._populate_row_models(sid, preserve_model="")
            self._populate_row_efforts(sid, preserve_effort="")

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
            effort = normalize_effort(ctrl.effort_combo.currentData())
            enabled = True if ctrl.enabled_check is None else bool(ctrl.enabled_check.isChecked())
            if aid or model or effort or not enabled:
                updated[sid] = SisterAgentAssignment(
                    agent_id=aid,
                    model=model,
                    effort=effort,
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
