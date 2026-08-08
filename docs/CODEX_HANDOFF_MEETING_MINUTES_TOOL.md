# 会議議事録作成ツール - Codex引き継ぎ資料

更新日: 2026-08-08

## 0. このドキュメントの目的

このドキュメントは、Teams / Google Meet等のオンライン会議について、PC上の音声と共有画面をローカルで取得し、会議終了後に文字起こし・話者分離・画面解析・議事録生成を行うデスクトップアプリの設計・実装をCodexへ引き継ぐための資料である。

Codexは本資料の「確定要件」を原則として変更せず、まずPoCを段階的に実装すること。未確定事項は、本資料で明示したもの以外を勝手に広げないこと。

---

# 1. プロジェクト概要

## 1.1 目的

TeamsやGoogle Meet等の会議中に以下を取得する。

- 自分のマイク音声
- PCから再生される会議相手の音声
- ユーザーが選択した、共有資料が表示されているChrome / Teams等のウィンドウ

会議中はAIによる重い解析を原則行わず、確実な記録を優先する。会議終了後にローカルAIで以下を実行する。

1. 文字起こし
2. 話者分離
3. 画面解析
4. 音声・画面の時系列統合
5. Markdown形式の議事録生成

最終的には一般社員がPython環境を意識せず、Windowsではexe等のアプリとして利用できる状態を目標とする。

## 1.2 想定利用環境

- Windows
- Ubuntu 22.04 LTS系
- NVIDIA RTX 4060 / VRAM 8GB
- RAM 64GB
- 管理者権限あり
- 会議時間は1時間程度を主な想定とする

## 1.3 会社利用上の前提

- すべての会議データ処理はローカルで完結できること
- 会議音声、画面、文字起こし等を外部AIサービスへ送信しないこと
- インターネット接続自体を禁止する要件ではない
- 外部アカウント登録が必須の技術は可能な限り避ける
- OSSライセンス、モデルライセンスを配布前に確認すること
- 生データの保存・削除をユーザー設定で切り替えられること

---

# 2. 確定要件

## 2.1 会議中

- リアルタイム文字起こしは不要
- リアルタイム画面理解は不要
- 自分のマイク音声とPC再生音声は別トラックで取得する
- ユーザーが会議開始時に取得対象ウィンドウを選択する
- マルチモニター同時取得は不要
- 画面を動画として常時録画するのではなく、意味のある変更が発生したときのみスクリーンショットを保存する
- 会議中UIにはマイクとPC音声の音量バーを表示したい
- 会議中はできるだけGPUを使わず、Teams / Chromeとのリソース競合を避ける

## 2.2 会議終了後

以下の処理を独立した機能として実装する。

- 文字起こし
- 話者分離
- 画面解析
- 議事録生成

各機能には個別実行ボタンを用意する。

さらに設定によって、会議終了後に上記を順番に自動実行できるようにする。

例:

```text
[✓] 自動で文字起こし
[✓] 自動で話者分離
[✓] 自動で画面解析
[✓] 自動で議事録生成
```

## 2.3 話者

正式対応は以下までとする。

- 自分
- Speaker 1
- Speaker 2
- Speaker 3 ...

会議終了後にユーザーがGUIからSpeaker名を手動変更できること。

例:

```text
Speaker 1 -> 田中
Speaker 2 -> 鈴木
```

音声のみから人物名を完全自動推定する機能は初期スコープ外とする。

## 2.4 出力

生成物は2ファイルに分離する。

- `minutes.md`: 読むための議事録。Confluenceへの貼り付けを想定
- `transcript.md`: 全文文字起こし・詳細確認用

初期段階ではConfluence APIへの直接投稿は実装しない。Markdownをユーザーがコピーして投稿する運用とする。

---

# 3. 設計の基本方針

## 3.1 最重要方針

会議中は「AI解析」より「欠損なく記録すること」を優先する。

