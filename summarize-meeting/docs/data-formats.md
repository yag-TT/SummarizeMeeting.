# 保存データ形式

## 1. 保存ルート

既定では全てアプリルート配下へ保存します。

```text
<app-root>/
├─ data/
│  ├─ settings.json
│  ├─ instance.lock
│  ├─ logs/application.log
│  └─ meetings/<session>/...
└─ models/
   ├─ faster-whisper/
   ├─ sherpa-onnx/diarization/
   └─ paddleocr/
```

`SUMMARIZE_MEETING_APP_ROOT`でアプリルートを上書きできます。ポータブル配置を前提とし、ユーザーのホームディレクトリへ暗黙に会議データを分散しません。

## 2. セッションディレクトリ

ディレクトリ名は`YYYY-MM-DD_HHMMSS_<sanitized-title>_<session-id-prefix>`です。

```text
data/meetings/<session>/
├─ session.json
├─ events.jsonl
├─ logs/
│  └─ session.log
├─ audio/
│  ├─ microphone.wav
│  ├─ system_audio.wav
│  ├─ manifest.json
│  └─ .work/<track>/*.wav
├─ screenshots/
│  ├─ events.jsonl
│  └─ 000001.png ...
├─ analysis/
│  ├─ jobs.json
│  ├─ transcription.json
│  ├─ diarization.json
│  ├─ speaker_names.json
│  ├─ diarized_transcription.json
│  ├─ screens.json
│  ├─ timeline.json
│  └─ minutes.json
└─ output/
   ├─ transcript.md
   └─ minutes.md
```

成果物は機能を実行した場合だけ作成されます。

## 3. `data/settings.json`

schema versionは1です。

```json
{
  "schema_version": 1,
  "last_microphone_device_id": null,
  "last_system_device_id": null,
  "screen_evaluation_fps": 2.0,
  "screen_change_thresholds": {
    "pixel_diff_threshold": 16,
    "changed_area_ratio_threshold": 0.03,
    "mean_abs_diff_threshold": 4.0,
    "debounce_ms": 500,
    "stable_changed_area_ratio": 0.01,
    "timeout_ms": 5000
  },
  "retention": {
    "keep_audio": true,
    "keep_screenshots": true
  },
  "llm": {
    "base_url": null
  },
  "auto_transcribe_after_recording": false,
  "log_level": "INFO"
}
```

主な検証範囲:

| フィールド | 範囲・値 |
|---|---|
| `screen_evaluation_fps` | 0.1〜10.0 |
| `pixel_diff_threshold` | 1〜255 |
| 各ratio | 0.0〜1.0 |
| `mean_abs_diff_threshold` | 0.0〜255.0 |
| `debounce_ms` | 0〜60000 |
| `timeout_ms` | 1〜300000 |
| `llm.base_url` | `null`または有効なHTTP(S) URL |
| `log_level` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

不正な設定は`settings.corrupt-<timestamp>.json`へ退避し、既定値で起動します。

## 4. `session.json`

schema versionは2です。

```json
{
  "title": "設計レビュー",
  "id": "9e0f9d92-6ab9-4cc7-96de-b7fe0a9acec8",
  "schema_version": 2,
  "status": "RECORDED",
  "started_at": "2026-08-11T10:00:00+09:00",
  "ended_at": "2026-08-11T10:30:00+09:00",
  "duration_ms": 1800000,
  "monotonic_origin_ns": 123456789000,
  "audio": {
    "microphone": {
      "id": "device-id",
      "name": "Microphone",
      "channels": 1,
      "is_loopback": false
    }
  },
  "screen": {
    "id": "screen-id",
    "title": "共有画面",
    "kind": "screen"
  },
  "components": {
    "microphone": {"status": "STOPPED", "error_code": null, "message": null},
    "system_audio": {"status": "NOT_CONFIGURED", "error_code": null, "message": null},
    "screen": {"status": "STOPPED", "error_code": null, "message": null},
    "session_storage": {"status": "STOPPED", "error_code": null, "message": null}
  },
  "retention": {"keep_audio": true, "keep_screenshots": true},
  "warnings": [],
  "platform": {"system": "Windows", "release": "11"}
}
```

### Session status

| 値 | 意味 |
|---|---|
| `CREATED` | domain初期値 |
| `PREPARING` | セッション作成済み、入力初期化中 |
| `RECORDING` | 録音中 |
| `STOPPING` | 停止要求送信中 |
| `FINALIZING` | WAV・manifest・メタデータ確定中 |
| `RECORDED` | 正常確定。録音後解析の対象 |
| `INTERRUPTED` | 中断または一部確定失敗 |
| `FAILED_TO_START` | 録音開始失敗 |

### Component status

`NOT_CONFIGURED`, `READY`, `STARTING`, `RUNNING`, `RECONNECTING`, `PAUSED`, `STOPPING`, `STOPPED`, `FAILED`を使用します。

warningは`code`, `message`, `timestamp_ms`を持ちます。`timestamp_ms`はセッションのmonotonic originからの経過です。

## 5. `events.jsonl`

