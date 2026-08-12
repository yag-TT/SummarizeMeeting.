# 内部API・worker APIリファレンス

## 1. APIの範囲

このプロジェクトはライブラリとしての後方互換な公開APIや、外部クライアント向けの受信HTTP APIを提供していません。本書の「API」は、プロジェクト内部で安定した境界として扱うPythonクラス、Qt Signal、worker CLI、worker JSON Lines、外向きllama.cpp HTTP通信を指します。

アンダースコアで始まる関数・属性は内部実装です。拡張コードからはController、Service、Repository、domain dataclassを優先して使用してください。

## 2. エントリーポイント

| 入口 | 内容 |
|---|---|
| `uv run summarize-meeting` | GUIアプリを起動 |
| `python -m summarize_meeting` | GUIアプリを起動 |
| `summarize_meeting.main()` | Pythonエントリーポイント |
| `summarize_meeting.bootstrap.main()` | Qt依存構築とイベントループ開始。終了コードを返す |

アプリルートは`PortableAppPaths.discover()`で決定します。`SUMMARIZE_MEETING_APP_ROOT`が設定されていればそのパス、frozen実行では実行ファイルの親、それ以外はソースツリーのプロジェクトルートです。

## 3. domain API

### `AudioDevice`

```python
AudioDevice(id: str, name: str, channels: int, is_loopback: bool = False)
```

### `ScreenTarget`

```python
ScreenTarget(id: str, title: str, kind: Literal["window", "screen", "portal"] = "window")
```

### `RecordingSession`

録音状態の集約です。主な操作は次の通りです。

```python
session.set_component(
    kind: ComponentKind,
    status: ComponentStatus,
    *,
    error_code: str | None = None,
    message: str | None = None,
) -> None

session.add_warning(code: str, message: str, timestamp_ms: int) -> None
session.to_dict() -> dict[str, Any]
RecordingSession.now_iso() -> str
```

