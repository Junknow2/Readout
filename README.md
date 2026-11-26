# 🏃 Readout (リードアウト)

マラソンや屋外ランニング中に「耳で聴いて知識をインプットする」ためのパーソナル音声ニュース・ポッドキャスト生成プラットフォーム。

Web上の記事（技術ブログ、note、海外テックニュース等）のURLや**PDFドキュメント（学術論文、技術レポート、調査資料等）**を入力・アップロードすると、本文抽出 ➜ Gemini 3.8 Flash による耳用リライト・翻訳 ➜ **ローカル音声合成「VOICEPEAK」（ナレーター: 女性2 / Japanese Female 2）**による圧倒的に自然でクリアなナレーション音声（192kbps MP3）を非同期自動生成（Edge-TTS 自動フォールバック対応）。屋外や直射日光下でも操作しやすい**高視認性PWA（Progressive Web App）**および**Pocket Casts等で購読できるプライベートPodcast RSSフィード**を提供します。

---

## 🚀 主な機能と特徴

1. **Gemini 3.8 Flash ネイティブ PDF 解析 ＆ 音声化 (`poc_pipeline.py`, `main.py`)**:
   - **ドラッグ＆ドロップ ＆ スマホファイル選択**: PWA画面の専用エリアへPDFファイルをドロップ、またはタップして端末内のPDFを選択するだけでワンステップ登録。
   - **PDF 直接URL対応**: `.pdf` で終わるURLや `application/pdf` のURLも `/api/queue` 経由で自動判別・直接ダウンロードしてパイプラインへ投入。
   - **Gemini 3.8 Flash マルチモーダルネイティブ解析**: `google-genai` SDK の `types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")` を採用。外部OCRや重厚なPDF解析ライブラリ不要で、英語論文や複雑な2段組・数式・図表の文脈を直接高精度に理解。
   - **学術・技術資料用リライトプロンプト**:
     - 脚注（Footnotes）、ヘッダー、フッター、ページ番号、図表注釈、参考文献リスト（References / Bibliography）を【完全に無視・除外】。
     - ラジオ風挨拶を徹底排除し、「{タイトル}についての解説。」から即座に本題へ導入。
     - 2段組やページ跨ぎの文をシームレスに結合し、自然な日本語「です・ます調」のナレーション原稿として出力。
   - **VOICEPEAK（女性2）高音質朗読**: 生成された原稿を120文字チャンクに最適分割し、VOICEPEAK で美しい日本語音声化 ➜ ffmpeg で 192kbps MP3 に結合。

2. **FastAPI バックエンド & 非同期パイプライン (`main.py`, `queue_manager.py`)**:
   - 内部ポート: `127.0.0.1:8002`
   - FastAPI BackgroundTasks による非同期音声生成（キュー投入後即レスポンス）。
   - キュー管理（`data/queue.json`）による状態永続化（`pending` ➜ `processing` ➜ `ready` ➜ `listened`）。
   - PDFアップロード: `POST /api/upload-pdf`（アップロードされたファイルは `data/pdfs/` へ保存、削除時に自動クリーンアップ）。
   - PDF配信: `GET /pdf/{id}.pdf` で原本PDFの確認・ダウンロードが可能。
   - 音声ストリーミング: `GET /audio/{filename}`（HTTP Range 206 Partial Content に対応し、シーク再生も滑らか）。
   - プライベート Podcast RSS: `GET /feed.xml`（Pocket Casts や Apple Podcasts で直接購読可能）。

2. **内蔵 RSS リーダー ＆ 1タップ音声キュー追加 (`feed_manager.py`)**:
   - 購読フィード管理（`data/feeds.json`）による RSS/Atom フィードの永続化。
   - デフォルトで「The GitHub Blog」「Zennのトレンド」等の良質テックフィードをプリセット。
   - 並行フェッチ（ThreadPoolExecutor）による高速新着記事アグリゲーション。
   - タイムラインから**「🎧 走るリストに追加」を1タップ**するだけで、自動で本文抽出・AIリライト・TTS音声化キューへ投入（「⏳ 生成中」➜「✓ キュー追加済」へ状態遷移）。