```text
会議中
  音声取得
  画面取得
  音量表示
  画面変更検知
  ローカル保存

      ↓ 会議終了

会議後
  文字起こし
  話者分離
  画面解析
  タイムライン統合
  議事録生成
```

RTX 4060 8GBでは複数の大きなAIモデルを同時にGPUへロードしない。

## 3.2 Teams / Google Meetとの連携方式

Teams APIやGoogle Meet API、会議Bot等への依存は避ける。

OSから以下を取得する。

- マイク入力
- PC再生音声
- 対象ウィンドウの画像

この方式によりTeams / Chrome / Meetの内部仕様への依存を減らす。

---

# 4. 推奨アーキテクチャ

```text
                          PySide6 GUI
                               |
                       Session Manager
                               |
             +-----------------+-----------------+
             |                                   |
        Recording Mode                      Analysis Mode
             |                                   |
     +-------+--------+               +----------+----------+
     |       |        |               |          |          |
    Mic    System   Screen            STT     Diarization  Vision
   Audio    Audio   Capture            |          |          |
     |       |        |                +----------+----------+
     +-------+--------+                           |
             |                              Timeline Merge
       Session Storage                           |
                                                 v
                                         Minutes Generator
                                            |           |
                                      minutes.md   transcript.md
```

Windows / Ubuntu差分は主にCapture層へ閉じ込める。

---

# 5. 技術選定 - 現時点の候補

以下は現段階の推奨候補であり、PoCで実機検証して確定すること。

| 機能 | 第一候補 | 備考 |
|---|---|---|
| 言語 | Python 3.11〜3.12 | 依存ライブラリ互換性を優先 |
| GUI | PySide6 | UI、設定、進捗、音量バー |
| 音声取得 | SoundCard | Windows / Linux共通化候補 |
| Windows再生音声 | WASAPI Loopback | 必須 |
| Ubuntu再生音声 | PipeWire / PulseAudio互換 | Ubuntu 22.04想定 |
| WAV保存 | soundfile | FFmpegを使用しない |
| 音声配列 | NumPy | 音量計算等にも使用 |
| VAD | Silero VAD | 会議後処理中心。必要ならチャンク化 |
| STT | faster-whisper | Whisper large-v3-turbo候補 |
| 話者分離 | sherpa-onnx | アカウント不要候補 |
| Windows画面取得 | DXcam系 | 実機PoCでウィンドウ取得方式確認 |
| Ubuntu画面取得 | xdg-desktop-portal + PipeWire + GStreamer | Wayland対応を優先 |
| 画像処理 | OpenCV | 変更検知・縮小等 |
| OCR | PaddleOCR | 必要に応じて使用 |
| 画面理解 | Qwen3-VL 4B級 | 量子化モデル候補 |
| 議事録LLM | Qwen3 8B級 | Q4量子化候補 |
| LLM runtime | llama.cpp | localhostで使用可能 |
| データ管理 | JSON / JSONL + 必要ならSQLite | 中間生成物を保持 |
| 配布 | PyInstaller / Nuitkaを比較 | 後工程で決定 |

### 重要

- FFmpegは使用しない。
- PySide6のQt Multimedia経由でFFmpegバックエンドを暗黙利用する構成も避ける。
- Capture部分はOS固有実装を抽象インターフェースの背後に置く。
- llama.cpp等の外部実行ファイルを同梱する場合は、配布方法・ライセンスを別途確認する。

---

# 6. 音声取得設計

## 6.1 2トラック方式

必ず可能な限り以下を分離して保存する。

```text
microphone.wav
  -> 自分のマイク音声

system.wav
  -> Teams / MeetからPCへ再生される他参加者音声
```

メリット:

- 自分自身の話者分離がほぼ不要になる
- Speaker diarization対象をsystem.wavに限定できる
- 自分と他参加者が重なったときの解析がしやすい
- 障害調査がしやすい

## 6.2 音量バー

