# 💠 Sisters Mascot（シスターズ・デスクトップマスコット＆ランチャー）

美緒ちゃんをいつも隣で優しく支える「シスターズ（17姉妹）」がデスクトップに常駐し、複数の AI CLI エージェント（`agy` / `claude` / `codex` / `opencode`）との会話、Google Chrome のブラウザ操作（Playwright MCP）、音声対話（Edge-TTS / VOICEVOX / PipeWire 音声入力）、アプリ・フォルダ・Webブックマークのランチャーをひとつにまとめた **軽量 PyQt6 製デスクトップマスコットアプリ** です。

![Sisters Mascot 実行画面スクリーンショット](docs/images/app-screenshot.png)

---

## ✨ 概要・アーキテクチャ

- **軽量常駐設計**: PyQt6 + `mpv`（または `ffplay`）+ PipeWire（`pw-record` / `parecord`）+ `Edge-TTS` / `SpeechRecognition` を組み合わせたシンプル構成で、**待機時のメモリ使用量 80MB 未満**の軽快さで動作します。
- **17姉妹プロファイル連動**: `~/sisters-wiki/docs/agents/*.md`（環境変数 `SISTERS_WIKI_AGENTS_DIR` で上書き可）から長女・碧（`aoi`）から十七女・焔（`homura`）まで全17姉妹の性格・口調・役割を読み込みます。`sisters-wiki` が無い PC でも内蔵プロファイルへ自動フォールバックします。
- **単一インスタンス動作**: 既に起動中に再度 `sisters-mascot` を実行すると、既存ウィンドウが前面化されチャット＆ランチャーパネルを開閉します。

---

## 🎨 立ち絵（スプライト）とカスタム設定

### 全17姉妹 × 5状態（計85枚）透過PNG立ち絵を標準収録
- **全17姉妹・全5状態の立ち絵を同梱**:
  長女・碧（`aoi`）から十七女・焔（`homura`）まで、全17姉妹それぞれの最新デザインによる **340×600px RGBA 透過 PNG 立ち絵（`idle` / `happy` / `thinking` / `talking` / `dragged` の5状態・計85枚）** を標準収録しています。
- **自然な切り替え＆快適な操作性**:
  状態切り替え時のキャラクター縮尺差や水平・足元基準を自動補正して位置ズレ・大きさの違和感を抑え、透過シルエットに基づいた当たり判定により透明余白での誤クリックを防ぎつつ快適にクリック・ドラッグできます。

![全17姉妹キャラクター一覧](docs/images/sisters-gallery.png)