録音セッション全体のイベントを1行1 JSONで追記します。

```json
{"schema_version":1,"timestamp_ms":1200,"type":"component_state_changed","component":"microphone","status":"RUNNING","error_code":null,"message":null}
```

代表的な`type`は`session_preparing`, `session_started`, `session_stopping`, `session_finished`, `component_state_changed`, `screen_target_replaced`, `low_disk_space`, `settings_write_failed`, `audio_work_cleanup_failed`です。イベントごとに追加フィールドが異なります。

## 6. `audio/manifest.json`

schema versionは3です。

```json
{
  "schema_version": 3,
  "monotonic_origin_ns": 123456789000,
  "tracks": {
    "microphone": {
      "file": "microphone.wav",
      "sample_rate": 48000,
      "channels": 1,
      "sample_width_bytes": 2,
      "frames_written": 1440000,
      "segments": 3,
      "estimated_start_offset_ms": 20,
      "capture_ended_offset_ms": 30020,
      "audio_duration_ms": 30000.0,
      "active_capture_duration_ms": 29980.0,
      "duration_drift_ms": 20.0,
      "overflow_count": 0,
      "queue_pressure_count": 0,
      "max_queue_usage_ratio": 0.1,
      "queue_capacity_chunks": 64,
      "gaps": [],
      "validated": true,
      "work_files_removed": true,
      "work_cleanup_error": null
    }
  }
}
```

track名は`microphone`または`system_audio`です。`file`は`audio/`基準の単純なファイル名だけを許可し、絶対パスやサブディレクトリ参照は拒否します。

`gaps`の各要素は`start_ms`, `end_ms`, `reconnect_attempts`, `outcome`を持ちます。

## 7. `screenshots/events.jsonl`

PNGと1対1になるイベントです。

```json
{"schema_version":1,"sequence":1,"timestamp_ms":2500,"file":"000001.png","width":1920,"height":1080,"reason":"changed","metrics":{"changed_ratio":0.12,"mean_abs_diff":8.4}}
```

イベントファイルはatomicに再公開され、更新に失敗した場合は対応PNGを正式成果物として残しません。

## 8. `analysis/jobs.json`

schema versionは1です。各解析種類の最新attemptだけを保持します。

```json
{
  "schema_version": 1,
  "jobs": {
    "transcription": {
      "job": "transcription",
      "status": "SUCCEEDED",
      "attempt_id": "2f0fc4db-cded-4f43-9c06-b56576dbfb38",
      "started_at": "2026-08-11T10:31:00.000+09:00",
      "ended_at": "2026-08-11T10:32:30.000+09:00",
      "model": "large-v3-turbo",
      "language": "ja",
      "output_path": "output/transcript.md",
      "error_message": null
    }
  }
}
```

job名は`transcription`, `diarization`, `screen_analysis`, `minutes`です。statusは`RUNNING`, `SUCCEEDED`, `FAILED`, `CANCELED`です。UIでは成果物ファイルに古い`SUCCEEDED`が残っていても、`jobs.json`の最新statusを優先します。

## 9. `analysis/transcription.json`

schema versionは1です。

```json
{
  "schema_version": 1,
  "status": "SUCCEEDED",
  "model": "large-v3-turbo",
  "requested_language": "ja",
  "completed_at": "2026-08-11T10:32:30.000+09:00",
  "tracks": [
    {
      "source": "microphone",
      "file": "microphone.wav",
      "start_offset_ms": 20,
      "detected_language": "ja",
      "language_probability": 0.99,
      "duration_seconds": 30.0,
      "segment_count": 4,
      "runtime_device": "cpu"
    }
  ],
  "segments": [
    {
      "start": 0.42,
      "end": 2.18,
      "source": "microphone",
      "text": "会議を始めます。",
      "avg_logprob": -0.12,
      "no_speech_prob": 0.01
    }
  ]
}
```

`start`と`end`はセッション共通時刻の秒です。manifestの`estimated_start_offset_ms`が加算されています。

## 10. 話者分離成果物

### `analysis/diarization.json`

schema version 1で、runtime、使用モデル、`provider`、入力PC音声、話者数設定、話者一覧、話者区間、warningを保存します。

```json
{
  "schema_version": 1,
  "status": "SUCCEEDED",
  "provider": "cpu",
  "source": {"file": "system_audio.wav", "start_offset_ms": 15, "duration_seconds": 30.0},
  "config": {"speaker_count": "auto", "cluster_threshold": 0.75, "min_duration_on": 0.3, "min_duration_off": 0.5},
  "speakers": [{"id": "speaker_01", "default_name": "Speaker 1", "turn_count": 3}],
  "turns": [{"start": 1.0, "end": 3.0, "audio_start": 0.985, "audio_end": 2.985, "speaker_id": "speaker_01"}],
  "warnings": []
}
```

### `analysis/speaker_names.json`

```json
{"schema_version":1,"updated_at":"2026-08-11T10:35:00.000+09:00","names":{"speaker_01":"田中"}}
```

### `analysis/diarized_transcription.json`