会議中のAI処理は不要。

PCMバッファからRMSまたはPeakを算出し、PySide6の表示へ反映する。

```text
PCM Buffer
   -> RMS / Peak
   -> 正規化 0.0〜1.0
   -> GUI Meter
```

UI更新頻度は高すぎないよう、10〜20fps程度以下を目安にする。

## 6.3 保存形式

初期PoCではPCM WAVを優先する。

- 圧縮による失敗要因を減らす
- STTへ直接渡しやすい
- 1時間程度なら容量は許容範囲

必要なら後からFLAC等を検討する。

---

# 7. 画面取得設計

## 7.1 選択対象

会議開始時に、ユーザーが共有資料の表示されているウィンドウを選択する。

例:

- Chrome - Google Meet
- Microsoft Teams
- PowerPoint

ディスプレイ全体を常時取得する方式を標準としない。

## 7.2 意味のある変更のみ保存

単純に「1ピクセルでも変化したら保存」してはいけない。

無視したい変化例:

- マウスカーソル移動
- 時計
- Teamsの小さな参加者アイコンの点滅
- 軽微なアニメーション
- スクロールバーの小変化
- 字幕の更新だけで画面全体の資料が同じ場合

初期アルゴリズム案:

```text
定期キャプチャ
   |
   v
縮小画像を生成
   |
   v
前回画像との差分
   |
   +-- 小さい -> 何もしない
   |
   v
変更領域の面積・割合を評価
   |
   +-- 小さい -> 何もしない
   |
   v
500ms程度デバウンス
   |
   v
再取得して画面が安定しているか確認
   |
   v
スクリーンショット保存
```

PoCではOpenCVベースの軽量処理から始める。

VLMを会議中の変更判定へ使用しない。

## 7.3 保存形式

PNGまたはWebPを候補とする。

PoCではPNGでもよいが、1時間の会議で枚数が増えることを考慮し、最終的にはWebPも比較する。

全画像に相対時刻またはセッション開始からのミリ秒を記録する。

---

# 8. OS別Capture方針

## 8.1 Windows

### 音声

- マイク入力
- WASAPI LoopbackによるPC再生音声

まずSoundCardで共通化可能か検証する。

不安定な場合のみWindows固有実装を追加する。

### 画面

DXcam系を候補とするが、単なる高速モニターキャプチャではなく「ユーザーが指定したウィンドウ」を安定取得できる方式をPoCで検証すること。

必要ならWindows Graphics Capture / Desktop Duplication等のラッパーを比較する。

## 8.2 Ubuntu 22.04

Ubuntu 22.04ではWaylandを正式ターゲットとして考える。

画面取得は以下を優先する。

```text
xdg-desktop-portal
        |
     ScreenCast
        |
     PipeWire
        |
    GStreamer
        |
      appsink
        |
   NumPy / OpenCV
```

ユーザーがOSの共有対象選択ダイアログで対象ウィンドウを選択する方式を許容する。

FFmpegは使わない。

X11対応は必要になった時点で追加し、最初のPoCを複雑化させない。

---

# 9. 会議後のAI処理

## 9.1 処理順

```text
1. STT
2. Speaker Diarization
3. Screen Analysis
4. Timeline Merge
5. Minutes Generation
```

各工程は独立Jobとする。

## 9.2 文字起こし

第一候補:

- faster-whisper
- Whisper large-v3-turbo

出力には最低限以下を含める。

```json
{
  "start": 123.40,
  "end": 128.75,
  "source": "system",
  "text": "この機能は来週までに対応します"
}
```

マイク側は`source: microphone`、PC側は`source: system`等で区別する。

## 9.3 話者分離

PC再生音声を主対象としてSpeaker 1 / Speaker 2等へ分離する。

候補はsherpa-onnx。

最終的な表示名はユーザーによる手動マッピングを許可する。

