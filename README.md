# 💠 Sisters Mascot（シスターズ・デスクトップマスコット＆ランチャー）

美緒ちゃんをいつも隣で優しく支える「シスターズ（17姉妹）」がデスクトップに常駐し、複数の AI CLI エージェント（`agy` / `claude` / `codex` / `opencode`）との会話、Google Chrome のブラウザ操作（Playwright MCP）、音声対話（Edge-TTS / VOICEVOX / PipeWire 音声入力）、アプリ・フォルダ・Webブックマークのランチャーをひとつにまとめた **軽量 PyQt6 製デスクトップマスコットアプリ** です。

---

## ✨ 概要・アーキテクチャ

- **軽量常駐設計**: PyQt6 + `mpv`（または `ffplay`）+ PipeWire（`pw-record` / `parecord`）+ `Edge-TTS` / `SpeechRecognition` を組み合わせたシンプル構成で、**待機時のメモリ使用量 80MB 未満**の軽快さで動作します。
- **17姉妹プロファイル連動**: `/home/dma/sisters-wiki/docs/agents/*.md` から長女・碧（`aoi`）から十七女・焔（`homura`）まで全17姉妹の性格・口調・役割を自動読み込みします。
- **単一インスタンス動作**: 既に起動中に再度 `sisters-mascot` を実行すると、既存ウィンドウが前面化されチャット＆ランチャーパネルを開閉します。

---

## 🎨 立ち絵（スプライト）とカスタム設定

### 収録立ち絵 & ちびクレストカード
- **透過 PNG 立ち絵収録（12姉妹）**:
  `aoi`（碧）、`ruri`（瑠璃）、`kohaku`（琥珀）、`sumire`（菫）、`akari`（灯）、`shion`（紫苑）、`hiiro`（緋色）、`mashiro`（真白）、`yui`（結）、`ritsu`（律）、`nagi`（凪）、`akane`（茜）の12名は透過済み PNG 立ち絵を標準収録しています。
- **ちびクレストカード描画（5姉妹）**:
  `kokonoe`（九重）、`towa`（永遠）、`aya`（彩）、`kanon`（花音）、`homura`（焔）の5名は、それぞれの姉妹カラーとモノグラムをあしらった高 DPI ちびメダリオン（クレストカード）として美しく描画されます。

### カスタム立ち絵の追加・差し替え方法
美緒ちゃんお気に入りの立ち絵（PNG / WebP / JPG）を簡単に設定できます。
1. **アプリ内の `⚙️ 設定` タブからインポート**:
   `🎨 立ち絵管理` セクションから状態（`idle` / `happy` / `thinking` / `talking` / `dragged`）を選んで画像を選択すると、自動で高さ 600px 以内にリサイズして反映されます。
2. **フォルダへ直接配置**:
   以下のパスに PNG または WebP を置くだけでも自動認識されます（標準立ち絵より優先されます）。
   ```text
   ~/.config/sisters-mascot/sprites/<sister_id>/
   ├── idle.png      # 通常待機
   ├── happy.png     # クリック・音声認識中など
   ├── thinking.png  # AI思考中・ツール実行中
   ├── talking.png   # 音声読み上げ中
   └── dragged.png   # ドラッグ移動中
   ```

---

## 🚀 起動方法と操作方法

### 起動方法
- **コマンドから起動**（PATH が通っている場合）:
  ```bash
  sisters-mascot
  ```
- **リポジトリから直接起動**:
  ```bash
  /home/dma/プロジェクト/sisters-mascot/run.sh
  ```
- **KDE アプリケーションランチャーから起動**:
  メニューから **`Sisters Mascot`** を検索して起動できます（`~/.local/share/applications/sisters-mascot.desktop`）。

> **KDE Plasma Wayland / XWayland について**:
> `run.sh` は `DISPLAY` が有効かつ `SISTERS_MASCOT_PLATFORM` が未設定のとき、`QT_QPA_PLATFORM=xcb`（XWayland）を優先して透明ウィンドウの自由ドラッグと座標復元を有効にします。純粋な Wayland バックエンドで試す場合は `SISTERS_MASCOT_PLATFORM=wayland sisters-mascot` で起動できます。