状態値は[保存データ形式](data-formats.md#session-status)を参照してください。

### `AnalysisJobState`

```python
state = AnalysisJobState.start(job="transcription", model="large-v3-turbo", language="ja")
terminal = state.finish(
    AnalysisJobStatus.SUCCEEDED,
    output_path="output/transcript.md",
)
value = terminal.to_dict()
```

`finish()`へ指定できるのは`SUCCEEDED`、`FAILED`、`CANCELED`だけです。各開始で新しい`attempt_id`が生成されます。

## 4. RecordingController

`RecordingController`はQObjectです。デバイスI/Oや録音処理をバックグラウンドthreadへ送り、UIへSignalを返します。

### 主なproperty・同期API

```python
controller.meetings_directory -> Path
controller.is_recording -> bool
controller.is_screen_previewing -> bool
controller.is_audio_previewing -> bool
controller.last_microphone_device_id -> str | None
controller.last_system_device_id -> str | None
controller.auto_transcribe_after_recording -> bool

controller.list_input_devices() -> list[AudioDevice]
controller.list_loopback_devices() -> list[AudioDevice]
controller.list_screen_targets() -> list[ScreenTarget]
controller.set_auto_transcribe_after_recording(enabled: bool) -> None
```

### 非同期操作

```python
controller.refresh_sources_async(request_id: int) -> None
controller.preview_screen_target_async(request_id: int, target: ScreenTarget) -> None
controller.cancel_screen_preview() -> None
controller.preview_audio_sources_async(
    request_id: int,
    microphone: AudioDevice | None,
    system_audio: AudioDevice | None,
) -> None
controller.cancel_audio_preview() -> None
```

同じ`request_id`が完了Signalに返ります。UIは最新requestだけを採用し、古い非同期結果を無視できます。音声プレビュー、画面プレビュー、本録音は同時実行できません。

### 録音操作

```python
session_path = controller.start_session(
    title="設計レビュー",
    microphone=microphone_or_none,
    system_audio=loopback_or_none,
    screen_target=screen_or_none,
)
controller.replace_screen_target(new_target)
controller.stop_session()
completed = controller.stop_for_shutdown(timeout_seconds=4.0)
```

`start_session()`はセッションディレクトリを同期的に作成して`Path`を返し、デバイス初期化は非同期です。マイクとPC音声の両方が`None`の場合は`ValueError`です。`stop_session()`は冪等に近い要求APIで、停止対象がなければ何もしません。

### Signal

| Signal | 引数 | 意味 |
|---|---|---|
| `sources_refreshed` | `(request_id: int, snapshot: object)` | 入力列挙完了。値は`CaptureSourcesSnapshot` |
| `screen_preview_ready` | `(request_id: int, frame: object)` | BGR `numpy.ndarray`を取得 |
| `screen_preview_failed` | `(request_id: int, message: str)` | 画面プレビュー失敗 |
| `screen_preview_cancelled` | `(request_id: int)` | 画面プレビュー取消完了 |
| `audio_preview_finished` | `(request_id: int, errors: object)` | 音声テスト完了。値はエラー文字列tuple |
| `session_preparing` | `(session_path: str)` | セッション作成済み、入力準備中 |
| `session_started` | `(session_path: str)` | 最低1つの音声入力で録音開始 |
| `session_start_failed` | `(session_path: str, message: str)` | 録音開始失敗 |
| `session_start_cancelled` | `(session_path: str)` | 準備中キャンセル完了 |
| `finalize_progress` | `(percent: int, message: str)` | 録音確定進捗 |
| `session_finished` | `(session_path: str)` | 録音確定完了。エラー時も発火 |
| `component_changed` | `(component: str, status: str, detail: str)` | 入力・保存コンポーネント状態変更 |
| `meter_changed` | `(component: str, level: float)` | 正規化音声レベル |
| `screenshot_count_changed` | `(count: int)` | 保存済み画面数 |
| `fatal_error` | `(message: str)` | UIへ通知すべき保存・入力エラー |

## 5. 解析Controller

4つのControllerは共通ライフサイクルを持ちます。

```python
controller.is_running -> bool
controller.cancel() -> None
controller.wait(timeout_seconds: float | None = None) -> bool
controller.shutdown(timeout_seconds: float | None = None) -> bool
```

共通Signal:

| Signal | 引数 |
|---|---|
| `job_started` | `(session_path: str)` |
| `job_progress` | `(percent: int, message: str)` |
| `job_finished` | `(session_path: str, output_path: str)` |
| `job_failed` | `(session_path: str, message: str)` |
| `job_canceled` | `(session_path: str)` |

固有の開始API:

```python
TranscriptionController.start(session_directory: Path) -> None
DiarizationController.start(
    session_directory: Path,
    *,
    speaker_count: int | None = None,
) -> None
DiarizationController.update_speaker_names(
    session_directory: Path,
    names: Mapping[str, str],
) -> Path
ScreenAnalysisController.start(session_directory: Path) -> None
MinutesController.start(session_directory: Path) -> None
MinutesController.is_configured -> bool
```

全Controllerは`meetings_dir`外のセッションを拒否します。話者数は`None`（自動）または1〜10です。

### `AnalysisWorkflow`

```python
workflow = AnalysisWorkflow((transcription, diarization, screen_analysis, minutes))
workflow.any_running -> bool
workflow.other_running(current) -> bool
workflow.start(current, action) -> None
workflow.cancel_all() -> None
workflow.shutdown(timeout_seconds: float) -> bool
workflow.availability(...) -> AnalysisAvailability
```

`start()`は判定と開始処理を同じlock内で行い、別解析との競合開始を防ぎます。

## 6. Processing Service API

ServiceはQObjectやthreadを必要としない同期APIです。テストや独自CLIではBackendを注入して直接利用できます。

### 文字起こし

```python
backend = FasterWhisperBackend(
    model_name="large-v3-turbo",
    models_directory=models_dir / "faster-whisper",
)
service = TranscriptionService(backend, model_name="large-v3-turbo")
output = service.run(session_dir, language="ja", progress_callback=callback)
```

戻り値は`output/transcript.md`です。`TranscriptionBackend.transcribe()`を実装すれば推論Backendを差し替えられます。

### 話者分離

```python
backend = SherpaOnnxDiarizationBackend(
    segmentation_model=model_root / "segmentation/model.int8.onnx",
    embedding_model=model_root / "embedding/nemo_en_titanet_small.onnx",
)
service = DiarizationService(backend, cluster_threshold=0.75)
output = service.run(session_dir, speaker_count=None, progress_callback=callback)
output = service.update_speaker_names(session_dir, {"speaker_01": "田中"})
```

戻り値は更新された`output/transcript.md`です。

### 画面解析

```python
backend = create_screen_analysis_backend(
    models_directory=models_dir / "paddleocr",
    language="ja",
)
service = ScreenAnalysisService(backend)
output = service.run(session_dir, progress_callback=callback)
```

戻り値は`analysis/screens.json`です。1画像の失敗はwarningへ集約し、1件以上成功すれば全体を成功とします。

### 会話要約

```python
backend = LlamaCppMinutesBackend(
    base_url="http://llm-host:8081/v1",
    model=None,
    max_output_tokens=4096,
    timeout_seconds=600,
)
service = MinutesService(backend, max_chunk_characters=12_000)
output = service.run(session_dir, progress_callback=callback)
```

戻り値は`output/minutes.md`です。長い入力は12,000文字単位で分割し、部分要約も同じ上限を目安に隣接する時系列単位から段階的に統合します。`MinutesBackend.generate(prompt, schema)`を実装すればLLM Backendを差し替えられます。

生成JSONには、要約、話題、要点に加えて`conversation_flow`が必須です。各項目の`title`、`detail`、`uncertain`、`evidence_ids`をLLMが返し、`start_ms`、`end_ms`、`speakers`は検証済みの根拠からアプリが算出します。文字起こしの明白な誤認識だけを文脈から補正し、確信できない箇所は`uncertain: true`として断定を避けます。

`progress_callback`はいずれも`Callable[[int, str], None]`です。percentは0〜100へ正規化されます。

## 7. Repository・ファイルAPI

### `PortableAppPaths`

```python
paths = PortableAppPaths.discover()
paths.ensure_writable()

paths.app_root
paths.data_dir
paths.models_dir
paths.meetings_dir
paths.logs_dir
paths.settings_file
paths.lock_file
```

### `FileSessionRepository`

```python
repository = FileSessionRepository(paths.meetings_dir)
session_paths = repository.create(session)
repository.save(session_paths, session)
repository.append_event(session_paths, event)
```

### `FileSessionCatalog`

```python
summaries: tuple[SessionSummary, ...] = FileSessionCatalog(paths.meetings_dir).scan()
```

現行schemaのセッションだけを新しい順で返し、解析可否も計算します。

### `FileAnalysisJobRepository`

```python
repository.save(session_directory, analysis_job_state) -> Path
```

戻り値は`analysis/jobs.json`です。破損JSONは`jobs.json.corrupt-<uuid>`へ退避します。

### atomic I/O

```python
write_json_atomic(path, value)
write_text_atomic(path, content)
write_bytes_atomic(path, content)

publisher = ArtifactPublisher(session_directory)
publisher.publish({json_path: json_bytes(value), markdown_path: markdown_bytes})
publisher.publish_text({path_a: text_a, path_b: text_b})
```

`ArtifactPublisher`はroot外の公開先を`ValueError`で拒否します。

## 8. Worker CLI

### 文字起こし

```console
uv run python -m summarize_meeting.processing.transcription_worker \
  --session "data/meetings/<session>" \
  --models-dir "models" \
  [--model large-v3-turbo] \
  [--language ja]
```

### 話者分離

```console
uv run python -m summarize_meeting.processing.diarization_worker \
  --session "data/meetings/<session>" \
  --models-dir "models" \
  [--speaker-count 2] \
  [--cluster-threshold 0.75]
```

### 画面解析

```console
uv run python -m summarize_meeting.processing.screen_analysis_worker \
  --session "data/meetings/<session>" \
  --models-dir "models" \
  [--language ja]
```

### 会話要約

```console
uv run python -m summarize_meeting.processing.minutes_worker \
  --session "data/meetings/<session>" \
  --base-url "http://llm-host:8081/v1" \
  [--model <model-id>]
```

全workerは成功時0、失敗時1を返します。失敗の最終診断はstderrへ出力します。

## 9. Worker JSON Linesプロトコル

stdoutはUTF-8の1行1 JSONです。

進捗:

```json
{"type":"progress","percent":42,"message":"処理しています"}
```

成功結果:

```json
{"type":"result","path":"D:/.../output/transcript.md"}
```

RunnerはJSONでない行を最大20行の診断bufferへ保持します。終了コード0かつ`result.path`があり、その解決先がセッション配下の場合だけ成功です。

## 10. llama.cpp HTTP API

アプリが呼び出す外向きAPIです。URLは`data/settings.json`の`llm.base_url`、または優先度の高い`SUMMARIZE_MEETING_LLM_URL`から取得します。

### モデル解決

モデル未指定時:

```http
GET {base_url}/models
Accept: application/json
```

`data[].id`が1件なら自動選択します。0件または複数件はエラーです。複数モデル時は`SUMMARIZE_MEETING_LLM_MODEL`またはworkerの`--model`を指定します。

### 要約生成

```http
POST {base_url}/chat/completions
Content-Type: application/json
Accept: application/json
```

主なrequestフィールド:

- `model`
- `messages`
- `temperature: 0.2`
- `max_tokens`
- `reasoning_effort: "none"`
- `stream: false`
- `response_format.type: "json_schema"`
- strictな`conversation_summary` JSON Schema

responseは`choices[0].message.content`にJSON文字列を含む必要があります。HTTPエラー本文は診断用に先頭1000文字まで取り込みます。

## 11. 例外と失敗の扱い

| 境界 | 主な例外 |
|---|---|
| 入力・設定検証 | `ValueError`, `RuntimeError` |
| 文字起こし | `TranscriptionError` |
| 話者分離 | `DiarizationError` |
| 画面解析 | `ScreenAnalysisError` |
| 会話要約・LLM通信 | `MinutesError` |
| WAV検証 | `WaveValidationError` |
| スクリーンショット保存 | `ScreenshotSaveError` |
| アプリルート書込不可 | `AppRootNotWritableError` |

Controllerはworker失敗をQtの`job_failed`へ変換します。Serviceを直接呼ぶ場合は、呼び出し側が各例外を処理してください。