![5状態アニメーション＆表情バリエーション](docs/images/states-showcase.png)

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
  ./run.sh
  ```
- **KDE アプリケーションランチャーから起動**:
  メニューから **`Sisters Mascot`** を検索して起動できます（`~/.local/share/applications/sisters-mascot.desktop`）。

> **KDE Plasma Wayland / XWayland について**:
> `run.sh` は `DISPLAY` が有効な環境（X11 / XWayland）では **`xcb` を優先**します。デスクトップマスコットに必要な「常に最前面・自由な位置配置・吹き出しの位置合わせ」は X11 系のウィンドウ管理機能で、ネイティブ Wayland ではコンポジターの制限によりクライアントから制御できないためです。`DISPLAY` が無い純粋な Wayland セッションでは `wayland` にフォールバックします。明示指定は `SISTERS_MASCOT_PLATFORM=wayland|xcb` または `QT_QPA_PLATFORM` で常に優先されます。
>
> **日本語入力（Mozc / fcitx5）**: pip 版 PyQt6 は独自の Qt プラグインを同梱しており、システムの fcitx5 プラグインを検索しないため、そのままでは xcb で日本語入力できません。本アプリは起動時にシステムの Qt6 プラグインディレクトリを `QT_PLUGIN_PATH` へ追加し（`main._ensure_system_qt_plugins`）、`QT_IM_MODULE=fcitx` で fcitx5 の Mozc が動作するようにしています。ネイティブ Wayland 起動時は Qt の text-input プロトコルを使うため `QT_IM_MODULE` を自動解除します（維持したい場合は `SISTERS_MASCOT_KEEP_IM_MODULE=1`）。
>
> **Wayland の制限**: ネイティブ Wayland ではウィンドウ位置はコンポジター管理のため、**座標の保存・復元や吹き出しの精密な位置合わせは行いません**（ドラッグは `startSystemMove` で動作）。最前面・位置・吹き出しを完全に使うには XWayland（`xcb`）起動を推奨します。X11 / Windows / macOS ではマスコット・パネルそれぞれの位置とサイズを保存・復元します。

### マウス・ドラッグ・ホイール・右クリック操作
- **左クリック**: 姉妹が弾性バウンス＆星やハートのパーティクルとともに挨拶し、チャット＆ランチャーパネルを開閉します。
- **左ドラッグ**: 振り子のような揺れ反応を見せながら好きな位置へ移動できます（X11 などでは移動後の座標は自動保存されます）。
- **マウスホイール**: マスコット上でホイールを回すと、足元を基準にサイズ（`100〜420px`）をスムーズに拡大・縮小できます。
- **パネルのリサイズ**: チャット＆ランチャーパネルの右下グリップをドラッグするとウィンドウサイズを自由に変更・保存できます（最小 `360×440`、最大は画面の表示可能領域いっぱい）。
- **マスコットとパネルは独立**: マスコットをドラッグしてもチャットパネルは追従しません。パネルは初回表示時のみマスコット横に配置され、以降は自分自身の位置を記憶します（再表示しても勝手に再配置されません）。ヘッダー（上部）のドラッグでパネル単体を移動できます。
- **日本語入力（Mozc など）**: IME 変換中の Enter は変換確定として扱われ、メッセージは誤送信されません。変換確定後の Enter で送信、`Shift+Enter` で改行です。
- **ホバー（マウスオーバー）**: カーソル方向へ少し傾きつつ足元にミニバーが表示され、`💬 チャット＆ランチャー`・`🎤 音声入力`・`🔊 読み上げ ON/OFF` をワンクリックで切り替えられます。
- **アニメーション設定 & GIF対応**: `⚙️ 設定` タブから **`✨ アニメーション`**（`リッチ` / `控えめ` / `OFF`）を切り替え可能。また、立ち絵フォルダに `.gif` を置くとアニメーション再生にも対応します。
- **右クリックメニュー**:
  - `💬 チャット＆ランチャー` を開く
  - `👯 姉妹を切り替え`（長女・碧〜十七女・焔の全17姉妹から選択）
  - `🤖 AIエージェント` 切替（`agy` / `claude` / `codex` / `opencode`）
  - `📏 サイズ変更`（`極小 130px` 〜 `特大 340px` / `+25px` / `-25px`）
  - `🔊 音声読み上げ (ON/OFF)` / `📌 常に最前面に表示` / `📍 画面右下に戻す` / `✖ 終了`

---

## 🤖 AIエージェント連携 & Google Chrome 操作

パネル上部のセレクタから **4つの AI CLI エージェント** を瞬時に切り替えられます。

| エージェント ID | 対応 CLI | モデル動的取得元 |
| :--- | :--- | :--- |
| **`agy`** | Antigravity CLI | `agy models` 出力 |
| **`claude`** | Claude Code | `~/.claude/settings*.json`・環境変数・公式エイリアス |
| **`codex`** | OpenAI Codex | `~/.codex/models_cache.json`・`config.toml` |
| **`opencode`** | OpenCode | `opencode models` 出力 |

- **動的モデル取得＆切り替え**: 起動時およびエージェント切り替え時に各 CLI の利用可能モデル一覧をバックグラウンドで自動取得してプルダウンへ反映します。`↻` ボタンでの強制再取得や自由入力も可能です（選択はエージェントごとに `~/.config/sisters-mascot/config.json` に保存されます）。
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
- 外部ツール: `mpv`（または `ffplay`）、Linux の録音用 `pw-record`（または `parecord`）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

---

## 🌍 別の環境（他Linux・メインPC / macOS / Windows）での使い方

特定のユーザー名や固定ホームディレクトリ（`/home/dma` 等）に依存しないポータブル構成になっているため、GitHub から clone して別の PC でも動かせます。

### 別 PC でのセットアップ手順

```bash
git clone https://github.com/dma-cmyk/sisters-mascot.git
cd sisters-mascot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./run.sh
```

> Windows（PowerShell）から起動する場合:
> ```powershell
> python -m venv .venv
> .venv\Scripts\pip install -r requirements.txt
> .venv\Scripts\python -m sisters_mascot.main
> ```

### OS・環境ごとの対応状況

| 機能 | Linux（他PC・メインPC含む） | macOS / Windows |
| :--- | :--- | :--- |
| **マスコット UI・アニメーション・サイズ変更** | ◎（XWayland / Wayland / X11） | ○（PyQt6 透明ウィンドウとして動作） |
| **17姉妹プロファイル** | ◎（`~/sisters-wiki` が無くても内蔵定義で動作） | ○（同左・`SISTERS_WIKI_AGENTS_DIR` 指定可） |
| **AIチャット & 動的モデル取得** | ◎（導入済み CLI を自動検出） | ○（導入済み CLI を PATH から検出） |
| **音声読み上げ（TTS）** | ◎（`edge-tts` + `mpv` / `ffplay`） | ○（`mpv` または `ffplay` を PATH に導入） |
| **アプリ・フォルダ・URLランチャー** | ◎（`.desktop` 一覧 + `xdg-open`） | ○（`open` / `os.startfile` / 既定ブラウザ） |
| **音声入力（STT / マイク録音）** | ◎（PipeWire `pw-record` / `parecord`） | △（既定録音は Linux 用。`⚙️ 設定` でカスタムSTT等を併用） |
| **Chrome 操作（Playwright MCP）** | ◎（各 CLI に設定済みの MCP をそのまま利用） | ○（同上・利用環境側の Playwright MCP 設定に依存） |

---

## 🗂️ ディレクトリ構成

```text
sisters-mascot/
├── assets/
│   ├── app-icon.png
│   └── sprites/<sister_id>/
│       ├── idle.png
│       ├── happy.png
│       ├── thinking.png
│       ├── talking.png
│       └── dragged.png
├── docs/
│   └── images/          # README掲載スクリーンショット・17姉妹一覧画像
├── scripts/
│   └── remove_bg.py
├── sisters_mascot/
│   ├── agents/          # agy / claude / codex / opencode アダプタとルーター
│   ├── audio/           # Edge-TTS / VOICEVOX / PipeWire録音・STT
│   ├── launcher/        # .desktop 検出とアプリ/フォルダ/URL起動
│   ├── sprites/         # 状態別アライメント補正付き透過PNGレンダラー
│   ├── ui/              # MascotWindow / BubblePanel / Toast / Tray
│   ├── config.py        # ~/.config/sisters-mascot/config.json 管理
│   ├── sisters_loader.py
│   └── main.py
├── tests/
├── run.sh
├── requirements.txt
└── README.md
```
