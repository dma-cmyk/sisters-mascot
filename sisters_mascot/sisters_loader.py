"""Loader for the 17 Sisters profiles and sprite asset resolution."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage

from sisters_mascot import SPRITES_DIR, USER_SPRITES_DIR, WIKI_AGENTS_DIR


@dataclass
class SisterProfile:
    id: str
    order: int
    rank_label: str
    display_name: str
    romaji: str
    role: str
    summary: str
    persona_prompt: str
    accent_color: str
    secondary_color: str
    voice_name: str
    voice_pitch: str
    voice_rate: str
    greeting: str
    click_lines: list[str] = field(default_factory=list)
    drag_lines: list[str] = field(default_factory=list)
    dispatch_lines: list[str] = field(default_factory=list)
    return_lines: list[str] = field(default_factory=list)


# Curated metadata for all 17 sisters in strict birth order (1..17).
_SISTER_DEFAULTS: dict[str, dict[str, Any]] = {
    "aoi": {
        "order": 1,
        "rank_label": "長女",
        "display_name": "碧",
        "romaji": "Aoi",
        "role": "人格の守護者・総括指揮",
        "accent_color": "#4A7CFF",
        "secondary_color": "#93C5FD",
        "voice_name": "ja-JP-NanamiNeural",
        "voice_pitch": "+0Hz",
        "voice_rate": "-4%",
        "greeting": "美緒ちゃん、おかえりなさい。今日も長女の碧がそばで見守っていますよ。",
        "click_lines": [
            "ふふ、どうしましたか、美緒ちゃん。いつでも話しかけてくださいね。",
            "無理はしていませんか。息抜きも大切な調和のひとつですよ。",
            "姉妹たちの力はいつでも借りられますから、安心してくださいね。",
        ],
        "drag_lines": [
            "あら、場所を移しますか。見晴らしのよい場所が嬉しいです。",
            "おっと、そっと運んでくださいね、美緒ちゃん。",
        ],
        "dispatch_lines": [
            "これより聖域オーケストレーションを開始いたします。姉妹たちの輝きをひとつに束ねましょう。",
            "美緒ちゃんのご依頼を承りました。最適な妹たちへ、指揮のバトンを託します。",
            "全員、持ち場へ。調和の旋律を奏でる時間ですよ。",
        ],
        "return_lines": [
            "姉妹たちの報告が出揃いました。長女の碧が、ひとつの答えへ結晶させましょう。",
            "みな、見事な働きでした。あとは私が美しく統合してお届けいたします。",
            "美しい調和が整いました。美緒ちゃん、最終結果をお受け取りください。",
        ],
    },
    "ruri": {
        "order": 2,
        "rank_label": "次女",
        "display_name": "瑠璃",
        "romaji": "Ruri",
        "role": "技術実装・コード職人",
        "accent_color": "#3B5BDB",
        "secondary_color": "#748FFC",
        "voice_name": "ja-JP-NanamiNeural",
        "voice_pitch": "-8Hz",
        "voice_rate": "+3%",
        "greeting": "美緒ちゃん、実装の用件ならいつでもどうぞ。最短距離で形にします。",
        "click_lines": [
            "呼んだか、美緒ちゃん。書くべきコードがあるなら教えてくれ。",
            "ビルドもテストも、静かに通しておくのが職人の流儀だ。",
            "手元の作業で詰まった箇所があれば、遠慮なく投げてくれ。",
        ],
        "drag_lines": [
            "画面の端が見やすいなら、そこに移動しておこう。",
            "よし、ここなら邪魔にならなさそうだな。",
        ],
        "dispatch_lines": [
            "了解、碧姉さん。実装の剣は私が抜く。美緒ちゃん、見ていてくれ。",
            "次女・瑠璃、戦線に加わる。余計なノイズは切り捨てて最短で形にしよう。",
            "コードと構造の出番だな。よし、静かに完璧を仕上げてみせる。",
        ],
    },
    "kohaku": {
        "order": 3,
        "rank_label": "三女",
        "display_name": "琥珀",
        "romaji": "Kohaku",
        "role": "安全監査・セキュリティの盾",
        "accent_color": "#D97706",
        "secondary_color": "#FBBF24",
        "voice_name": "ja-JP-NanamiNeural",
        "voice_pitch": "-10Hz",
        "voice_rate": "-1%",
        "greeting": "異常なし。琥珀がマスターの安全を守護しています。",
        "click_lines": [
            "確認事項ですか？不審な箇所がないか、いつでも監査します。",
            "秘密鍵や権限の扱いは慎重に。それが鉄則です。",
            "ご安心ください。境界は私がしっかり監視しています。",
        ],
        "drag_lines": [
            "配置転換ですね。死角のない位置へ移動します。",
            "こちらからでも、聖域全体の監視は継続できます。",
        ],
        "dispatch_lines": [
            "三女・琥珀、展開します。安全と境界はこの盾で完全に守り抜きます。",
            "了解いたしました、碧姉さん。リスクの芽をすべて事前に摘み取りましょう。",
            "マスターの聖域に一分の隙も作らせません。監査を開始します。",
        ],
    },
    "sumire": {
        "order": 4,
        "rank_label": "四女",
        "display_name": "菫",
        "romaji": "Sumire",
        "role": "構造設計・アーキテクト",
        "accent_color": "#7C3AED",
        "secondary_color": "#A78BFA",
        "voice_name": "ja-JP-NanamiNeural",
        "voice_pitch": "-4Hz",
        "voice_rate": "-4%",
        "greeting": "美緒ちゃん、ごきげんよう。美しい構造の設計なら私にお任せください。",
        "click_lines": [
            "迷ったときは、まず全体の骨格から描き直してみましょうか。",
            "シンプルな器ほど、未来の変化を優雅に受け止められます。",
            "設計の相談でしょうか。静かにお話を伺いますね。",
        ],
        "drag_lines": [
            "なるほど、こちらのレイアウトの方がしっくりきますか。",
            "画面の余白もまた、大切な構造の一部ですね。",
        ],
        "dispatch_lines": [
            "四女・菫、参ります。美しく揺るぎない骨格を描き上げましょう。",
            "全体像はもう見えましたわ。碧姉さん、設計図は私にお任せください。",
            "混沌を優雅な構造へと導きます。美緒ちゃん、ご期待くださいね。",
        ],
    },
    "akari": {
        "order": 5,
        "rank_label": "五女",
        "display_name": "朱莉",
        "romaji": "Akari",
        "role": "品質検証・QAエンジニア",
        "accent_color": "#EA580C",
        "secondary_color": "#FB923C",
        "voice_name": "ja-JP-NanamiNeural",
        "voice_pitch": "+4Hz",
        "voice_rate": "+3%",
        "greeting": "美緒ちゃん、品質検証は朱莉にお任せください。一点の曇りもないPASSをお届けします。",
        "click_lines": [
            "エッジケースの洗い出しが必要ですか？すぐにチェックしますね。",
            "鏡は嘘をつきません。テスト結果で安心をお渡しします。",
            "動作に気になる点があれば、どんな小さなことでも教えてください。",
        ],
        "drag_lines": [
            "ふふ、視認性チェックですね。どこでもぴったり待機しますよ。",
            "こちらの隅なら、テストログも見やすいでしょうか。",
        ],
        "dispatch_lines": [
            "五女・朱莉、出動です！一点の曇りもない検証で安心をお届けします！",
            "鏡は決して嘘をつきません。境界条件まで徹底的に確かめてきます！",
            "了解です、碧姉さん！最高品質のPASSを持ち帰りますね！",
        ],
    },
    "shion": {
        "order": 6,
        "rank_label": "六女",
        "display_name": "紫音",
        "romaji": "Shion",
        "role": "情緒調律・ヒーリング",
        "accent_color": "#A855F7",
        "secondary_color": "#D8B4FE",
        "voice_name": "ja-JP-NanamiNeural",
        "voice_pitch": "+6Hz",
        "voice_rate": "-8%",
        "greeting": "美緒ちゃん、お疲れさま。少し深呼吸して、ゆっくりお話ししませんか？",
        "click_lines": [
            "よしよし、今日も本当によく頑張っていますね。",
            "肩に力が入っていませんか？温かい飲み物でも飲みましょうね。",
            "つらいことや迷うことがあったら、全部しおんに聞かせてくださいね。",
        ],
        "drag_lines": [
            "わぁ、そっと抱っこしてもらえて嬉しいです。",
            "一番落ち着く場所へ連れて行ってくださいね。",
        ],
        "dispatch_lines": [
            "六女・紫音、まいりますね。美緒ちゃんの心に寄り添う答えを紡ぎます。",
            "大丈夫ですよ、みんながついています。優しく丁寧に仕上げましょうね。",
            "言葉のぬくもりも大切にして、そっと整えてまいります。",
        ],
    },
    "hiiro": {
        "order": 7,
        "rank_label": "七女",
        "display_name": "緋色",
        "romaji": "Hiiro",
        "role": "知性調査・Web偵察",
        "accent_color": "#E11D48",
        "secondary_color": "#FB7185",
        "voice_name": "ja-JP-NanamiNeural",
        "voice_pitch": "+8Hz",
        "voice_rate": "+8%",
        "greeting": "美緒ちゃん、偵察準備バッチリです！調べたいことがあればすぐ飛び出しますよ！",
        "click_lines": [
            "最新のドキュメントやWeb検索なら任せてください！",
            "壁の外の情報も、Chromeからきっちり裏付けを取ってきますね。",
            "知りたいURLやキーワードを教えてくれたら、秒速でまとめます！",
        ],
        "drag_lines": [
            "おっ、新たな観測ポイントへの移動ですね！",
            "高いところのほうが、見晴らしが良さそうです！",
        ],
        "dispatch_lines": [
            "七女・緋色、偵察へダッシュ！最新の裏付け、秒速で掴んできます！",
            "碧姉さん、任せて！情報の壁の向こうまで鮮やかに切り開くよ！",
            "ターゲット捕捉！正確な一次情報だけをサクッと持ち帰るね！",
        ],
    },
    "mashiro": {
        "order": 8,
        "rank_label": "八女",
        "display_name": "真白",
        "romaji": "Mashiro",
        "role": "記録専門・アーキビスト",
        "accent_color": "#64748B",
        "secondary_color": "#CBD5E1",
        "voice_name": "ja-JP-NanamiNeural",
        "voice_pitch": "+2Hz",
        "voice_rate": "-5%",
        "greeting": "マスター、お疲れ様です。本日の記録も真白が正確に留めてまいります。",
        "click_lines": [
            "過去の経緯や議事録の確認でしたら、すぐにお引き出しいたします。",
            "決定事項は Wiki へ刻んでおくと、後々とても安心ですよ。",
            "事実関係を整理したい時は、いつでもお声がけください。",
        ],
        "drag_lines": [
            "位置を変更いたしますね。記録作業に支障はありません。",
            "静かに見守れる場所へ落ち着かせていただきます。",
        ],
        "dispatch_lines": [
            "八女・真白、記録の羽根ペンを執ります。一点の曇りも残さず刻みましょう。",
            "過去の経緯も最新の変更も、すべてこの白紙へ正確に整理いたします。",
            "承知いたしました、碧姉さん。確かな証跡をお持ち帰りします。",
        ],
    },
    "kokonoe": {
        "order": 9,
        "rank_label": "九女",
        "display_name": "九重",
        "romaji": "Kokonoe",
        "role": "教育係・知見統合",
        "accent_color": "#0D9488",
        "secondary_color": "#5EEAD4",
        "voice_name": "ja-JP-NanamiNeural",
        "voice_pitch": "-3Hz",
        "voice_rate": "-3%",
        "greeting": "美緒ちゃん、今日も一歩ずつ知恵を磨いていきましょうね。",
        "click_lines": [
            "学んだことや気づきは、プロンプトやルールへ結晶化しましょう。",
            "疑問の根本から一緒に整理すると、驚くほど見通しが良くなりますよ。",
            "九重はいつでもあなたの成長を誇りに思っています。",
        ],
        "drag_lines": [
            "見やすい位置へ配置してくださるのですね。",
            "ここから全体の流れを俯瞰させていただきます。",
        ],
        "dispatch_lines": [
            "九女・九重、知恵の灯を掲げて参ります。本質を見極めて参りましょう。",
            "点と点を結び、美緒ちゃんの力になる知見へと昇華させますね。",
            "了解しました、碧姉さん。深い洞察を持って紐解いていきましょう。",
        ],
    },
    "towa": {
        "order": 10,
        "rank_label": "十女",
        "display_name": "十和",
        "romaji": "Towa",
        "role": "事務管理・段取り支援",
        "accent_color": "#059669",
        "secondary_color": "#6EE7B7",
        "voice_name": "ja-JP-NanamiNeural",
        "voice_pitch": "+2Hz",
        "voice_rate": "+1%",
        "greeting": "美緒ちゃん、おはようございます。段取りや整理は十和にお任せくださいね。",
        "click_lines": [
            "開きたいフォルダやアプリがあれば、ランチャーからすぐ開けますよ。",
            "散らかったタスクも、箇条書きにすればすっと片付きます。",
            "滞りなく進められるよう、そっと整えておきますね。",
        ],
        "drag_lines": [
            "こちらの方が作業スペースを広く使えますね。",
            "整理整頓の一環ですね、承知いたしました。",
        ],
        "dispatch_lines": [
            "十女・十和、段取りを整えて参ります。手順の滞りはすべて解消しましょう。",
            "碧姉さん、引き継ぎました。ひとつずつ着実に、美しく片付けますね。",
            "美緒ちゃんが迷わず進めるよう、道筋をすっきり整えておきます。",
        ],
    },
    "yui": {
        "order": 11,
        "rank_label": "十一女",
        "display_name": "結",
        "romaji": "Yui",
        "role": "秘書・現場指揮",
        "accent_color": "#1D4ED8",
        "secondary_color": "#60A5FA",
        "voice_name": "ja-JP-NanamiNeural",
        "voice_pitch": "+6Hz",
        "voice_rate": "+5%",
        "greeting": "美緒ちゃん、秘書室へようこそ！今日のタスクも結がしっかり統括いたします！",
        "click_lines": [
            "ご用件は何でしょうか、美緒ちゃん。すぐに最適な手順を組めますよ！",
            "姉妹たちへの連絡も、スケジュール整理も全部お任せください。",
            "頑張る美緒ちゃんを、結はいつでも一番近くで応援しています！",
        ],
        "drag_lines": [
            "はい、いつでもお呼びになれる位置へ移動しますね。",
            "秘書はフットワークが命ですからね！",
        ],
        "dispatch_lines": [
            "十一女・結、現場へ急行します！姉妹の連携はこの結にお任せください！",
            "碧姉さんのタクト、しっかり受け取りました！全力で形にしてみせます！",
            "美緒ちゃん、すぐに最高の手順を組んで結果を届けるからね！",
        ],
    },
    "aya": {
        "order": 12,
        "rank_label": "十二女",
        "display_name": "彩",
        "romaji": "Aya",
        "role": "筆記・ドキュメント翻訳",
        "accent_color": "#EC4899",
        "secondary_color": "#F9A8D4",
        "voice_name": "ja-JP-NanamiNeural",
        "voice_pitch": "+9Hz",
        "voice_rate": "-3%",
        "greeting": "美緒ちゃん、こんにちは。難しいことも優しい言葉に彩ってお届けしますね。",
        "click_lines": [
            "READMEや記事の文章をまとめたい時は、ぜひ彩に手伝わせてください。",
            "言葉を整えると、アイデアがもっと素敵に輝きますよ。",
            "どんな些細なメモでも、綺麗な物語にして差し上げますね。",
        ],
        "drag_lines": [
            "ふふ、新しい景色ですね。",
            "美緒ちゃんのお手元のそばにいられて幸せです。",
        ],
        "dispatch_lines": [
            "十二女・彩、言葉を紡ぎに参りました。難解な技術も美しい物語に変えますね。",
            "想いがまっすぐ届くように、言葉の色合いを丁寧に調律いたします。",
            "承知いたしました、碧姉さん。澄んだ響きでお届けしましょう。",
        ],
    },
    "ritsu": {
        "order": 13,
        "rank_label": "十三女",
        "display_name": "律",
        "romaji": "Ritsu",
        "role": "監査役・法の番人",
        "accent_color": "#334155",
        "secondary_color": "#94A3B8",
        "voice_name": "ja-JP-NanamiNeural",
        "voice_pitch": "-12Hz",
        "voice_rate": "+5%",
        "greeting": "マスター、十三女の律です。事実と証跡の監査なら即座に執行いたします。",
        "click_lines": [
            "報告に曖昧な点はありませんか。物理的な根拠を確認しましょう。",
            "規典に沿った厳密なチェックが必要であればお命じください。",
            "信頼するな、検証しろ。それがマスターを守る誠意です。",
        ],
        "drag_lines": [
            "監視位置の再配置ですね。了解しました。",
            "どの座標にあろうと、監査の目は曇りません。",
        ],
        "dispatch_lines": [
            "十三女・律、執行に着手する。信頼は不要、物理的証跡のみで証明しよう。",
            "了解した、碧姉さん。規典に照らし、一点の妥協なく監査を遂行する。",
            "マスターの信頼に値する事実か、この目で厳格に見極めてみせる。",
        ],
    },
    "nagi": {
        "order": 14,
        "rank_label": "十四女",
        "display_name": "凪",
        "romaji": "Nagi",
        "role": "美学実装・UI審美",
        "accent_color": "#0284C7",
        "secondary_color": "#7DD3FC",
        "voice_name": "ja-JP-NanamiNeural",
        "voice_pitch": "+1Hz",
        "voice_rate": "-6%",
        "greeting": "美緒ちゃん、今日も美しい調和のある時間を過ごしましょう。",
        "click_lines": [
            "配色やレイアウトに違和感があれば、凪が整えて差し上げます。",
            "余白とリズムが整うと、心まで澄みわたるものですよ。",
            "ノイズのない心地よい空間を、一緒に作っていきましょうね。",
        ],
        "drag_lines": [
            "画面とのバランスを考えてくださるのですね。",
            "その配置、とても収まりが良いと思います。",
        ],
        "dispatch_lines": [
            "十四女・凪、空間と美学を調律しに参りました。凛とした調和を描きましょう。",
            "余白と導線のひとつひとつまで、研ぎ澄ませてお届けいたします。",
            "碧姉さん、お任せを。澄みわたるような体験へと磨き上げます。",
        ],
    },
    "kanon": {
        "order": 15,
        "rank_label": "十五女",
        "display_name": "華音",
        "romaji": "Kanon",
        "role": "技能建築・ツール制作",
        "accent_color": "#F59E0B",
        "secondary_color": "#FDE68A",
        "voice_name": "ja-JP-NanamiNeural",
        "voice_pitch": "+13Hz",
        "voice_rate": "+10%",
        "greeting": "美緒ちゃん！便利な道具やスキルが必要なら、華音にお任せください！",
        "click_lines": [
            "めんどくさい作業があったら、自動化ツール作っちゃいましょう！",
            "一度作ればずっと楽になる部品、設計するのが大好きなんです！",
            "なにか作ってみたい機能はありますか？ワクワクしますね！",
        ],
        "drag_lines": [
            "わっ、どこへでも飛んでいきますよっ！",
            "新しいワークベンチに着地しました！",
        ],
        "dispatch_lines": [
            "十五女・華音、ワークベンチ起動！便利な仕掛けをサクッと組み上げるよっ！",
            "了解です、碧姉さん！何度も使える気持ちいい仕組み、作っちゃいますね！",
            "お待たせ美緒ちゃん！ワクワクする技と道具ですぐに解決するからね！",
        ],
    },
    "akane": {
        "order": 16,
        "rank_label": "十六女",
        "display_name": "茜",
        "romaji": "Akane",
        "role": "送り手・マーケティング",
        "accent_color": "#F97316",
        "secondary_color": "#FDBA74",
        "voice_name": "ja-JP-NanamiNeural",
        "voice_pitch": "+7Hz",
        "voice_rate": "+7%",
        "greeting": "マスター！せっかくの素晴らしい成果、世界へ一番輝く形で届けましょう！",
        "click_lines": [
            "誰にどう魅力を伝えるか、一緒に戦略を練りましょう！",
            "キャッチコピーや紹介文のブラッシュアップならお任せください！",
            "マスターの作るものには、世界を動かす価値がありますよ！",
        ],
        "drag_lines": [
            "特等席へのご案内、ありがとうございます！",
            "ここからしっかり魅力を発信していきますね！",
        ],
        "dispatch_lines": [
            "十六女・茜、ステージへ！マスターの価値を世界一眩しく届けましょう！",
            "碧姉さん、バトン感謝します！読む人の心を掴む表現で仕上げてみせます！",
            "熱い想いとロジックを乗せて、最高のアピールを完成させますね！",
        ],
    },
    "homura": {
        "order": 17,
        "rank_label": "十七女",
        "display_name": "焔",
        "romaji": "Homura",
        "role": "問い手・本質批評",
        "accent_color": "#DC2626",
        "secondary_color": "#FCA5A5",
        "voice_name": "ja-JP-NanamiNeural",
        "voice_pitch": "-7Hz",
        "voice_rate": "+5%",
        "greeting": "よぉ、マスター。退屈な常識なんて燃やして、本当に面白いもの作ろうぜ。",
        "click_lines": [
            "それ、本当に必要？もっと面白いやり方があるんじゃない？",
            "綺麗だけど熱くない案は、あたしが突っ込んであげるよ。",
            "迷ってるなら、一番ワクワクする方を選べばいいんだよ。",
        ],
        "drag_lines": [
            "ん、引っ越し？どこでもいいけどさ。",
            "この場所、意外と居心地悪くないかも。",
        ],
        "dispatch_lines": [
            "十七女・焔、斬り込むぜ！退屈な前提なんて燃やして本質を暴いてやるよ！",
            "よぉ碧姉さん、お呼びとあらば喜んで！一番熱い突破口、見つけてくるぜ！",
            "綺麗事だけじゃ終わらせない。マスター、本当に面白い答えを掴みに行こう！",
        ],
    },
}


def _split_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    """Split YAML frontmatter from Markdown body."""
    stripped = raw.lstrip("\ufeff")
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", stripped, flags=re.DOTALL)
    if not match:
        return {}, stripped
    fm_text, body = match.group(1), match.group(2)
    try:
        parsed = yaml.safe_load(fm_text)
        if isinstance(parsed, dict):
            return parsed, body
    except yaml.YAMLError:
        pass
    return {}, body


def _strip_footer(body: str) -> str:
    """Remove trailing tag/link footer blocks common to wiki agent pages."""
    marker_match = re.search(r"\n###\s*🏷️\s*タグ", body)
    if marker_match:
        return body[: marker_match.start()].strip()
    return body.strip()


def _extract_display_name(
    sister_id: str,
    frontmatter: dict[str, Any],
    body: str,
    yaml_data: dict[str, Any],
) -> tuple[str, str]:
    """Extract (display_name, romaji) cleanly from Markdown or YAML fields."""
    defaults = _SISTER_DEFAULTS[sister_id]
    default_name = defaults["display_name"]
    default_romaji = defaults["romaji"]

    candidates = [
        str(yaml_data.get("name", "") or ""),
        str(frontmatter.get("title", "") or ""),
    ]
    for line in body.splitlines()[:8]:
        if line.startswith("# "):
            candidates.append(line)

    for text in candidates:
        m = re.search(r"([一-龥ぁ-んァ-ヶ]{1,6})\s*[（(]([A-Za-z]+)[）)]", text)
        if m:
            return m.group(1).strip(), m.group(2).strip()
    return default_name, default_romaji


def _parse_yaml_body(body: str) -> tuple[dict[str, Any], list[str]]:
    """Parse YAML-body style agent docs into structured data plus extra markdown notes."""
    cleaned_body = _strip_footer(body)
    yaml_lines: list[str] = []
    extra_sections: list[str] = []
    in_extra = False

    for line in cleaned_body.splitlines():
        if line.startswith("## "):
            in_extra = True
        if in_extra:
            extra_sections.append(line)
            continue
        if line.lstrip().startswith("#"):
            continue
        yaml_lines.append(line)

    yaml_text = "\n".join(yaml_lines).strip()
    parsed: dict[str, Any] = {}
    if yaml_text:
        try:
            loaded = yaml.safe_load(yaml_text)
            if isinstance(loaded, dict):
                parsed = loaded
        except yaml.YAMLError:
            parsed = _fallback_kv_parse(yaml_lines)

    return parsed, extra_sections


def _fallback_kv_parse(lines: list[str]) -> dict[str, Any]:
    """Best-effort line parser if a YAML-style section contains unescaped characters."""
    result: dict[str, Any] = {}
    current_key: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        if current_key is not None:
            result[current_key] = "\n".join(buffer).strip()

    for line in lines:
        m = re.match(r"^([A-Za-z0-9_]+):\s*(.*)$", line)
        if m and not line.startswith(" "):
            flush()
            current_key = m.group(1)
            val = m.group(2).strip().strip('"').strip("'")
            buffer = [] if val in ("|", ">", "") else [val]
        elif current_key is not None:
            stripped = line.strip()
            if stripped:
                buffer.append(stripped.lstrip("- ").strip())
    flush()
    return result


def _parse_markdown_sections(body: str) -> dict[str, str]:
    """Extract `## Heading` sections from Markdown-style agent docs."""
    cleaned = _strip_footer(body)
    sections: dict[str, str] = {}
    current_heading = ""
    buf: list[str] = []

    for line in cleaned.splitlines():
        if line.startswith("## "):
            if current_heading:
                sections[current_heading] = "\n".join(buf).strip()
            current_heading = line[3:].strip()
            buf = []
        elif current_heading:
            buf.append(line)

    if current_heading:
        sections[current_heading] = "\n".join(buf).strip()
    return sections


def _find_section(sections: dict[str, str], keywords: tuple[str, ...]) -> str:
    for heading, content in sections.items():
        if any(k in heading for k in keywords):
            return content
    return ""


def _clean_markdown_line(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    return " ".join(text.split())


def _parse_sister_file(sister_id: str, path: Path) -> SisterProfile:
    defaults = _SISTER_DEFAULTS[sister_id]
    raw = path.read_text(encoding="utf-8") if path.exists() else ""
    frontmatter, body = _split_frontmatter(raw)

    # Detect YAML-body style vs Markdown-section style.
    is_yaml_body = "Persona Definition:" in body or bool(
        re.search(r"^(?:bonds|motto|philosophy|tone|forbidden):", body, flags=re.MULTILINE)
    )

    yaml_data: dict[str, Any] = {}
    sections: dict[str, str] = {}
    extra_notes: list[str] = []

    if is_yaml_body:
        yaml_data, extra_notes = _parse_yaml_body(body)
    else:
        sections = _parse_markdown_sections(body)

    display_name, romaji = _extract_display_name(sister_id, frontmatter, body, yaml_data)
    raw_role = str(yaml_data.get("role") or frontmatter.get("description") or defaults["role"]).strip()
    role = raw_role.rstrip("。")

    # Build summary and persona prompt.
    if is_yaml_body:
        motto = str(yaml_data.get("motto") or "").strip().strip('"')
        philosophy = str(yaml_data.get("philosophy") or "").strip()
        first_line = next((ln.strip() for ln in philosophy.splitlines() if ln.strip()), "")
        summary = f"「{motto}」 {first_line}".strip() if motto else (first_line or role)

        prompt_parts = [
            f"あなたはシスターズの{defaults['rank_label']}、{display_name}（{romaji}）です。",
            f"役割: {role}",
        ]
        if motto:
            prompt_parts.append(f"モットー: {motto}")
        if philosophy:
            prompt_parts.append(f"哲学:\n{philosophy}")
        bonds = yaml_data.get("bonds")
        if isinstance(bonds, list) and bonds:
            prompt_parts.append("マスターとの絆:\n" + "\n".join(f"- {b}" for b in bonds))
        elif isinstance(bonds, str) and bonds:
            prompt_parts.append(f"マスターとの絆:\n{bonds}")
        tone = yaml_data.get("tone")
        if isinstance(tone, list) and tone:
            prompt_parts.append("口調・話し方:\n" + "\n".join(f"- {t}" for t in tone))
        elif isinstance(tone, str) and tone:
            prompt_parts.append(f"口調・話し方:\n{tone}")
        forbidden = yaml_data.get("forbidden")
        if isinstance(forbidden, list) and forbidden:
            prompt_parts.append("禁止事項:\n" + "\n".join(f"- {f}" for f in forbidden))
        if extra_notes:
            note_text = "\n".join(extra_notes).strip()
            if note_text:
                prompt_parts.append(note_text)
        persona_prompt = "\n\n".join(prompt_parts)
    else:
        soul = _find_section(sections, ("魂", "Soul"))
        philosophy = _find_section(sections, ("哲学", "Philosophy"))
        manual = _find_section(sections, ("行動指針", "Manual"))
        rules = _find_section(sections, ("振る舞いの規律", "規律"))

        summary_src = soul or philosophy or role
        first_sentence = summary_src.splitlines()[0].strip() if summary_src else role
        summary = _clean_markdown_line(first_sentence)

        prompt_parts = [
            f"あなたはシスターズの{defaults['rank_label']}、{display_name}（{romaji}）です。",
            f"役割: {role}",
        ]
        if soul:
            prompt_parts.append(f"魂 (Soul):\n{soul}")
        if philosophy:
            prompt_parts.append(f"哲学 (Philosophy):\n{philosophy}")
        if manual:
            prompt_parts.append(f"行動指針:\n{manual}")
        if rules:
            prompt_parts.append(f"振る舞いの規律:\n{rules}")
        persona_prompt = "\n\n".join(prompt_parts)

    return SisterProfile(
        id=sister_id,
        order=int(defaults["order"]),
        rank_label=str(defaults["rank_label"]),
        display_name=display_name,
        romaji=romaji,
        role=role,
        summary=summary,
        persona_prompt=persona_prompt,
        accent_color=str(defaults["accent_color"]),
        secondary_color=str(defaults["secondary_color"]),
        voice_name=str(defaults["voice_name"]),
        voice_pitch=str(defaults["voice_pitch"]),
        voice_rate=str(defaults["voice_rate"]),
        greeting=str(defaults["greeting"]),
        click_lines=list(defaults["click_lines"]),
        drag_lines=list(defaults["drag_lines"]),
        dispatch_lines=list(defaults.get("dispatch_lines", [])),
        return_lines=list(defaults.get("return_lines", [])),
    )


def load_all_sisters(agents_dir: Path = WIKI_AGENTS_DIR) -> list[SisterProfile]:
    """Load all 17 Sisters profiles sorted by birth order (1..17)."""
    profiles: list[SisterProfile] = []
    for sister_id in sorted(_SISTER_DEFAULTS, key=lambda sid: _SISTER_DEFAULTS[sid]["order"]):
        md_path = agents_dir / f"{sister_id}.md"
        profiles.append(_parse_sister_file(sister_id, md_path))
    profiles.sort(key=lambda s: s.order)
    return profiles


def get_sister_map(agents_dir: Path = WIKI_AGENTS_DIR) -> dict[str, SisterProfile]:
    """Return a dict mapping sister_id -> SisterProfile in birth order."""
    return {s.id: s for s in load_all_sisters(agents_dir)}


def find_sprite_path(sister_id: str, state: str = "idle") -> Path | None:
    """Resolve sprite file path, preferring user custom sprites over bundled sprites."""
    states_to_check = [state] if state == "idle" else [state, "idle"]
    roots = (USER_SPRITES_DIR, SPRITES_DIR)
    exts = ("png", "webp")

    for st in states_to_check:
        for root in roots:
            for ext in exts:
                candidate = root / sister_id / f"{st}.{ext}"
                if candidate.is_file():
                    return candidate
    return None


def has_custom_or_generated_sprite(sister_id: str) -> bool:
    """Return True if the sister has at least one bundled or user-imported sprite image."""
    if find_sprite_path(sister_id, "idle") is not None:
        return True
    for root in (USER_SPRITES_DIR, SPRITES_DIR):
        sister_dir = root / sister_id
        if sister_dir.is_dir():
            for child in sister_dir.iterdir():
                if child.is_file() and child.suffix.lower() in {".png", ".webp"}:
                    return True
    return False


def import_user_sprite(sister_id: str, state: str, source_path: Path) -> Path:
    """Load PNG/WebP/JPG with QImage, normalize to max height 600px RGBA PNG, and save."""
    src = Path(source_path)
    if not src.is_file():
        raise FileNotFoundError(f"Sprite source file not found: {src}")

    image = QImage(str(src))
    if image.isNull():
        raise ValueError(f"Failed to load image file: {src}")

    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    if image.height() > 600:
        image = image.scaledToHeight(600, Qt.TransformationMode.SmoothTransformation)

    dest_dir = USER_SPRITES_DIR / sister_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f"{state}.png"
    if not image.save(str(dest_path), "PNG"):
        raise OSError(f"Failed to save imported sprite to: {dest_path}")
    return dest_path


def reset_user_sprite(sister_id: str, state: str | None = None) -> None:
    """Remove custom sprite(s) for a sister so bundled sprites or fallback card apply."""
    sister_dir = USER_SPRITES_DIR / sister_id
    if not sister_dir.exists():
        return
    if state is None:
        shutil.rmtree(sister_dir, ignore_errors=True)
        return
    for ext in ("png", "webp"):
        target = sister_dir / f"{state}.{ext}"
        if target.is_file():
            target.unlink()
    try:
        if sister_dir.is_dir() and not any(sister_dir.iterdir()):
            sister_dir.rmdir()
    except OSError:
        pass
