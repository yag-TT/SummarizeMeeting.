# Summarize Meeting Phase 5 詳細設計

## 1. 目的

Phase 5では、Phase 2またはPhase 3の話者付き文字起こしとPhase 4の画面解析結果をセッション開始基準の時刻で統合し、再利用可能な`analysis/timeline.json`、検証済み構造データ`analysis/minutes.json`、Confluenceへ貼り付けやすい`output/minutes.md`を生成する。

議事録生成にはLAN内のllama.cpp serverへ導入済みのモデルを使用する。アプリへllama.cppやLLMモデルを同梱・ダウンロードする処理はPhase 5正常系PoCの対象外とする。

## 2. 前提

- 正式対象はWindows 11
- `session.json`が`RECORDED`
- `analysis/transcription.json`が`SUCCEEDED`
- `http://192.168.1.158:8081/v1`でllama.cppのOpenAI互換APIへ接続できる
- llama.cppでLLMが1つロード済み、またはモデルIDを明示している
- 会議データは指定したLAN内サーバー以外へ送信しない
- 画面解析は任意。未実行でも音声だけから生成できる
- AI JobはGUIプロセスと分離する

## 3. 対象範囲

### 3.1 対象

- 話者付き文字起こしを優先したtimeline統合
- 話者分離未実行時の通常文字起こしへのフォールバック
- 成功済み画面解析結果の任意統合
- timestampによる安定sort
- 長時間入力の分割生成と最終統合
- llama.cpp OpenAI互換APIのStructured Output
- 根拠IDによる生成結果検証
- 相対期限、担当者、画面由来決定事項の保守的な補正
- Markdown議事録生成
- GUIからの実行、キャンセル、再実行
- Job状態の永続化

### 3.2 対象外

- llama.cpp、Ollama、LLM重みのアプリ同梱
- llama.cpp serverの自動インストール
- 会議中のリアルタイム要約
- クラウドLLMへのフォールバック
- 議事録のGUI編集
- Confluence APIへの投稿
- 発話内容の事実確認
- 音声認識やOCR原文の自動修正

## 4. 技術選定

| 項目 | 採用案 |
|---|---|
| LLM runtime | LAN内の既存llama.cpp server |
| API | `http://192.168.1.158:8081/v1/chat/completions` |
| 出力制約 | JSON Schema Structured Output |
| HTTP client | Python標準`urllib` |
| Job分離 | child Python process |
| 長時間処理 | 文字数上限によるmap-reduce |
| 生成後検証 | evidence IDと決定的規則 |
| 出力 | UTF-8 JSON / Markdown |

接続先には有効な`http`または`https` URLを許可する。既定のLAN内接続はHTTPのため、会議内容は暗号化されずに送信される。

## 5. 構成

```text
GUI process
  MainWindow
    -> MinutesController
         -> child Python process
              -> MinutesService
                   -> timeline builder
                   -> LlamaCppMinutesBackend
                   -> evidence validator
                   -> analysis/timeline.json
                   -> analysis/minutes.json
                   -> output/minutes.md

llama.cpp server
  -> already installed / already downloaded model
```

録音、文字起こし、話者分離、画面解析、議事録生成は同時実行しない。キャンセル時はPython workerのプロセスツリーを終了する。

## 6. LLM接続設定

既定値:

- base URL: `http://192.168.1.158:8081/v1`
- model: 自動選択

自動選択では`GET /v1/models`に見えるモデルが1つの場合だけ採用する。複数ある場合は誤選択を避けて失敗し、次の環境変数で明示する。

```powershell
$env:SUMMARIZE_MEETING_LLM_URL = "http://192.168.1.158:8081/v1"
$env:SUMMARIZE_MEETING_LLM_MODEL = "<model-id>"
uv run summarize-meeting
```

llama.cpp server側の例:

```powershell
llama-server --host 0.0.0.0 --port 8081 --model <model.gguf> --ctx-size 16384
```

## 7. timeline入力選択

文字起こしは次の優先順とする。

1. `analysis/diarized_transcription.json`が`SUCCEEDED`
2. `analysis/transcription.json`が`SUCCEEDED`

