# Summarize Meeting Phase 4 詳細設計

## 1. 目的

Phase 4では、Phase 1で「意味のある画面変更」として保存したスクリーンショットを会議終了後にローカル解析し、Phase 5の統合議事録が参照できるtimestamp付き`analysis/screens.json`を生成する。

正常系PoCではWindows 11内蔵OCRを使用し、画面内テキスト、画面種別、タイトル候補、抽出的な要約、重要事項候補を生成する。画像に存在しない内容を推測しない。

## 2. 前提

- 正式対象はWindows 11
- `session.json`が`RECORDED`
- `screenshots/events.jsonl`と1枚以上の画像が存在する
- 全イベントはセッション開始時刻基準の`timestamp_ms`を持つ
- 画像と解析結果を外部サービスへ送信しない
- AI JobはGUIプロセスと分離する
- アプリは第三者へ配布しないため、再配布ライセンス対応は対象外
- Windowsの日本語OCR言語パックがインストール済み

## 3. 対象範囲

### 3.1 対象

- 保存済みスクリーンショットだけを会議終了後に解析
- Windows Media OCRによる日本語・英数字認識
- OCR行とbounding rectangleの保存
- 画面種別の規則ベース分類
- 画面タイトル候補の抽出
- OCR本文に基づく抽出的要約
- 期限、担当、決定、TODO、変更、エラー等の重要行抽出
- `analysis/screens.json`のatomic生成
- 画像単位の成功・失敗記録
- GUIからの実行、再実行、キャンセル
- Job状態の`analysis/jobs.json`保存
- アプリ再起動後の状態表示

### 3.2 対象外

- 録画および動画解析
- 会議中のリアルタイムOCR/VLM
- OCR結果のGUI編集
- 画像に写っていない内容の推測
- 図表の数値構造化
- 顔認識、人物同定、感情推定
- Qwen3-VLによる生成的画面理解
- 複数画像をまたぐ重複除去とストーリー統合
- 音声とのtimeline統合

生成的VLMとtimeline統合は、それぞれPhase 4精度改善とPhase 5で扱う。

## 4. 技術選定

| 項目 | 採用案 |
|---|---|
| OCR runtime | Windows.Media.Ocr |
| OCR言語 | `ja` |
| 画像decode | OpenCV `imdecode` |
| 画面理解 | OCRに基づく決定的な規則処理 |
| provider | CPU / Windows OS API |
| Job分離 | child Python process |
| 出力 | UTF-8 JSON |

Windows Media OCRはデバイスにインストールされたOCR言語を列挙し、指定言語が利用可能か判定できる。言語パックがない場合はJob開始後に明示的なエラーとして返す。

Qwen3-VL 4B Instructの公式BF16重みは約9GBあり、想定GPUのRTX 4060 8GBへそのまま常駐できない。正常系PoCでは安定したOS OCRを先行採用し、Q4級量子化モデルとllama.cpp系multimodal runtimeの比較を別工程にする。

参考:

- Windows OCR language support: https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrengine.islanguagesupported
- Windows OCR available languages: https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrengine.availablerecognizerlanguages
- Qwen3-VL official repository: https://github.com/QwenLM/Qwen3-VL
- Qwen3-VL-4B-Instruct model: https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct

## 5. 処理構成

```text
GUI process
  MainWindow
    -> ScreenAnalysisController
         -> child Python process
              -> ScreenAnalysisService
                   -> WindowsOcrBackend
                   -> deterministic understanding
                   -> analysis/screens.json
```

録音、STT、話者分離、画面解析は同時実行しない。OCR engineと画像bufferはworkerだけで保持し、Job終了時にプロセスごと解放する。

## 6. 入力

### 6.1 `screenshots/events.jsonl`

Phase 1が画像保存成功後に追記したJSON Linesを正とする。

```json
{
  "schema_version": 1,
  "sequence": 1,
  "timestamp_ms": 1600,
  "file": "000001.png",
  "width": 890,
  "height": 797,
  "reason": "initial",
  "metrics": {
    "changed_ratio": 1.0,
    "mean_abs_diff": 255.0
  }
}
```

処理順は`timestamp_ms`、`sequence`、`file`の安定sortとする。timestampは画像解析で作り直さず、Phase 1の共通monotonic originに基づく値を保持する。

### 6.2 画像パス

- basenameだけを許可
- 絶対パス、親参照、サブディレクトリを拒否
- 拡張子は`.png`、`.jpg`、`.jpeg`、`.webp`
- OpenCVはUnicodeパス非対応を避けるため、`Path.read_bytes()`と`cv2.imdecode()`を使う

## 7. OCR

1. 画像をBGRとしてdecodeする
2. BGRA8へ変換する
3. WinRT `Buffer`へコピーする
4. `SoftwareBitmap`を作成する
5. `OcrEngine(ja)`で認識する
6. 全文、行、各行のword bounding rectangle統合値を取得する

OCR結果は補正・翻訳せず、そのまま保存する。OCR誤認識は後続VLMまたはPhase 5で参照できるよう原文を残す。

## 8. 決定的な画面理解

生成モデルを使わず、OCR行だけから次を作る。

### 8.1 type