3. **ランナー特化・完全オフライン対応 PWA (`templates/index.html`, `static/`)**:
   - **タブ切り替えバー**: `[ 🏃 再生キュー ]` と `[ 📰 RSSリーダー ]` をシームレスに行き来可能。
   - **高コントラスト・ダークテーマ**: 屋外直射日光下やOLED画面でも見やすい漆黒（`#0A0A0E`）× ネオンイエロー（`#CCFF00`）。
   - **大型タッチUI**: 揺れや手袋着用時でも誤タップしない特大再生ボタン、10秒戻し（-10s）/ 30秒送り（+30s）ボタン。
   - **再生速度変更**: 1.0x / 1.25x / 1.5x / 2.0x をワンタップ切り替え。
   - **Service Worker (`sw.js`) による完全オフライン再生**:
     - 静的アセットキャッシュに加え、生成済み MP3 音声を CacheStorage に自動事前キャッシュ。
     - 地下鉄や電波圏外のランニングコースでもキャッシュから途切れず再生可能。
     - CacheStorage からの Range リクエスト（206 Partial Content）ハンドリングを実装。
   - **MediaSession API 完全連動**:
     - Android / Pixel のロック画面ウィジェット、通知コントロール、および骨伝導イヤホン（Shokz等）の物理ボタン（Play, Pause, Next, Prev, Seek）と同期。

4. **複数ページ・連載コラム記事の自動検出 ＆ 1本化マージ (`poc_pipeline.py`)**:
   - 日経BP、東洋経済、ダイヤモンド、ITmedia 等の複数ページ分割記事（`?P=2` や `<link rel="next">`、ページネーション）や連載コラム（`data-content-type="nextArticle"` 等）を自動検出。
   - 最大10ページまで安全に再帰・反復クロールし、同一ドメイン・同一パス整合性検証および無限ループ防止ガードを適用。
   - ページ間の重複タイトルやフッターノイズを自動除去し、Gemini 3.8 Flash で1本の自然なポッドキャスト原稿としてリライト ➜ 1つの高品質音声（MP3）に統合生成。
   - PWAのキュー画面にも「全Nページ結合」バッジを自動表示。

5. **最高峰ローカル音声合成「VOICEPEAK」（女性2）＆ 安定化・エンジン可視化 (`poc_pipeline.py`)**:
   - 音声エンジンとしてローカルに導入された「VOICEPEAK」（ナレーター: `Japanese Female 2` / 女性2）を標準採用。クラウドTTSを凌駕する自然なアクセント・滑らかな抑揚を実現。
   - **インテリジェント・センテンス分割 ＆ 長文合成安定化**: VOICEPEAK CLI の文字数上限（140文字/回）を克服するため文境界で120文字前後へ自動分割。チャンク間のクールダウン待機（0.4秒）と、一時的なアクセス違反（`code 3221225477`）や多重起動ガード（`code 1`）に対する**チャンク単位の自動リトライ（最大3回、1.5秒→3秒バックオフ）**により長文でも安定完走。
   - **英単語・アルファベットの徹底カタカナ化**: Web記事・PDF・Curator の全原稿生成プロンプトで英単語・略語・媒体名・人名を自然なカタカナ読みへ変換し、さらに TTS 直前の軽量サニタイザー（`AI`→`エーアイ`, `ROI`→`アールオーアイ`, `KPI`→`ケーピーアイ`, `LLM`→`エルエルエム`, `MIT`→`エムアイティー`, `P&G`→`ピーアンドジー`, `RIETI`→`リエティ` 等）を安全ネットとして適用。
   - **ffmpeg concat 192kbps エンコード**: 生成されたチャンク WAV 群を ffmpeg の concat demuxer で無劣化・シームレスに結合し、192kbps MP3 として高速エンコード。作業後の一時ファイルは自動安全消去。
   - **自動フォールバック ＆ 使用エンジンバッジ表示**: VOICEPEAK 未検出時やリトライ上限超過時は `edge_tts`（`ja-JP-NanamiNeural`）へ自動フォールバック。実際に使用したエンジン（`tts_engine`）を各アイテム・Curator パートへ記録し、UI 上に控えめなバッジ（`🎙️ VOICEPEAK` / `🔊 Edge-TTS`）を表示。