```json
{
  "speaker_1": "田中",
  "speaker_2": "鈴木"
}
```

このマッピングを保存し、`transcript.md`と`minutes.md`を再生成できるようにする。

## 9.4 画面解析

保存された「意味のある画面変更」のスクリーンショットのみを解析する。

第一段階:

- OCR
- 画像の種類・タイトル・重要事項抽出

必要に応じてQwen3-VL 4B級を使用する。

画面解析出力例:

```json
{
  "timestamp": 845.2,
  "image": "000042.webp",
  "type": "presentation",
  "title": "API仕様変更",
  "summary": "認証API変更案を説明している",
  "important": [
    "旧APIを廃止予定",
    "新APIへの移行が必要"
  ]
}
```

## 9.5 タイムライン統合

音声と画面情報を共通時刻で統合する。

例:

```text
14:04:05
画面:
  Issue #123
  担当: 田中
  期限: 8/15

Speaker 1:
  「この部分は来週までに修正します」
```

ここが議事録品質に強く影響するため、すべてのイベントにセッション開始時刻基準のtimestampを付けること。

---

# 10. GPU / モデル管理

RTX 4060 8GBを前提に、大型モデルを同時にGPUへ載せない。

推奨:

```text
STT Job
  Whisperをロード
  -> 処理
  -> 解放

Vision Job
  Qwen-VLをロード
  -> 処理
  -> 解放

Minutes Job
  Text LLMをロード
  -> 処理
  -> 解放
```

## 10.1 実装上の推奨

重いAI Jobは可能なら別プロセスとして起動する。

理由:

- Job終了時にGPU VRAMを確実に解放しやすい
- ライブラリクラッシュがGUIへ波及しにくい
- Whisper / llama.cpp / Visionの依存衝突を抑えやすい
- Job単位で再実行しやすい

GUIプロセスにすべてのAIモデルを常駐させない。

---

# 11. UI案

## 11.1 録音画面

```text
+-------------------------------------------+
| 会議名 [ 開発チーム定例____________ ]    |
|                                           |
| マイク                                    |
| Realtek Microphone                        |
| [##########------]                        |
|                                           |
| PC音声                                    |
| Speakers                                  |
| [#######---------]                        |
|                                           |
| 取得画面                                  |
| Chrome - Google Meet                      |
|                                           |
| 経過時間          00:37:21                |
| 保存画像          42                      |
|                                           |
|                [ 会議終了 ]               |
+-------------------------------------------+
```

必要な状態表示:

- マイク接続状態
- PC音声取得状態
- 画面取得状態
- 録音経過時間
- 保存済みスクリーンショット数
- エラー / 警告

## 11.2 解析画面

```text
+-------------------------------------------+
| 文字起こし       完了        [ 実行 ]    |
| 話者分離         完了        [ 実行 ]    |
| 画面解析         未実行      [ 実行 ]    |
| 議事録生成       未実行      [ 実行 ]    |
|                                           |
| [ 全て自動実行 ]                          |
+-------------------------------------------+
```

Jobの状態を少なくとも以下で管理する。

- NOT_STARTED
- RUNNING
- SUCCEEDED
- FAILED
- CANCELED

失敗したJobだけ再実行できること。

## 11.3 話者編集

```text
自分       -> 自分
Speaker 1  -> [田中____________]
Speaker 2  -> [鈴木____________]
```

話者名変更後、全文文字起こし・議事録を再生成できるようにする。

---

# 12. セッション保存構造

推奨:

```text
meetings/
└─ 2026-08-08_開発定例/
   ├─ session.json
   ├─ audio/
   │  ├─ microphone.wav
   │  └─ system.wav
   ├─ screenshots/
   │  ├─ 000001.webp
   │  ├─ 000002.webp
   │  └─ ...
   ├─ analysis/
   │  ├─ transcription.json
   │  ├─ diarization.json
   │  ├─ screens.json
   │  └─ timeline.json
   └─ output/
      ├─ minutes.md
      └─ transcript.md
```