### マウス・ドラッグ・右クリック操作
- **左クリック**: 姉妹が可愛くバウンスしてセリフを喋り、チャット＆ランチャーパネルを開閉します。
- **左ドラッグ**: デスクトップ上の好きな位置へ自由に移動できます（移動後の座標は自動保存されます）。
- **ホバー（マウスオーバー）**: 足元にミニバーが表示され、`💬 チャット＆ランチャー`・`🎤 音声入力`・`🔊 読み上げ ON/OFF` をワンクリックで切り替えられます。
- **右クリックメニュー**:
  - `💬 チャット＆ランチャー` を開く
  - `👯 姉妹を切り替え`（17姉妹から選択。立ち絵ありは `✨`、クレストカードは `💠` 表示）
  - `🤖 AIエージェント` 切替（`agy` / `claude` / `codex` / `opencode`）
  - `🔊 音声読み上げ (ON/OFF)` / `📌 常に最前面に表示` / `📍 画面右下に戻す` / `✖ 終了`

---

## 🤖 AIエージェント連携 & Google Chrome 操作

パネル上部のセレクタから **4つの AI CLI エージェント** を瞬時に切り替えられます。

| エージェント ID | 対応 CLI | 特徴 |
| :--- | :--- | :--- |
| **`agy`** | Antigravity CLI | シスターズ既定の統合エージェント |
| **`claude`** | Claude Code | Anthropic の CLI エージェント |
| **`codex`** | OpenAI Codex | OpenAI の CLI エージェント |
| **`opencode`** | OpenCode | 高速・多モデル対応の CLI エージェント |

- **モデル切り替え**: パネル上部のモデル選択欄から `↻` ボタンで最新モデル一覧を取得したり、使いたいモデル名を直接指定できます（設定はエージェントごとに `~/.config/sisters-mascot/config.json` に保存されます）。
- **Google Chrome 操作（Playwright MCP `--extension` モード）**:
  各エージェントの Playwright MCP（`--extension` モード）と連携しており、AI が `browser_` 系ツールを呼び出すとマスコットが **`🌐 Chrome操作中…`** のステータスと専用オーラに切り替わります。
  - `🚀 ランチャー` タブの定型プロンプト（「開いているページを調査」「ChromeでWeb調査」）や、Webブックマークの右クリックメニュー `🌐 Playwrightで調査` からすぐに Chrome 調査を始められます。

---

## 🔊 音声設定（TTS / STT）

`⚙️ 設定` タブの **`🔊 音声設定 (TTS / STT)`** から調整できます。

- **音声読み上げ (TTS)**:
  - **Edge-TTS（標準）**: `edge-tts` + `mpv`（または `ffplay`）を使用し、17姉妹それぞれに合わせた日本語ニューラル音声（`ja-JP-NanamiNeural` 等）とピッチ・話速で読み上げます。音量・話速補正・最大文字数・コードブロック読み上げスキップ・クリック時ボイスを設定可能です。
  - **VOICEVOX（任意）**: ローカル等で VOICEVOX サーバー（既定 `http://127.0.0.1:50021`）を起動している場合は、エンジンを VOICEVOX に切り替えて好みの話者 ID で読み上げられます。
  - **OFF**: 読み上げを無効化します。
- **音声入力 (STT)**:
  - PipeWire の `pw-record`（または `parecord`）で録音し、`Google音声認識 (ja-JP)`（`SpeechRecognition`）またはカスタムコマンドで文字起こしします。認識結果をそのまま自動送信するオプションもあります。

---

## 📦 依存パッケージ

- Python 3.11+（仮想環境 `.venv`）
- `PyQt6>=6.7.0`、`PyYAML>=6.0`
- 音声機能: `edge-tts>=6.1.0`、`SpeechRecognition>=3.10.0`
- 外部ツール: `mpv`（または `ffplay`）、`pw-record`（または `parecord`）

```bash
/home/dma/プロジェクト/sisters-mascot/.venv/bin/pip install -r /home/dma/プロジェクト/sisters-mascot/requirements.txt
```

---

## 🗂️ ディレクトリ構成

```text
sisters-mascot/
├── assets/
│   ├── app-icon.png
│   └── sprites/<sister_id>/idle.png
├── scripts/
│   └── remove_bg.py
├── sisters_mascot/
│   ├── agents/          # agy / claude / codex / opencode アダプタとルーター
│   ├── audio/           # Edge-TTS / VOICEVOX / PipeWire録音・STT
│   ├── launcher/        # .desktop 検出とアプリ/フォルダ/URL起動
│   ├── sprites/         # 透過立ち絵 & 高DPIちびクレストカード描画
│   ├── ui/              # MascotWindow / BubblePanel / Toast / Tray
│   ├── config.py        # ~/.config/sisters-mascot/config.json 管理
│   ├── sisters_loader.py
│   └── main.py
├── tests/
├── run.sh
├── requirements.txt
└── README.md
```