6. **Personal Knowledge Curator（AI 知的キュレーション ＆ 深夜事前音声化 ＆ フィードバック学習ループ）(`curator_sources.py`, `curator_engine.py`, `run_curator_daily.bat`)**:
   - **多ソース巡回 ＆ 厳格なハルシネーション/ペイウォール排除**: Knowledge at Wharton、MIT Sloan Management Review、RIETI、Behavioral Scientist、The Conversation、Greenbook、Quirk's、ESOMAR、Our World in Data、Works in Progress、NBER、J-STAGE API、OpenAlex API、さらに Gemini Google Search Grounding を組み合わせ、全候補を Python 側で実アクセス・本文検証（404・ログイン壁・ペイウォール・本文不足を機械的に除外し Research Log へ記録）。
   - **3 独立探索 ＆ 7 軸加重評価 ＆ 枠別選考**: Core / Discovery / Wild Card の 3 トラックで独立評価（**Wild Card には過去の好みプロファイルを一切渡さず過剰適合を構造的に防止**）。共通テーマに無理やり寄せず、各枠単体で充実した全文論考記事（`free_full_text`）を優先し、7 軸加重スコア（問いの深さ 20%、説明力 20%、根拠の質 20%、独自性 15%、長期的価値 10%、好奇心 10%、音声適性 5%）と関心適合度・新規性から枠別選考。
   - **独立 3 パート編成 ＆ 7 ステップ音声原稿**: Part 1（Core Deep Dive）、Part 2（Discovery Deep Dive）、Part 3（Wild Card）の独立 3 パートを 7 ステップ構成・英単語カタカナ化済みの自然な日本語ナレーション原稿として生成（過去ログの旧 `part4_connections` も後方互換表示に対応）。
   - **毎日 03:00 自動事前音声化 ＆ 逐次公開**: Windows タスクスケジューラ `ReadoutDailyCurator` が毎日 `03:00` に起動し、VOICEPEAK（女性2）で 1 パート完了するごとに `data/curator/daily_YYYY-MM-DD.json` へ即時公開（使用エンジン `tts_engine` も記録）。14 日より前の未採用 MP3 は自動削除（JSON・原稿・Research Log・履歴は永久保存）。
   - **待ち時間 0 秒の即キュー投入 ＆ 5 段階フィードバック**: `✨ Curator` タブからワンタップで事前音声化済み MP3 を即座にメイン再生キューへ追加（`status="ready"`、`tts_engine` 引き継ぎ）。5 段階評価＋理由チップ＋任意メモで Core 関心プロファイルを継続学習。Research Log の不採用記事も「＋ 自分で聴く」で通常キューへ投入可能。

---

## 📁 ディレクトリ構成