## 12.1 session.json例

```json
{
  "id": "uuid",
  "title": "開発定例",
  "started_at": "2026-08-08T10:00:00+09:00",
  "ended_at": "2026-08-08T11:00:00+09:00",
  "duration_sec": 3600,
  "audio": {
    "microphone_device": "...",
    "system_device": "..."
  },
  "screen": {
    "target": "Chrome - Google Meet"
  },
  "retention": {
    "keep_audio": true,
    "keep_screenshots": true
  }
}
```

中間JSONを残す理由:

- 議事録モデルだけ変更して再生成できる
- 話者名変更後に再生成できる
- 障害調査がしやすい
- STTを毎回やり直さなくてよい

---

# 13. Markdown出力仕様

## 13.1 minutes.md

推奨テンプレート:

```markdown
# 開発定例

## 会議概要
- 日時:
- 会議時間:
- 参加者:

## 要約

## 議題

### 1. ...

## 決定事項
- ...

## TODO
| 担当 | 内容 | 期限 |
|---|---|---|
| ... | ... | ... |

## 保留事項
- ...

## 参考情報
- 会議中に表示された重要な画面・資料の内容
```

Confluenceへコピーしやすい、過度に特殊でないMarkdownを使う。

## 13.2 transcript.md

全文文字起こしを時刻・話者付きで記録する。

```markdown
# Transcript

## 00:04:12
**自分**
今回の仕様について確認します。

## 00:04:19
**田中**
こちらでは来週までに対応できます。
```

---

# 14. データ保持・削除

設定で以下を選択できるようにする。

- 音声を保持する / 削除する
- スクリーンショットを保持する / 削除する

自動削除を行う場合は、少なくとも必要な解析JobとMarkdown生成が正常完了したことを確認してから削除する。

解析失敗時に原本を自動削除しない。

---

# 15. 推奨Pythonモジュール構成

```text
meeting_minutes/
├─ main.py
├─ ui/
│  ├─ main_window.py
│  ├─ recording_page.py
│  ├─ analysis_page.py
│  ├─ speaker_edit_page.py
│  └─ settings_page.py
├─ application/
│  ├─ session_manager.py
│  ├─ recording_service.py
│  ├─ analysis_service.py
│  ├─ job_manager.py
│  └─ gpu_task_manager.py
├─ capture/
│  ├─ audio/
│  │  ├─ base.py
│  │  ├─ microphone_capture.py
│  │  ├─ system_audio_capture.py
│  │  ├─ windows_audio.py
│  │  └─ ubuntu_audio.py
│  └─ screen/
│     ├─ base.py
│     ├─ windows_capture.py
│     └─ ubuntu_capture.py
├─ processing/
│  ├─ screen_change_detector.py
│  ├─ transcription.py
│  ├─ diarization.py
│  ├─ vision.py
│  ├─ timeline.py
│  └─ minutes_generator.py
├─ domain/
│  ├─ session.py
│  ├─ transcript.py
│  ├─ speaker.py
│  ├─ screen_event.py
│  └─ analysis_job.py
├─ infrastructure/
│  ├─ storage.py
│  ├─ settings.py
│  ├─ process_runner.py
│  └─ model_manager.py
└─ tests/
```

UIがOS固有APIやAIライブラリを直接呼ばないようにする。

---

# 16. スレッド / プロセス方針

## 16.1 会議中

PySide6のUIスレッドをブロックしない。

推奨ワーカー:

```text
Main/UI Thread
   |
   +-- Microphone Capture Worker
   +-- System Audio Capture Worker
   +-- Screen Capture Worker
   +-- Session Writer / Event Worker
```

音声ファイルへの書き込みをUIスレッドで行わない。

画面変更検知もCapture Worker側または専用Workerで処理する。

## 16.2 会議後

AI処理はバックグラウンドJobとして行う。