schema version 1です。`segments`は元の文字起こし情報に`speaker_id`, `speaker_name`, `ambiguous`などの話者割当情報を加えた配列です。会話要約はこのファイルが成功状態なら元の`transcription.json`より優先します。

## 11. `analysis/screens.json`

schema versionは1です。

```json
{
  "schema_version": 1,
  "status": "SUCCEEDED",
  "runtime": "paddleocr-3.7/PP-OCRv6-medium-onnx",
  "language": "ja",
  "statistics": {"total": 2, "succeeded": 1, "failed": 1},
  "screens": [
    {
      "sequence": 1,
      "timestamp_ms": 2500,
      "image": "screenshots/000001.png",
      "status": "SUCCEEDED",
      "type": "document",
      "title": "設計方針",
      "summary": "設計方針 ...",
      "important": ["決定事項"],
      "ocr": {
        "language": "ja",
        "text": "設計方針\n決定事項",
        "lines": [{"text":"設計方針","x":10.0,"y":20.0,"width":100.0,"height":24.0}]
      },
      "error_message": null
    }
  ],
  "warnings": []
}
```

個別画像の`status`は`SUCCEEDED`または`FAILED`です。全画像が失敗した場合は`screens.json`を成功成果物として公開しません。

## 12. 会話要約成果物

### `analysis/timeline.json`

schema version 1です。発話は`speech-00001`、画面は`screen-00001`のような根拠IDを持ち、`timestamp_ms`順で並びます。

```json
{
  "schema_version": 1,
  "session": {"title":"設計レビュー","started_at":"...","ended_at":"..."},
  "sources": {"transcript":"analysis/diarized_transcription.json","screens":"analysis/screens.json"},
  "statistics": {"speech_count":4,"screen_count":1},
  "items": [
    {"id":"speech-00001","kind":"speech","timestamp_ms":420,"start":0.42,"end":2.18,"source":"microphone","speaker_id":"self","speaker_name":"自分","ambiguous":false,"text":"会議を始めます。"}
  ],
  "warnings": []
}
```

### `analysis/minutes.json`

schema versionは3です。

```json
{
  "schema_version": 3,
  "status": "SUCCEEDED",
  "generation_id": "uuid",
  "completed_at": "2026-08-11T10:40:00.000+09:00",
  "runtime": "llama.cpp OpenAI-compatible API",
  "model": "model-id",
  "source": {
    "timeline": "analysis/timeline.json",
    "timeline_sha256": "...",
    "chunk_count": 1
  },
  "minutes": {
    "summary": "...",
    "participants": ["自分", "田中"],
    "conversation_flow": [
      {
        "title": "設計案の説明と確認",
        "detail": "自分が設計案を説明し、田中が前提条件を確認した。",
        "uncertain": false,
        "evidence_ids": ["speech-00001", "speech-00002"],
        "start_ms": 420,
        "end_ms": 18200,
        "speakers": ["自分", "田中"]
      }
    ],
    "topics": [{"title":"...","summary":"...","evidence_ids":["speech-00001"]}],
    "key_points": [{"text":"...","evidence_ids":["speech-00001"]}],
    "decisions": [],
    "todos": [],
    "pending": [],
    "references": []
  },
  "warnings": []
}
```

LLMが返した根拠IDは実在するtimeline itemだけに絞られます。`conversation_flow`の時刻範囲と話者は根拠IDから算出され、開始時刻順に整列されます。根拠がない主張や不正な生成トークンは除外または再構成され、`warnings`へ記録されます。

`timeline.json`, `minutes.json`, `output/minutes.md`は同じ`ArtifactPublisher`処理で一括公開されます。

## 13. Markdown成果物

- `output/transcript.md`: 時刻、話者、発話本文を人間向けに表示します。話者分離または話者名更新後は同じファイルを再生成します。
- `output/minutes.md`: 会話情報、要約、時刻・話者付きの詳細な会話の流れ、話題、要点、明示的な決定・TODO・未解決事項、画面参照を表示します。不確実なフロー項目には文字起こしが不明瞭である旨を表示します。

JSONが機械処理用の正本で、Markdownは表示用派生成果物です。

## 14. ログ形式

### `data/logs/application.log`

プレーンテキストです。時刻、level、logger、process、thread、messageを持ちます。5 MiBごとにローテーションし、3世代保持します。

### `<session>/logs/session.log`

1行1 JSONです。

```json
{"timestamp":"2026-08-11T10:00:01.000+09:00","level":"INFO","event":"session_started","session_id":"...","timestamp_ms":1000,"details":{"screen_configured":true}}
```

機密フィールドと登録済み機密値は`[REDACTED]`へ置換されます。

## 15. 復旧データ

復旧後の`session.json`には`recovery`が追加されます。

```json
{
  "recovery": {
    "recovered_at": "2026-08-11T11:00:00+09:00",
    "tracks": [],
    "screenshots": ["000003.png"],
    "warnings": []
  }
}
```

復旧WAVは既存の最終WAVや`.work` segmentを上書きせず、`microphone.recovered.wav`などの別名で生成します。