- `meeting`: Teams、Google Meet、参加者等
- `presentation`: PowerPoint、スライド等
- `spreadsheet`: Excel、スプレッドシート、セル等
- `document`: Word、文書、ページ等
- `code`: Visual Studio、GitHub、Pull Request等
- `browser`: Chrome、Edge、URL等
- `unknown`: 該当なし

### 8.2 title

先頭の非空OCR行を最大120文字で使用する。タイトルらしさを生成的に推測しない。

### 8.3 summary

先頭5行を最大300文字で連結し、`画面内テキスト:`を付ける。OCRが空の場合は`画面内の文字を検出できませんでした`とする。

### 8.4 important

次を含む行を出現順で最大10件抽出する。

- TODO、決定、担当、期限、締切
- 課題、重要、必須、変更
- エラー、失敗、リスク
- 日付形式、今週、来週

抽出元に存在しない文言を追加しない。

## 9. 部分失敗

- 画像1枚の欠損、decode失敗、OCR失敗では残りの画像を継続する
- 失敗画像も`status: FAILED`、`error_message`付きでイベント情報を保存する
- 1枚以上成功すればJob全体は`SUCCEEDED`とし、統計とwarningを残す
- 全画像が失敗した場合はJobを`FAILED`とし、既存の`analysis/screens.json`を変更しない

音声、文字起こし、話者分離結果は変更しない。

## 10. 出力スキーマ

```json
{
  "schema_version": 1,
  "status": "SUCCEEDED",
  "completed_at": "2026-08-08T20:30:00.000+09:00",
  "runtime": "windows-media-ocr",
  "language": "ja",
  "statistics": {
    "total": 1,
    "succeeded": 1,
    "failed": 0
  },
  "screens": [
    {
      "sequence": 1,
      "timestamp_ms": 1600,
      "timestamp": 1.6,
      "image": "screenshots/000001.png",
      "width": 890,
      "height": 797,
      "reason": "initial",
      "metrics": {"changed_ratio": 1.0},
      "status": "SUCCEEDED",
      "type": "browser",
      "title": "新しいタブ",
      "summary": "画面内テキスト: 新しいタブ ...",
      "important": [],
      "ocr": {
        "language": "ja",
        "text": "新しいタブ ...",
        "lines": [
          {"text": "新しいタブ", "x": 10.0, "y": 10.0, "width": 80.0, "height": 18.0}
        ]
      },
      "error_message": null
    }
  ],
  "warnings": []
}
```

JSONは同一ディレクトリの`.tmp`へwrite、flush、`fsync`した後に`os.replace()`する。

## 11. Job状態

`analysis/jobs.json`の`screen_analysis`へ次を保存する。

- `RUNNING`
- `SUCCEEDED`
- `FAILED`
- `CANCELED`

成功時の`output_path`は`analysis/screens.json`。アプリ終了時に`RUNNING`が残った場合は、次回起動時に「前回中断」と表示して再実行を許可する。

## 12. GUI

- `解析対象`で録音済みセッションを選択
- 画像イベントと画像がある場合だけ「画面解析」を有効化
- 状態は未実行、実行中、完了、失敗、キャンセル、前回中断
- 実行中はボタンを「キャンセル」に変更
- OCR進捗を画像件数で表示
- 実行中は録音、STT、話者分離、解析対象変更を無効化

Phase 4では画像内容のプレビューとOCR編集UIを作らない。

## 13. ログとプライバシー

- 画像、OCR本文、タイトル、重要事項をapplication logへ出さない
- worker標準出力はprogressとresult pathだけ
- worker標準エラーは例外種別と診断だけ
- `screens.json`は会議データとしてアプリディレクトリ内に保持する

## 14. テスト

### 14.1 単体テスト

- timestamp順sort
- 画面種別分類
- 重要行抽出
- OCR行座標のJSON化
- Unicode画像パス
- path traversal拒否
- 画像単位の部分失敗
- 全失敗時に既存結果を保持
- atomic JSON生成

### 14.2 Job・GUIテスト

- Job成功、開始失敗、キャンセル状態保存
- 会議一覧の実行可否と状態復元
- GUIからの実行
- 他AI Jobとの排他
- アプリ終了時のキャンセル

### 14.3 実機PoC

- Windows日本語OCR言語パックを検出
- 実際のWGC保存画像をworkerで解析
- `timestamp_ms`がイベントと一致
- 日本語と英数字のOCR行が1件以上生成
- worker終了後にプロセスが残らない

## 15. 正常系受入条件

- GUIから画面解析を実行、キャンセル、再実行できる
- 推論中もGUIが応答する
- 1枚以上の保存画像からOCR結果を生成できる
- 全screenにセッション開始基準timestampがある
- 全screenに成功または失敗状態がある
- `screens.json`が再実行可能な中間データとして残る
- 部分失敗で成功画像を失わない
- 全失敗で以前の成功結果を失わない
- 録音原本、画像原本、Phase 2・3出力を変更しない
- アプリ再起動後にJob状態を表示できる

## 16. 精度改善候補

1. Qwen3-VL 2B/4BのQ4量子化runtime比較
2. OCR全文と画像をVLMへ同時入力するstructured JSON prompt
3. 類似画像の解析結果再利用
4. presentation title領域等のlayout-aware抽出
5. 表・課題管理画面から担当、期限、状態を構造化
6. OCR/VLM信頼度とユーザー確認UI