特に重いモデルはサブプロセス化を優先する。

UIは進捗とキャンセル操作のみ担当する。

---

# 17. PoC実装順序

いきなりAI機能をすべて実装しない。

## Phase 1 - 記録基盤

最優先。

### Windows

- PySide6基本画面
- マイクデバイス列挙
- PC再生デバイス列挙
- マイク録音
- WASAPI Loopback録音
- 2トラックWAV保存
- 音量バー
- 対象ウィンドウ選択
- スクリーンショット取得
- 画面変更検知
- session.json生成

### Ubuntu 22.04

同等機能を以下で実証する。

- マイク取得
- PC再生音声取得
- xdg-desktop-portalによる画面選択
- PipeWire / GStreamer経由のフレーム取得
- 画面変更検知

### Phase 1の成功条件

1時間の模擬会議で以下を満たすこと。

- UIがフリーズしない
- マイク音声に欠損がない
- PC再生音声に欠損がない
- 音声2トラックの開始時刻が大きくずれない
- 画面変更時に適切な画像が保存される
- マウス移動だけで大量保存されない
- 録音終了後にファイルが正常に閉じられる
- 異常終了後も可能な範囲でデータを復旧できる構造になっている

## Phase 2 - STT

- faster-whisper導入
- microphone.wav / system.wavの文字起こし
- timestamp付きJSON生成
- transcript.md仮生成

## Phase 3 - 話者分離

- sherpa-onnx等を比較
- system.wavのSpeaker分離
- STTとのtimestamp統合
- GUIでSpeaker名変更

## Phase 4 - 画面解析

- OCR
- Qwen3-VL等のローカルVLM
- screens.json生成

## Phase 5 - 統合議事録

- timeline.json
- ローカルLLM
- minutes.md生成
- 再生成

## Phase 6 - 配布

- Windows exe
- Ubuntuアプリ配布形式
- モデル配置
- 初回セットアップ
- OSSライセンス表記
- 更新方法

---

# 18. エラーハンドリングで重視すること

会社の会議記録ツールなので、「失敗して録音データが全部消える」ことを最も避ける。

優先順位:

1. 音声データ保全
2. セッションメタデータ保全
3. スクリーンショット保全
4. AI解析
5. UI表示

例:

- 画面キャプチャが失敗しても録音は継続する
- マイクだけ失敗した場合、PC音声録音を停止しない
- AI解析が失敗しても原本は保持する
- Job失敗理由をログへ記録する
- 会議終了処理でWAVヘッダ等を確実にfinalizeする

---

# 19. ログ

最低限以下をローカルログへ記録する。

- アプリ起動 / 終了
- セッション開始 / 終了
- 選択デバイス
- Capture Worker開始 / 停止
- バッファオーバーラン / ドロップ
- スクリーンショット保存数
- Job開始 / 完了 / 失敗
- 例外スタック

会議本文そのものを通常ログへ大量出力しない。

---

# 20. セキュリティ / プライバシー

- 既定ではlocalhost以外へ会議データを送らない
- LLM serverを使用する場合は`127.0.0.1`へbindする
- ネットワークアクセスが不要な処理は外部通信しない
- 会社利用のため、テレメトリ有無を依存ライブラリごとに確認する
- APIキーを前提にしない
- 保存先をユーザーが変更可能にする余地を残す
- 解析途中で外部クラウドへfallbackする機能を勝手に追加しない

---

# 21. 非機能要件

## パフォーマンス

会議中のアプリ自身のGPU使用は原則最小化する。

CPUも画面変更検知のために高FPSキャプチャしない。

目安:

- 画面確認: 1〜2fps程度からPoC
- 音量UI: 10〜20fps以下
- スクリーンショット保存: 変更時のみ

値は実測で調整する。

## 安定性

- 1時間連続動作を最低基準にする
- UIフリーズを許容しない
- AI解析Jobの失敗から再開できる