通常文字起こしでは`microphone`を`自分`、`system_audio`を`PC音声`とする。話者付き文字起こしでは保存済み`speaker_id`と`speaker_name`を使用する。

`analysis/screens.json`が`SUCCEEDED`なら、画像単位で`SUCCEEDED`の項目だけを使う。未実行または不正状態なら音声だけで継続しwarningを残す。

## 8. `analysis/timeline.json`

```json
{
  "schema_version": 1,
  "generated_at": "2026-08-08T20:52:06.410+09:00",
  "session": {
    "title": "開発定例",
    "started_at": "2026-08-08T19:00:00+09:00",
    "ended_at": "2026-08-08T20:00:00+09:00"
  },
  "sources": {
    "transcript": "analysis/diarized_transcription.json",
    "screens": "analysis/screens.json"
  },
  "statistics": {"speech_count": 10, "screen_count": 3},
  "items": [
    {
      "id": "speech-00001",
      "kind": "speech",
      "timestamp_ms": 17539,
      "start": 17.539,
      "end": 21.879,
      "source": "system_audio",
      "speaker_id": "speaker_01",
      "speaker_name": "田中",
      "ambiguous": false,
      "text": "来週の金曜日までに、テスト結果を共有します。"
    },
    {
      "id": "screen-00001",
      "kind": "screen",
      "timestamp_ms": 18000,
      "image": "screenshots/000001.png",
      "screen_type": "presentation",
      "title": "テスト計画",
      "summary": "画面内テキスト: ...",
      "important": ["期限: 来週金曜日"]
    }
  ],
  "warnings": []
}
```

sort keyは`timestamp_ms`、`kind`、`id`とする。各項目へ安定した根拠IDを付ける。

## 9. 長時間会議

timeline項目をJSON化した文字数で最大24,000文字ずつに分割する。各chunkを同一スキーマで整理し、2chunk以上なら部分結果をもう一度LLMへ渡して統合する。

Phase 5ではtokenizer依存を避けるため文字数を保守的な上限として使う。分割数は`analysis/minutes.json`へ記録する。

## 10. Structured Output

出力項目:

- `summary`
- `participants`
- `topics[]`
- `key_points[]`
- `decisions[]`
- `todos[]`
- `pending[]`
- `references[]`

会議、雑談、相談、インタビューなどの種類を決めつけず、`summary`、`topics[]`、`key_points[]`を中心に会話内容を整理する。明示的な合意、今後の対応、未解決事項がある場合だけ`decisions[]`、`todos[]`、`pending[]`へ格納する。各構造化項目は`evidence_ids[]`を必須とする。API要求では`response_format.type=json_schema`、`strict=true`を指定し、推論表示は`reasoning_effort=none`とする。

## 11. 生成後検証

LLMのJSON Schema適合だけでは内容の正しさを保証できないため、保存前に次を適用する。

1. timelineに存在しないevidence IDを除外
2. 有効な根拠が0件になった項目を除外
3. 決定・TODO・保留は発話、またはカテゴリ固有keywordを含む`important`画面を必須化
4. TODO担当者が参加者名にも根拠本文にもなければ`不明`
5. TODO期限が根拠本文に存在しなければ、相対日付・日付表現を根拠から抽出し直す
6. 期限が抽出できなければ`不明`
7. `<|...`等の生成制御tokenを除去
8. 要約に生成制御tokenが混入した場合は根拠発話の抽出的要約へフォールバック
9. 参考情報はscreen evidenceを必須化し、画像パスとtimestampをtimelineから上書き

検証による除外・補正は`warnings`へ残す。根拠IDがあるだけで意味的正しさが保証されるわけではないため、重要会議では人による確認が必要である。

## 12. `analysis/minutes.json`

