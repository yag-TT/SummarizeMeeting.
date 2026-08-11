# 開発・テストガイド

## 1. 前提

- Python 3.11以上
- uv
- Git
- Windows 11、Ubuntu 22.04、またはWSL2

依存バージョンは`pyproject.toml`と`uv.lock`で管理します。OSをまたいで`.venv`を共有しないでください。

## 2. 初期セットアップ

### Windows

```console
uv sync --frozen
uv run summarize-meeting
```

### Ubuntu 22.04 / WSL2

詳細は[Ubuntu 22.04へのコピー導入手順](ubuntu-install.md)を参照してください。

```bash
bash scripts/setup_ubuntu.sh --models all
bash scripts/run_ubuntu.sh
```

## 3. モデル準備

```console
uv run python scripts/setup_models.py diarization
uv run python scripts/setup_models.py ocr
uv run python scripts/setup_models.py all
```

- faster-whisperモデルは文字起こし初回実行時に`models/faster-whisper/`へ取得されます。
- 話者分離モデルは`models/sherpa-onnx/diarization/`へ配置します。
- OCRモデルは`models/paddleocr/`へ配置し、固定revisionとSHA-256を検証します。

## 4. LLM設定

会話要約を使う場合だけ、`data/settings.json`へOpenAI互換API URLを設定します。

```json
{
  "schema_version": 1,
  "llm": {
    "base_url": "http://llm-host:8081/v1"
  }
}
```

環境変数は設定ファイルより優先されます。

```text
SUMMARIZE_MEETING_LLM_URL=http://llm-host:8081/v1
SUMMARIZE_MEETING_LLM_MODEL=<model-id>
SUMMARIZE_MEETING_APP_ROOT=<portable-root>
```

## 5. 日常の検証

変更前後の基本コマンド:

```console
uv run ruff check .
uv run pytest -q
```

CIもWindows Server 2022とUbuntu 22.04で同じruff・pytestを実行します。GUIテスト時は`QT_QPA_PLATFORM=offscreen`です。

### テスト配置

```text
tests/
├─ unit/         # fake backend、fault injection、状態遷移、純粋ロジック
└─ integration/  # Recorder、Qt capture、実モデル境界などの統合
```

実モデル・実機依存テストには環境条件によるskipがあります。skipの理由を消すためだけにテストを弱めないでください。

### 関連テストの選び方

| 変更対象 | 最低限の関連テスト |
|---|---|
| 録音開始・入力 | `test_recording_startup.py`, `test_source_refresh.py` |
| 録音確定 | `test_recording_controller_finalize.py`, `test_audio_writer.py` |
| セッション保存・復旧 | `test_session_repository.py`, `test_recovery_service.py` |
| 解析ジョブ制御 | `test_analysis_job_runner.py`, 各`*_controller.py` |
| 文字起こし | `test_transcription.py`, `test_transcription_worker.py` |
| 話者分離 | `test_diarization.py`, `test_diarization_worker.py` |
| 画面解析 | `test_screen_analysis.py`, `test_screen_analysis_controller.py` |
| 会話要約 | `test_minutes.py`, `test_minutes_worker.py` |
| 複数成果物・I/O | `test_atomic_io.py`, `test_screenshot_store.py` |
| UI | `test_settings_integration.py`, `test_analysis_stage.py` |

## 6. 非破壊診断

```console
uv run python scripts/doctor.py
```

次を診断します。

- OS、desktop session、Wayland/X11/WSLg
- Portal、PipeWire、PulseAudio
- 音声入力とloopback
- 保存先の書込権限
- OCRモデル
- faster-whisper用CUDA
- sherpa-onnx wheel、CUDA共有ライブラリ、話者分離モデル初期化

診断は設定変更やモデル削除を行いません。

## 7. Worker単独デバッグ