## 保守性

- Capture実装をOS別に交換可能
- STT / diarization / VLM / LLMを交換可能
- 中間JSONスキーマを明示する

---

# 22. 初期スコープ外

初期PoCでは以下を実装しない。

- Teams / Google Meet API連携
- 会議Bot参加
- リアルタイム字幕
- リアルタイム議事録生成
- リアルタイムVLM画面理解
- 複数モニター同時キャプチャ
- 話者の完全自動実名特定
- Confluence API自動投稿
- クラウドAI fallback
- 動画としての画面録画

---

# 23. 未確定・PoCで決める項目

Codexは以下を勝手に固定せず、PoC結果で選定すること。

1. Windowsの最終的なウィンドウキャプチャ実装
2. Windows音声取得でSoundCardのみで十分か、OS固有fallbackが必要か
3. Ubuntu 22.04のPipeWire / portal / GStreamer実装方法の詳細
4. スクリーンショット変更判定の閾値
5. PNG / WebPどちらを標準にするか
6. faster-whisperのcompute_type
7. 話者分離モデルの最終候補
8. Qwen-VLのモデル・量子化方式
9. 議事録生成用LLMのモデル・量子化方式
10. PyInstaller / Nuitka等の最終配布手段
11. Python 3.11 / 3.12の最終固定

---

# 24. Codexへの実装指示

最初にPhase 1の記録基盤から着手すること。

AIモデルの導入を先行させない。

推奨する最初の作業順:

1. Pythonプロジェクト骨格を作る
2. domainモデルとsession.jsonの仕様を定義
3. PySide6でRecording画面を作る
4. AudioCapture抽象インターフェースを作る
5. Windowsのマイク録音を実装
6. WindowsのLoopback録音を実装
7. WAV保存と音量バーを実装
8. ScreenCapture抽象インターフェースを作る
9. Windowsで対象ウィンドウ取得PoC
10. ScreenChangeDetectorを実装
11. 15分 -> 1時間の連続テスト
12. その後Ubuntu 22.04のCapture実装へ進む

実装時には、小さくテスト可能な単位で進める。

### Codexが避けるべきこと

- FFmpegを依存に追加する
- 最初から全機能を1つの巨大クラスへ実装する
- UIスレッドで音声取得・ファイルI/O・AI推論を行う
- Teams / Meet内部APIへ依存する
- 外部AI APIを導入する
- 会議中にWhisperやVLMを常時動作させる
- 生データを解析完了前に削除する
- GPUモデルを複数常駐させる
- OS固有コードをUI層へ直接書く

---

# 25. 最初に作るべき成果物

Codexが実装開始時にまず作成する成果物:

1. `README.md`
2. `pyproject.toml`
3. 上記ディレクトリ構成
4. `domain/session.py`
5. `capture/audio/base.py`
6. `capture/screen/base.py`
7. `ui/recording_page.py`
8. `application/session_manager.py`
9. Windows音声取得PoC
10. 録音の自動テストまたは手動検証手順

READMEには少なくとも以下を書く。

- 目的
- 対応OS
- 非対応機能
- 開発環境セットアップ
- 起動方法
- Phase 1の検証方法
- 完全ローカル処理方針

---

# 26. 最終的な完成イメージ

```text
アプリ起動
  |
  v
会議名入力
マイク選択
PC音声選択
対象ウィンドウ選択
  |
  v
[会議開始]
  |
  +-- microphone.wav
  +-- system.wav
  +-- 画面変更時だけスクリーンショット
  +-- 音量バー表示
  |
  v
[会議終了]
  |
  v
解析画面
  |
  +-- 文字起こし
  +-- 話者分離
  +-- 画面解析
  +-- Speaker名編集
  +-- 議事録生成
  |
  v
minutes.md
transcript.md
  |
  v
Confluenceへ手動コピー
```

以上を現時点のプロジェクト方針とする。