```json
{
  "schema_version": 2,
  "status": "SUCCEEDED",
  "generation_id": "uuid",
  "completed_at": "2026-08-08T20:52:15.086+09:00",
  "runtime": "llama.cpp OpenAI-compatible API",
  "model": "Infatoshi_Qwen3.6-35B-A3B-GGUF_Qwen3.6-35B-A3B-Q5_K_M.gguf",
  "source": {
    "timeline": "analysis/timeline.json",
    "timeline_sha256": "...",
    "chunk_count": 1
  },
  "minutes": {
    "summary": "テスト結果の共有予定を確認した。",
    "participants": ["自分", "田中"],
    "topics": [
      {
        "title": "テスト結果の共有",
        "summary": "共有時期について話した。",
        "evidence_ids": ["speech-00002"]
      }
    ],
    "key_points": [
      {
        "text": "テスト結果は来週の金曜日までに共有される。",
        "evidence_ids": ["speech-00002"]
      }
    ],
    "decisions": [],
    "todos": [
      {
        "assignee": "不明",
        "task": "テスト結果を共有する",
        "deadline": "来週の金曜日",
        "evidence_ids": ["speech-00002"]
      }
    ],
    "pending": [],
    "references": []
  },
  "warnings": []
}
```

`timeline_sha256`により会話要約がどのtimelineから生成されたかを確認できる。

## 13. `output/minutes.md`

次の順で生成する。

1. 記録名
2. 会話情報（記録日時、長さ、話者）
3. 会話の要約
4. 主な話題
5. 会話の要点
6. 明確な合意・決定（存在する場合だけ）
7. 今後の対応表（存在する場合だけ）
8. 未解決・確認事項（存在する場合だけ）
9. 関連する画面情報（存在する場合だけ）
10. 要約時の注意（warningがある場合）

一般的な会話でも空の会議用見出しが並ばないよう、補助的な節は内容がある場合だけ出力する。画面リンクは`output/`から見た`../screenshots/...`を使う。

## 14. 再実行と失敗

- timelineはLLM呼び出し前にatomic保存する
- LLM接続・生成失敗では既存`minutes.json`と`minutes.md`を変更しない
- 成功時は各ファイルを`.tmp`、flush、`fsync`、`os.replace()`で確定する
- 話者名変更後は会話要約を再実行できる
- llama.cpp server未起動時は起動案内を含むエラーを表示する
- 複数モデルが見えるのにモデル未指定なら誤選択せず失敗する

## 15. JobとGUI

`analysis/jobs.json`の`minutes`へ`RUNNING`、`SUCCEEDED`、`FAILED`、`CANCELED`を保存する。成功時の`output_path`は`output/minutes.md`。

GUIは文字起こし成功後に「会話要約」を有効化する。実行中は他のAI Jobと録音を無効化し、ボタンを「キャンセル」にする。再起動後も保存済みJob状態を表示する。

## 16. テスト

- 話者付き文字起こし優先
- 通常文字起こしへのフォールバック
- 画面解析なしでの成功
- speech/screenのtimestamp統合
- map-reduce
- JSON Schema API要求
- LAN内HTTP endpointの受理とHTTP(S)以外の拒否
- 複数モデル自動選択拒否
- 根拠なし項目の除外
- OCRだけからの決定推測の除外
- 架空の絶対期限を原文へ補正
- 生成制御tokenの除去
- Markdown各節と画面相対リンク
- 雑談などで会議用の空セクションを出力しないこと
- Job成功、失敗、キャンセル
- 全Phase回帰テスト

## 17. 正常系受入条件

- 話者付き文字起こしと画面解析から`timeline.json`を生成できる
- 画面解析がなくても音声だけで生成できる
- 既存llama.cppモデルだけを使用し、データを指定したLAN内サーバー以外へ送信しない
- `minutes.json`と`minutes.md`を生成できる
- 会議以外の会話でも全体要約、主な話題、会話の要点を生成できる
- TODOの担当不明・期限不明を明示できる
- 根拠のない重要項目を保存前に除外できる
- 再実行、キャンセル、再起動後状態表示ができる
- Phase 1〜4の原本・中間結果を変更しない

## 18. 後続候補

1. llama.cppモデル選択GUIと接続テスト
2. 設定ファイルへのモデルID保存
3. llama.cpp serverの認証・TLS対応
4. 長時間会議のtoken-aware chunking
5. 生成項目ごとの根拠プレビュー・承認UI
6. Confluence API投稿