GUIを介さず録音済みセッションへ解析を実行できます。完全な引数は[APIリファレンス](api-reference.md#8-worker-cli)を参照してください。

```console
uv run python -m summarize_meeting.processing.transcription_worker --session "data/meetings/<session>" --models-dir "models"
uv run python -m summarize_meeting.processing.diarization_worker --session "data/meetings/<session>" --models-dir "models"
uv run python -m summarize_meeting.processing.screen_analysis_worker --session "data/meetings/<session>" --models-dir "models"
uv run python -m summarize_meeting.processing.minutes_worker --session "data/meetings/<session>" --base-url "http://llm-host:8081/v1"
```

stdoutにはJSON Lines、失敗診断にはstderrを使います。PowerShellやshell scriptから利用する場合、stdoutへ独自の説明文を混ぜないでください。

## 8. 開発補助ツール

### 長時間文字起こし用セッション

```console
uv run python -m summarize_meeting.devtools.benchmark_session \
  --source-session data/meetings/stt-smoke-ja \
  --output-session data/meetings/stt-benchmark-1h \
  --duration-seconds 3600
```

### 実音声スモーク

```console
uv run python -m summarize_meeting.devtools.real_audio_smoke \
  --source-wave data/meetings/stt-smoke-ja/audio/system_audio.wav \
  --microphone "Virtual microphone" \
  --loopback "Monitor source" \
  --speaker "Virtual speaker"
```

### 録音済みセッション検証

```console
uv run python -m summarize_meeting.devtools.validate_phase2_session \
  --session "data/meetings/<session>" \
  --expect-microphone "確認文" \
  --expect-system-audio "確認文"
```

## 9. ログによる調査

1. `data/logs/application.log`でアプリ全体のERROR/WARNINGを確認。
2. 対象セッションの`logs/session.log`で時刻順イベントを確認。
3. `session.json`のstatus、components、warningsを確認。
4. `analysis/jobs.json`で最新attemptのstatusと`error_message`を確認。
5. 音声問題では`audio/manifest.json`のgaps、overflow、queue pressure、duration driftを確認。

ログは機密値をマスクします。調査コードを追加する場合も、会議名、パス、デバイス名・ID、発話本文を通常ログへ直接出さないでください。

## 10. 新機能追加の指針

### 新しい録音後解析

1. `processing/`へBackend ProtocolとServiceを追加。
2. Serviceでschema、前段成果物、パスを検証。
3. `processing/<name>_worker.py`を追加し、共通JSON Lines形式で進捗と結果を出す。
4. `application/<name>_controller.py`で前提条件とコマンドを定義。
5. `AnalysisJobRunner`へライフサイクルを委譲。
6. `AnalysisWorkflow`とUIへ接続。
7. `FileSessionCatalog`へ実行可否・状態表示を追加。
8. unit test、worker test、fault injection test、文書を追加。

### 新しい保存成果物

- 単一ファイルはatomic I/O helperを使用。
- 複数ファイルが同じ世代を構成する場合は`ArtifactPublisher`を使用。
- セッションルート外への書き込みを拒否。
- schema version、必須フィールド、失敗時の旧成果物扱いを定義。
- [保存データ形式](data-formats.md)を更新。

### 新しい設定

1. `AppSettings`へ型付きフィールドを追加。
2. `from_dict()`へ型・範囲検証と既定値を追加。
3. UIまたは環境変数との優先順位を定義。
4. `test_settings.py`と本書を更新。

## 11. コーディング上の注意

- GUI threadでモデル推論、デバイス列挙、録音確定を実行しない。
- 捕捉したERROR/WARNINGはアプリログまたはセッションログへ残す。
- 読込`OSError`を「ファイルなし」や空データとして扱って上書きしない。
- worker成功出力は必ずセッション配下に置く。
- `RUNNING`を保存してからworkerを開始し、必ず終端状態へ遷移させる。
- thread/processを追加する場合はcancelとbounded waitを設計する。
- 実モデルのないテストではProtocolにfakeを注入する。

## 12. ドキュメント更新チェックリスト

- 機能や制約の変更: `project-overview.md`
- モジュール責務や処理順の変更: `architecture.md`
- クラス、Signal、CLI、HTTP契約の変更: `api-reference.md`
- JSONフィールド、schema、ディレクトリ変更: `data-formats.md`
- セットアップ、モデル、テスト変更: `development.md`と必要に応じて`ubuntu-install.md`
- 入口や読む順序の変更: `docs/README.md`とルート`README.md`