```text
Readout/
├── data/
│   ├── queue.json         # 再生キュー永続化ストア
│   ├── feeds.json         # 購読RSSフィード設定
│   ├── curator/           # Personal Knowledge Curator 成果物 (daily_*.json, scripts_*.txt, research_log_*.json, history.json, audio/)
│   ├── pdfs/              # アップロードされた原本PDFファイル保管
│   └── logs/              # バックグラウンド起動・Curator日次ログ
├── output/                # 生成された MP3 および テキスト原稿
├── static/
│   ├── css/
│   │   └── style.css      # ランナー特化ハイコントラスト CSS (タブ・Curator・PDF・RSS含む)
│   ├── js/
│   │   └── app.js         # PWA クライアントロジック (MediaSession/キャッシュ/Curator/PDF/RSS)
│   ├── icons/             # 192x192, 512x512 PWA アプリアイコン
│   ├── manifest.json      # PWA Web App Manifest
│   └── sw.js              # オフライン & Rangeリクエスト対応 Service Worker
├── templates/
│   └── index.html         # PWA UI テンプレート (3タブ: 再生キュー / Curator / RSS)
├── main.py                # FastAPI メインアプリケーション (Curator/PDF/キュー/RSS API)
├── curator_sources.py     # Curator 情報ソース巡回・実アクセス検証・ペイウォール排除
├── curator_engine.py      # Curator 7軸評価・枠別選考・7ステップ原稿・逐次VOICEPEAK音声化・学習ループ
├── queue_manager.py       # キュー永続化マネージャー (PDF削除連動)
├── feed_manager.py        # RSSフィード購読・タイムライン管理
├── poc_pipeline.py        # 抽出・Gemini PDF/Webリライト・VOICEPEAK/Edge-TTS パイプライン
├── test_curator_verification.py   # Personal Knowledge Curator 自動検証テスト
├── test_verification.py   # Web & RSS 自動結合テストスクリプト
├── test_voicepeak_verification.py # VOICEPEAK 統合・音声検証テストスクリプト
├── test_pdf_verification.py       # PDF 解析・VOICEPEAK 音声化結合テストスクリプト
├── run_curator_daily.bat  # 毎日03:00 自動キュレーション＆事前音声化バッチ (ReadoutDailyCurator)
├── run_server.bat         # サーバー起動バッチ
├── run_server.ps1         # サーバー起動 PowerShell (Win32_Process非表示自立起動)
├── requirements.txt       # 依存ライブラリ
└── venv/                  # 専用仮想環境 (Python 3.12)

```

---

## 🛠️ サーバーの起動方法

### 1. バッチファイルまたはPowerShellで起動
```powershell
cd C:\Users\Jun\Documents\Dev\Readout
.\run_server.bat
# または
.\run_server.ps1
```
サーバーが `http://127.0.0.1:8002` で起動します。

---

## 📱 PWA の使用方法

1. スマホまたはPCのブラウザ（Chrome / Edge / Safari）で `http://127.0.0.1:8002`（または公開トンネルURL）を開きます。
2. 「ホーム画面に追加」「アプリをインストール」でPWAとしてインストール可能。
3. 上部の入力バーに聴きたい記事のURLをペーストして「追加」を押す。
4. Gemini 3.8 Flash によるリライトと VOICEPEAK（女性2）による高品質音声化が完了し、キューに「READY」として追加されます（万一の障害時は Edge-TTS に自動フォールバック）。
5. 「⚡ オフライン済」バッジが表示されたら、スマホを機内モード（オフライン）にしてもそのまま高音質再生が可能です。
6. 右上の「RSS」ボタンを押すと、Pocket Casts 購読用のプライベート RSS フィード URL（`/feed.xml`）がコピーされます。

---

## 📡 API エンドポイント一覧

| メソッド | パス | 説明 |
|---|---|---|
| `GET` | `/` | PWA UI 画面 (HTML) |
| `GET` | `/manifest.json` | Web App Manifest |
| `GET` | `/sw.js` | Service Worker (Rootスコープ配信) |
| `GET` | `/api/queue` | 未読キューアイテム一覧取得 |
| `POST` | `/api/queue` | 新規URL追加（Web記事 / PDF直リンク対応、バックグラウンド生成開始） |
| `POST` | `/api/upload-pdf` | **PDFファイルアップロード（Gemini 3.8 Flash ＋ VOICEPEAK 音声化）** |
| `GET` | `/pdf/{id}.pdf` | アップロード済み原本PDFダウンロード・プレビュー |
| `POST` | `/api/queue/{id}/listened` | 既読フラグ更新（キューから除外） |
| `DELETE` | `/api/queue/{id}` | キュー削除およびMP3/原本PDFファイル削除 |
| `GET` | `/api/feeds` | 購読中RSSフィード一覧取得 |
| `POST` | `/api/feeds` | 新規RSSフィード購読登録 |
| `DELETE` | `/api/feeds/{id}` | RSSフィード購読解除 |
| `GET` | `/api/articles` | 購読フィード新着記事タイムライン一覧（最新順） |
| `GET` | `/audio/{filename}` | MP3 音声ストリーミング（HTTP Range 206 対応） |
| `GET` | `/feed.xml` | Pocket Casts 購読用 Podcast RSS 2.0 フィード |

