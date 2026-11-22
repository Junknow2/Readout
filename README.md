# 🏃 Readout (リードアウト)

マラソンや屋外ランニング中に「耳で聴いて知識をインプットする」ためのパーソナル音声ニュース・ポッドキャスト生成プラットフォーム。

Web上の記事（技術ブログ、note、海外テックニュース等）のURLを入力すると、本文抽出 ➜ Gemini による耳用リライト・翻訳 ➜ Edge-TTS による高品質日本語ナレーション音声（MP3）を非同期自動生成。屋外や直射日光下でも操作しやすい**高視認性PWA（Progressive Web App）**および**Pocket Casts等で購読できるプライベートPodcast RSSフィード**を提供します。

---

## 🚀 主な機能と特徴

1. **FastAPI バックエンド & 非同期パイプライン (`main.py`, `queue_manager.py`)**:
   - 内部ポート: `127.0.0.1:8002`
   - FastAPI BackgroundTasks による非同期音声生成（キュー投入後即レスポンス）。
   - キュー管理（`data/queue.json`）による状態永続化（`pending` ➜ `processing` ➜ `ready` ➜ `listened`）。
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

---

## 📁 ディレクトリ構成

```text
Readout/
├── data/
│   ├── queue.json         # 再生キュー永続化ストア
│   └── feeds.json         # 購読RSSフィード設定
├── output/                # 生成された MP3 および テキスト原稿
├── static/
│   ├── css/
│   │   └── style.css      # ランナー特化ハイコントラスト CSS (タブ・RSSリーダー含む)
│   ├── js/
│   │   └── app.js         # PWA クライアントロジック (MediaSession/キャッシュ/RSS)
│   ├── icons/             # 192x192, 512x512 PWA アプリアイコン
│   ├── manifest.json      # PWA Web App Manifest
│   └── sw.js              # オフライン & Rangeリクエスト対応 Service Worker
├── templates/
│   └── index.html         # PWA UI テンプレート
├── main.py                # FastAPI メインアプリケーション
├── queue_manager.py       # キュー永続化マネージャー
├── feed_manager.py        # RSSフィード購読・タイムライン管理
├── poc_pipeline.py        # 抽出・Geminiリライト・Edge-TTS パイプライン
├── test_verification.py   # 自動結合テストスクリプト
├── run_server.bat         # サーバー起動バッチ
├── run_server.ps1         # サーバー起動 PowerShell
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
4. 約15〜20秒で Gemini 3.8 Flash によるリライトと Edge-TTS による音声化が完了し、キューに「READY」として追加されます。
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
| `POST` | `/api/queue` | 新規URL追加（バックグラウンド生成開始） |
| `POST` | `/api/queue/{id}/listened` | 既読フラグ更新（キューから除外） |
| `DELETE` | `/api/queue/{id}` | キュー削除およびMP3ファイル削除 |
| `GET` | `/api/feeds` | 購読中RSSフィード一覧取得 |
| `POST` | `/api/feeds` | 新規RSSフィード購読登録 |
| `DELETE` | `/api/feeds/{id}` | RSSフィード購読解除 |
| `GET` | `/api/articles` | 購読フィード新着記事タイムライン一覧（最新順） |
| `GET` | `/audio/{filename}` | MP3 音声ストリーミング（HTTP Range 206 対応） |
| `GET` | `/feed.xml` | Pocket Casts 購読用 Podcast RSS 2.0 フィード |

