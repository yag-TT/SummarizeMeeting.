# 会議議事録作成ツール Phase 1 詳細設計

更新日: 2026-08-08

状態: Draft（製品判断の回答反映済み、実機PoC項目を含む）

対象: Phase 1「記録基盤」のWindows 11先行実装

## 1. 文書の位置付け

本書は [`CODEX_HANDOFF_MEETING_MINUTES_TOOL.md`](./CODEX_HANDOFF_MEETING_MINUTES_TOOL.md) の確定要件を、実装可能な粒度へ落とし込む詳細設計である。

本書では以下を扱う。

- Pythonプロジェクトとパッケージ構成
- GUI、Application、Capture、Storageの責務境界
- マイク音声とPC再生音声の並行録音
- 音声メーター
- 対象ウィンドウ選択と画面変更検知
- セッション状態、保存形式、時刻同期、障害復旧
- テスト戦略とPhase 1完了条件
- 実装前にユーザー確認が必要な事項
- 実機PoCによって決定する事項

文字起こし、話者分離、OCR/VLM、議事録LLMの内部設計はPhase 2以降で別途定義する。Phase 1では、それらが再処理可能になるよう原本と時刻情報を保存するところまでを対象とする。

## 2. 設計原則

優先順位は次のとおりとする。

1. 音声データを失わない
2. セッションメタデータを失わない
3. UIを応答可能な状態に保つ
4. 画面変更を適切に保存する
5. 利便性や見た目を改善する

以下を禁止する。

- UIスレッドでのブロッキング録音、画像処理、ファイル書き込み
- 1つのWorker障害による他の正常Workerの強制停止
- 会議中のWhisper、VLM、LLM起動
- FFmpegおよびQt MultimediaのFFmpegバックエンドへの依存
- Teams / Google Meet内部APIへの依存
- 会議データの外部サービス送信
- セッション終了確認前の原本削除

## 3. Phase 1のスコープ

### 3.1 対象

- Windows 11向けPySide6デスクトップUI
- マイク入力デバイスの列挙と録音
- PC出力デバイスの列挙とWASAPI Loopback録音
- マイクとPC音声の別トラック保存
- 各音声ソースのレベルメーター
- OSまたはアプリUIによる対象ウィンドウ選択
- 低頻度のフレーム評価と意味のある変更時のPNG保存
- `session.json`、イベントログ、アプリログの保存
- 正常停止、部分障害、次回起動時の復旧検査
- Fake Captureを使用した自動テスト
- Windows実機での15分および1時間連続試験

### 3.2 対象外

- Ubuntu Capture実装（Windows検証後に同じPortへ追加する）
- STT、話者分離、画面内容理解、議事録生成
- リアルタイム字幕、リアルタイム議事録
- 特定アプリだけの音声抽出
- 動画保存
- 自動削除
- 配布用exeの生成
- Windowsのスリープ／休止状態をまたぐ録音継続および専用復旧処理

## 4. 技術基準

### 4.1 Pythonと依存管理

- Gitリポジトリ直下の `summarize-meeting/` をPythonプロジェクトルートとする。
- `pyproject.toml`、`uv.lock`、`.python-version`、`.venv`、`src/`、`tests/` は `summarize-meeting/` 配下に置く。
- 設計文書はGitリポジトリ直下の `docs/` に置く。
- 依存管理・仮想環境・実行は `uv` を使用する。
- 初期基準はPython 3.11とする。
- `uv.lock` は再現性確保のためGit管理対象とする。
- 実行時依存と開発時依存を分離する。
- Windows固有依存には環境マーカーを付与する。
- Phase 1のCapture PoCが完了するまで、画面Captureライブラリを最終固定しない。

想定コマンドは以下とする。

```powershell
uv sync
uv run meeting-minutes
uv run pytest
uv run ruff check .
```

依存候補は以下である。追加は各機能の実装開始時に `uv add` で行い、未使用候補を先行追加しない。

| 区分 | 候補 | 用途 | 決定状態 |
|---|---|---|---|
| Runtime | PySide6 | GUIとスレッド間Signal | 採用予定 |
| Runtime | numpy | 音声変換、RMS、画像差分補助 | 採用予定 |
| Runtime | SoundCard | WASAPIマイク・Loopback PoC | PoC対象 |
| Runtime | opencv-python-headless | 画像縮小・差分・保存 | 採用予定 |
| Runtime | Windows Runtime bridge | Windows.Graphics.Capture接続 | PoCで選定 |
| Dev | pytest | 単体・結合テスト | 採用予定 |
| Dev | pytest-qt | GUI Signalと状態試験 | 採用予定 |
| Dev | ruff | lint・format | 採用予定 |
| Dev | mypy | Port境界の型検査 | 採用予定 |

SoundCardはWindows/WASAPIとLoopbackを提供するが、公式READMEにはWindowsでの単一チャンネル録音、blocksize無視、buffer underrunに関する既知事項が記載されている。このため、SoundCardを最終実装と決め打ちせず、`AudioBackend` Portの背後で実機評価する。

### 4.2 保存形式の初期値

| データ | Phase 1形式 | 理由 |
|---|---|---|
| 音声 | PCM 16-bit little-endian WAV | 可搬性、復旧容易性、STT入力との相性 |
| スクリーンショット | PNG | 可逆、ライブラリ差が少ない |
| メタデータ | UTF-8 JSON | 人が確認でき、スキーマ化しやすい |
| イベント | UTF-8 JSONL | 追記可能で障害時に全体を失いにくい |
| ログ | UTF-8テキスト | ローカル調査用 |

WebPへの切替は1時間試験で、PNGとの容量、保存CPU時間、読込互換性を比較した後に決める。

### 4.3 配置・実行方式

- アプリはインストーラーで導入せず、配布フォルダを任意の書込み可能な場所へコピーして使用する。
- 管理者権限、Windows Registry、`Program Files`、Windowsサービスへの登録を前提にしない。
- アプリ本体、設定、ログ、会議データを1つのポータブルフォルダ内で完結させる。
- 会議データや設定を `%LOCALAPPDATA%`、Documentsなどへ暗黙に保存しない。
- 配置先が書込み不可の場合は起動時preflightで明示し、別の場所へコピーするよう案内する。ユーザーが認識しない別パスへのfallbackは行わない。
- 同じアプリフォルダからの同時起動は禁止する。コピー先が異なる別アプリフォルダは、それぞれ独立した `data/` を持つため別インスタンスとして扱う。
- Phase 6では、PyInstaller / Nuitkaの比較に加え、展開済みフォルダをZIP等で配布し、解凍またはコピーだけで起動できることを受入条件にする。

配布フォルダの完成イメージ:

```text
SummarizeMeeting/
├─ SummarizeMeeting.exe
├─ runtime/                 # Python・DLL・アプリ依存物（方式はPhase 6で決定）
├─ models/                  # Phase 2以降のローカルモデル
├─ licenses/
└─ data/
   ├─ settings.json
   ├─ instance.lock
   ├─ logs/
   │  └─ application.log
   └─ meetings/
      └─ <session>/
```

アプリ更新時は `data/` を保持したまま本体を置き換えられる構造にする。配布物には空の `data/` だけを含め、会議データを含めない。

単一起動制御にはアプリルートごとの `QLockFile` を使用する。2つ目のプロセスは録音画面を開かず、「このアプリフォルダのSummarize Meetingは既に起動しています」と表示して終了する。異常終了後のstale lockは、プロセス生存確認を行ったうえで回復する。

## 5. 論理アーキテクチャ

```text
PySide6 Main Thread
  MainWindow / RecordingPage
        |
        | command / state / signal
        v
Application Layer
  RecordingController
  SessionManager
  WorkerSupervisor
        |
        +---------------------+---------------------+
        |                     |                     |
        v                     v                     v
Mic Capture Worker      System Capture Worker   Screen Worker
        |                     |                     |
        v                     v                     v
Mic Writer Worker       System Writer Worker    Screenshot Store
        |                     |                     |
        +---------------------+---------------------+
                              |
                              v
                       Session Repository
                    session.json / events.jsonl
```

### 5.1 依存方向

依存方向は外側から内側へ向ける。

```text
ui -> application -> domain
capture adapters -> capture ports -> domain
infrastructure adapters -> application/domain ports
```

禁止する依存:

- `domain` からPySide6、SoundCard、OpenCVへの依存
- `ui` からSoundCardやWindows APIの直接呼び出し
- `capture` からWidgetの直接更新
- Workerから別Workerの内部状態を直接操作

## 6. パッケージ構成

Phase 1完了時の構成を以下とする。

```text
summarize-meeting/
├─ pyproject.toml
├─ uv.lock
├─ src/meeting_minutes/
│  ├─ __init__.py
│  ├─ __main__.py
│  ├─ bootstrap.py
│  ├─ ui/
│  │  ├─ main_window.py
│  │  ├─ recording_page.py
│  │  ├─ device_models.py
│  │  └─ widgets/
│  │     └─ level_meter.py
│  ├─ application/
│  │  ├─ recording_controller.py
│  │  ├─ session_manager.py
│  │  ├─ worker_supervisor.py
│  │  ├─ recovery_service.py
│  │  └─ ports/
│  │     ├─ clock.py
│  │     ├─ session_repository.py
│  │     └─ event_sink.py
│  ├─ domain/
│  │  ├─ session.py
│  │  ├─ capture.py
│  │  ├─ events.py
│  │  ├─ errors.py
│  │  └─ value_objects.py
│  ├─ capture/
│  │  ├─ audio/
│  │  │  ├─ base.py
│  │  │  ├─ worker.py
│  │  │  ├─ soundcard_backend.py
│  │  │  └─ pcm.py
│  │  └─ screen/
│  │     ├─ base.py
│  │     ├─ worker.py
│  │     ├─ windows_graphics_capture.py
│  │     └─ change_detector.py
│  ├─ infrastructure/
│  │  ├─ file_session_repository.py
│  │  ├─ audio_writer.py
│  │  ├─ screenshot_store.py
│  │  ├─ settings.py
│  │  ├─ logging.py
│  │  └─ system_clock.py
│  └─ resources/
│     └─ icons/
└─ tests/
   ├─ unit/
   ├─ integration/
   ├─ fakes/
   └─ manual/
```

`windows_graphics_capture.py` の名称は第一候補を示す。PoCで別方式になっても、`capture/screen/base.py` の契約と上位層は変更しない。

## 7. ドメインモデル

### 7.1 識別子と時刻

- `SessionId`: UUID v4文字列
- `TimestampNs`: セッション基準の単調増加ナノ秒
- `UtcDateTime`: ISO 8601、タイムゾーン付き
- 内部計算は整数ナノ秒を使用し、JSONでは `timestamp_ms` に整数ミリ秒を保存する
- 表示時だけ `HH:MM:SS` へ変換する

壁時計はNTP補正や手動変更で前後するため、イベントの順序と音声同期には使用しない。セッション開始時に以下の対応だけを保存する。

```text
started_at_utc <-> monotonic_origin_ns
```

### 7.2 Session

```python
Session
  id: SessionId
  schema_version: int
  title: str
  status: SessionStatus
  started_at: datetime | None
  ended_at: datetime | None
  duration_ms: int | None
  audio: AudioSessionConfig
  screen: ScreenSessionConfig
  retention: RetentionPolicy
  component_status: dict[ComponentKind, ComponentStatus]
  warnings: list[SessionWarning]
```

`Session` は永続化可能な状態だけを保持し、OSハンドル、Recorder、QThread、NumPy配列を保持しない。

### 7.3 SessionStatus

```text
CREATED
  -> PREPARING
  -> RECORDING
  -> STOPPING
  -> FINALIZING
  -> RECORDED

PREPARING / RECORDING / STOPPING / FINALIZING
  -> INTERRUPTED

PREPARING
  -> FAILED_TO_START
```

- 一部Componentが失敗しても、1つ以上の音声トラックが記録中ならセッション全体は `RECORDING` を維持し、warningを付ける。
- マイクまたはPC音声の片方が開始できない場合、警告確認ダイアログを表示せず、取得可能なトラックで録音を開始する。欠けているComponentは状態ランプとテキストで常時明示する。
- 全音声Componentが停止した場合は重大警告を表示する。画面Captureだけが失敗しても音声は継続する。
- 次回起動時に未完了セッションを検出した場合は `INTERRUPTED` とし、復旧処理を実行可能にする。

### 7.4 ComponentStatus

対象Component:

- `MICROPHONE`
- `SYSTEM_AUDIO`
- `SCREEN`
- `SESSION_STORAGE`

状態:

```text
NOT_CONFIGURED
READY
STARTING
RUNNING
RECONNECTING
PAUSED
STOPPING
STOPPED
FAILED
```

- `RECONNECTING` は音声デバイス切断後に同じデバイスへ再接続している状態である。
- `PAUSED` は対象ウィンドウ最小化などにより画面frameを一時取得できない状態である。Phase 1ではScreen Componentにのみ使用する。

各状態変更は `events.jsonl` へ記録する。

## 8. Port設計

以下は概念契約であり、実装時には `typing.Protocol`、immutable dataclass、Enumを使用する。

### 8.1 AudioBackend

```python
class AudioBackend(Protocol):
    def list_input_devices(self) -> Sequence[AudioDevice]: ...
    def list_loopback_devices(self) -> Sequence[AudioDevice]: ...
    def open_stream(self, config: AudioStreamConfig) -> AudioStream: ...

class AudioStream(Protocol):
    @property
    def actual_format(self) -> AudioFormat: ...
    def read(self, requested_frames: int) -> AudioChunk: ...
    def close(self) -> None: ...
```

`AudioChunk`:

```python
AudioChunk
  samples: numpy.ndarray       # frames x channels, float32 -1.0..1.0
  frame_index: int             # このトラック内の先頭frame
  captured_at_ns: int          # read完了時のmonotonic時刻
  overflowed: bool
```

### 8.2 ScreenCaptureBackend

```python
class ScreenCaptureBackend(Protocol):
    def is_supported(self) -> bool: ...
    def pick_target(self, owner_window_handle: int) -> ScreenTarget | None: ...
    def open_stream(self, target: ScreenTarget) -> ScreenStream: ...

class ScreenStream(Protocol):
    def read_latest(self, timeout_ms: int) -> ScreenFrame | None: ...
    def close(self) -> None: ...
```

`ScreenTarget` に保存するのは表示名、種類、再選択用の安全な識別情報だけとし、再利用不能な生OSハンドルを `session.json` に永続化しない。

### 8.3 SessionRepository

```python
class SessionRepository(Protocol):
    def create(self, session: Session) -> SessionPaths: ...
    def save(self, session: Session) -> None: ...
    def append_event(self, event: SessionEvent) -> None: ...
    def find_interrupted(self) -> Sequence[SessionId]: ...
```

`save()` は同一ディレクトリ内の一時ファイルへ書き、flush後に `os.replace()` する。一部だけ書かれた `session.json` を残さない。

## 9. 録音開始シーケンス

```text
User                   UI            Controller       Workers/Storage
 | [会議開始]           |                 |                 |
 |-------------------->| validate        |                 |
 |                     |---------------->| preflight       |
 |                     |                 |---- create dir ->|
 |                     |                 |---- open mic ---->|
 |                     |                 |---- open loopback>|
 |                     |                 |---- open screen -->|
 |                     |                 |<--- ready/error ---|
 |                     |                 | persist PREPARING |
 |                     |                 | set monotonic t0  |
 |                     |                 | release start gate|
 |                     |                 | persist RECORDING |
 |                     |<----------------| state + warnings  |
 |<--------------------| recording UI    |                 |
```

開始処理の順序:

1. 会議名、固定保存先、選択デバイス、画面対象を検証する。
2. 空き容量を検査する。
3. セッションディレクトリと初期 `session.json` を作る。
4. 各Capture streamを開き、最初のread直前まで準備する。
5. Writerを先に起動し、書込み可能状態を確認する。
6. 全Workerがreadyになったら共通start gateを解放する。
7. `monotonic_origin_ns` と `started_at` を確定する。
8. セッションを `RECORDING` に遷移させる。
9. UIタイマーを開始する。

マイクまたはPC音声の片方だけがreadyになった場合は、確認ダイアログを挟まずにstart gateを解放する。両方ともreadyにならない場合は音声会議記録として開始できないため `FAILED_TO_START` とする。Screenだけがreadyにならない場合は、画面状態をFAILEDとして音声録音を開始する。

start gateは完全なサンプル同期を保証しない。各Audio Workerは最初のread完了時刻とchunk長から、そのトラックの推定開始offsetを計算し、manifestへ保存する。

`start_session` は入力検証、容量preflight、Session作成、session log作成までをUI threadで完了した後、`session_preparing` を通知して専用startup threadを開始し、直ちにUIへ制御を返す。デバイスopen、READY待ち、Screen Worker準備、容量監視開始はstartup threadで行い、最大5秒のAudio開始待ちでUI event loopを停止させない。

PREPARING中の終了操作はstartup cancellation eventを設定する。startup threadはREADY待ちを最大50 ms間隔で中断確認し、未解放のAudio start gateを解放して各Workerをcooperativeに停止した後、Sessionを `FAILED_TO_START`、warningを `SESSION_START_CANCELLED` として確定する。Screen・容量監視・Sessionの `RECORDING` 遷移・Audio gate解放・`session_started` 通知は同じController排他区間で確定し、開始と停止の競合を防ぐ。

## 10. 音声取得詳細

### 10.1 PC音声の範囲

WASAPI Loopbackは選択した出力エンドポイントに再生される音声全体を対象とする。TeamsやChromeなど特定プロセスだけへ限定しない。通知音、音楽、別アプリ音声も同じ出力先なら記録される。

特定プロセスの音声だけを取得する機能はPhase 1対象外とし、UIには「PC音声は選択したスピーカーへ出力される全音声を記録します」と表示する。

### 10.2 AudioStreamConfig

初期要求値:

```text
sample_rate: 48000 Hz
sample_format: backend float32 -> storage PCM16
requested_block_duration: 100 ms
exclusive_mode: false
channels: backend既定またはPoCで確認した複数channel map
```

- 48 kHzでopenできない場合は44.1 kHzを試し、実際の値を保存する。
- マイクとPC音声のsample rate・channel数は同一でなくてよい。
- Phase 1では会議中にresampleやmono downmixを行わない。
- STT用の16 kHz mono変換はPhase 2の派生処理とする。
- Windows/SoundCardの単一channel既知問題を避けるため、PoCでは「単一channelを明示要求」と「backend既定channel」の両方を録音比較する。

### 10.3 WorkerとQueue

音声ソースごとにCapture WorkerとWriter Workerを分離する。

```text
Audio Capture Worker
  blocking read
  -> validate ndarray
  -> calculate meter
  -> enqueue AudioChunk

Audio Writer Worker
  dequeue
  -> clip -1.0..1.0
  -> PCM16 conversion
  -> append WAV segment
  -> checkpoint
```

Queue方針:

- ソースごとに独立したbounded queueを持つ。
- 初期容量は30秒相当とする。
- queue使用率80%でwarningを記録する。
- queue満杯時は古いchunkを暗黙破棄しない。
- 書込み不能が継続した場合はそのComponentを `FAILED` とし、UIへ重大警告を通知する。
- 音声データを守るため、スクリーンショット保存より音声Writerを優先する。

実装ではenqueue直後の `qsize / maxsize` を計測し、80%へ到達したepisodeごとに `AUDIO_QUEUE_PRESSURE` warningを1回記録する。50%以下へ戻るまでは同じwarningを繰り返さない。最大使用率とepisode数を最終manifestへ保存する。

queue満杯時は1秒まで空きを待つ。空かなければ `overflow_count` を増やし、chunkを捨てて継続せず、そのAudio Componentを `FAILED / AUDIO_QUEUE_PRESSURE` にする。Writerは既にqueueへ入ったchunkをdrainしてWAVを確定する。

### 10.4 レベルメーター

各chunkから以下を計算する。

```text
peak = max(abs(samples))
rms = sqrt(mean(samples ** 2))
dbfs = 20 * log10(max(rms, 1e-12))
normalized = clamp((dbfs - floor_db) / (0 - floor_db), 0, 1)
floor_db = -60 dBFS
```

- Capture WorkerからUIへ最大10 Hzで値を通知する。
- UIは約150 msのrelease smoothingを適用して視認性を上げる。
- メーター更新をqueueへ蓄積しない。未描画値がある場合は最新値で上書きする。
- 無音が続いても録音停止とは判断しない。

### 10.5 WAV保全

1時間録音と異常終了復旧のため、会議中は内部segmentへ保存し、正常終了時に最終WAVへ統合する。

```text
audio/
├─ .work/
│  ├─ microphone/
│  │  ├─ 000000.wav
│  │  └─ 000001.wav
│  └─ system/
│     ├─ 000000.wav
│     └─ 000001.wav
├─ microphone.wav
├─ system.wav
└─ manifest.json
```

- segment目標長は60秒とする。
- segment切替はWriter側で行い、Captureを停止しない。
- 完了segmentごとにframe数とbyte数をmanifestへatomic保存する。
- 正常停止時は現在segmentをcloseし、同一formatのsegmentを順に結合する。
- 最終WAVを検証してから `.work` を削除する。
- 最終WAVは再openし、PCM形式、sample rate、channel数、sample幅、header上のframe数、末尾まで実際に読めたframe数、durationを検証する。
- WAV検証失敗時はfinalize失敗としてsegmentを保持する。検証成功後の `.work` 削除だけが失敗した場合は、WAVを有効として `AUDIO_WORK_CLEANUP_FAILED` warningを記録する。
- 統合中に失敗した場合はsegmentを残し、再試行可能にする。
- 次回起動時に未完了segmentを検出したら、開けるsegmentだけでrecovered WAVを生成する。
- 最終ファイルが4 GiBへ近づく構成では標準RIFF WAVの上限が問題になるため、事前容量計算で警告する。1時間・PCM16・48 kHzの想定内で実測する。

### 10.6 同期情報

`audio/manifest.json` の概念スキーマ:

```json
{
  "schema_version": 1,
  "monotonic_origin_ns": 123456789000,
  "tracks": {
    "microphone": {
      "sample_rate": 48000,
      "channels": 2,
      "sample_width_bytes": 2,
      "estimated_start_offset_ms": 18,
      "capture_ended_offset_ms": 3600021,
      "frames_written": 172800000,
      "audio_duration_ms": 3600000.0,
      "active_capture_duration_ms": 3600003.0,
      "duration_drift_ms": -3.0,
      "overflow_count": 0,
      "queue_pressure_count": 0,
      "max_queue_usage_ratio": 0.12,
      "queue_capacity_chunks": 300,
      "segments": 60,
      "validated": true,
      "work_files_removed": true,
      "work_cleanup_error": null,
      "gaps": []
    },
    "system": {
      "sample_rate": 48000,
      "channels": 2,
      "sample_width_bytes": 2,
      "estimated_start_offset_ms": 34,
      "capture_ended_offset_ms": 3600037,
      "frames_written": 172800000,
      "audio_duration_ms": 3600000.0,
      "active_capture_duration_ms": 3600003.0,
      "duration_drift_ms": -3.0,
      "overflow_count": 0,
      "queue_pressure_count": 1,
      "max_queue_usage_ratio": 0.83,
      "queue_capacity_chunks": 300,
      "segments": 60,
      "validated": true,
      "work_files_removed": true,
      "work_cleanup_error": null,
      "gaps": []
    }
  }
}
```

音声タイムライン上の時刻は次で算出する。

```text
timestamp_sec = estimated_start_offset_sec + frame_index / sample_rate
```

最初の非空chunkについて、`read完了時刻 - chunk duration - monotonic origin` を `estimated_start_offset_ms` とする。スケジューリング誤差などで負になる推定値は0へclampする。

終了時は次を記録する。

```text
audio_duration_ms = frames_written * 1000 / sample_rate
active_capture_duration_ms = capture_ended_offset_ms
                           - estimated_start_offset_ms
                           - sum(reconnect_gap_duration_ms)
duration_drift_ms = audio_duration_ms - active_capture_duration_ms
```

`duration_drift_ms` はデバイスクロック、backend buffering、queue待ちを診断する値であり、Phase 1ではこの値を使ったstretchやsilence挿入を行わない。

### 10.7 音声デバイス切断と再接続

- 録音中にデバイス切断を検出した場合、対象Componentを `RECONNECTING` にして黄色ランプを表示する。
- 別のデバイスや新しい既定デバイスへ自動で切り替えない。
- セッション開始時と同じ安定device IDだけを再openする。
- 初期値として2秒間隔、最大5回、合計約10秒間再接続を試行する。
- 再接続成功時は新しいWAV segmentを開始し、Componentを `RUNNING` に戻す。
- 切断開始、再接続成功、再接続断念を `events.jsonl` に記録する。
- 切断中の時間は音声gapとしてmanifestへ記録し、無音sampleを暗黙に挿入しない。
- 再接続できなければComponentを `FAILED` にし、赤ランプを表示する。もう一方のAudioとScreenは継続する。
- 再接続回数と間隔は実機PoCで調整可能な設定値とするが、通常UIには露出しない。

## 11. 画面取得詳細

### 11.1 Windows Graphics Capture PoC

画面取得backendには `Windows.Graphics.Capture` を採用する。Python側はPyWinRT 3.2.1を使用する。

- アプリ内の列挙UIで選択したHWNDを `create_for_window(HWND)` へ渡す。OS pickerは重ねて表示しない。
- Hardware D3D11 deviceからWinRT `IDirect3DDevice` を生成する。
- `Direct3D11CaptureFramePool.create_free_threaded` を使用し、Screen Worker内でsessionを維持する。
- pixel formatは `B8G8R8A8UIntNormalized`、buffer数は2とする。
- frame surfaceは `SoftwareBitmap.create_copy_from_surface_async` でCPU側へcopyし、BGRのNumPy配列へ正規化する。
- cursor captureとcapture borderは無効にする。無効化に失敗した場合はScreen開始失敗として扱い、音声は継続する。
- 静止画で新規frameが来ない評価周期は、最後に取得したframeのcopyを返す。ScreenChangeDetectorが同一frameを再保存しない。
- DWMのextended frame boundsとframe sizeを照合し、古いsizeのframeを破棄する。
- 対象のリサイズ時はframe poolをrecreateし、新sizeのframeをbaseline候補にする。
- session、frame pool、surface、SoftwareBitmap、D3D deviceはScreen Worker終了時に解放する。

PoCではPySide6の自己ウィンドウについて、別Workerからの初回取得、静止中の連続評価、リサイズ追従、BGR画素値、正常解放を確認済みである。UI Threadを停止した状態では対象の再描画も止まるため、画面取得は必ずScreen Workerで実行する。

旧MSS Adapterは比較・診断用にソースを残すが、通常のRecordingControllerからは使用しない。全画面captureやMSSへの自動fallbackは行わない。

遮蔽、複数モニター、DPI変更、HDR、保護コンテンツ、Windowsロック、Remote Desktopは手動実機試験を継続する。

最小化と対象終了に対する製品動作は次で固定し、backendごとの検出方法だけをPoCで決める。

- 最小化または一時的なframe停止を検出した場合、Screenを `PAUSED` にして黄色ランプを表示する。
- 最小化中は最後のframeを繰り返し保存せず、音声録音を継続する。
- 対象復元後にframe取得が戻れば、自動的に `RUNNING` へ戻して変更検知を再開する。
- 対象ウィンドウが終了した場合はScreenを `FAILED` にし、赤ランプと再選択ボタンを表示する。
- 再選択は音声を停止せずに実行できる。新しい対象の初回frameを保存し、変更検知baselineをリセットする。
- 全画面Captureへ自動fallbackしない。

### 11.2 フレーム処理頻度

- backendはより高い頻度でframeを供給してもよい。
- Screen Workerは最新frameだけを保持し、初期値2 fpsで評価する。
- 評価待ちframeをFIFOへ蓄積しない。
- 画像差分と保存はGPUを要求しない。
- 最初の安定frameは必ず保存する。

### 11.3 ScreenChangeDetector

処理パイプライン:

```text
最新frame
  -> ContentSizeで有効領域crop
  -> BGR/RGB正規化
  -> 320x180以下へaspect維持縮小
  -> grayscale
  -> 軽いGaussian blur
  -> last_saved_signatureとの差分
  -> 変更候補判定
  -> debounce
  -> 安定性判定
  -> 原寸frameをPNG保存
```

初期パラメータ:

```text
evaluation_fps: 2.0
pixel_diff_threshold: 16 / 255
changed_area_ratio_threshold: 0.03
mean_absolute_diff_threshold: 4.0 / 255
debounce_ms: 500
stable_changed_area_ratio: 0.01
minimum_save_interval_ms: 2000
```

これらは最終値ではなく設定可能なPoC初期値とする。

状態:

```text
NO_BASELINE
STABLE
CHANGE_PENDING(candidate_started_at)
```

判定規則:

1. `NO_BASELINE` では安定frameを初回保存しbaselineにする。
2. baselineとの差が閾値未満なら `STABLE` を維持する。
3. 閾値を超えたら `CHANGE_PENDING` へ遷移する。
4. debounce後のframeがcandidateと十分近く、baselineとは異なる場合だけ保存する。
5. アニメーションが続いて安定しない場合、5秒でtimeoutし、その時点の最新frameを1枚だけ保存する。
6. 保存成功後だけbaselineを更新する。
7. 保存失敗時はbaselineを更新せず、イベントを記録する。

### 11.4 カーソルと小領域変化

初期実装ではカーソル形状の直接検出は行わず、縮小、blur、変更面積閾値で影響を抑える。以下を実測する。

- 静止スライド上でマウスを5分動かす
- Teams参加者アイコンや時計だけが変化する
- Meet字幕だけが更新される
- スライドを切り替える
- スクロールする
- 動画やアニメーションを表示する

字幕更新を完全に無視するには領域maskまたは意味解析が必要になる可能性がある。Phase 1では画面ごとの固定mask UIを追加せず、保存枚数と誤検出を測定してから判断する。

### 11.5 保存メタデータ

ファイル名は連番とし、時刻はJSONL側で管理する。

```text
screenshots/000001.png
screenshots/000002.png
```

`screenshots/events.jsonl` 例:

```json
{"schema_version":1,"sequence":1,"timestamp_ms":0,"file":"000001.png","width":1920,"height":1080,"reason":"initial","metrics":{"changed_ratio":1.0,"mean_abs_diff":1.0}}
```

PNGは同一ディレクトリの `<sequence>.png.tmp` へ保存してflush、`fsync` し、そのtempをOpenCVで再decodeして寸法を検証した後に `os.replace` で正式名へrenameする。メタデータイベントは画像のrename成功後に追記し、JSONLもflush、`fsync` する。

encode、temp書込み、decode検証、rename、metadata追記の失敗は `ScreenshotSaveError` としてScreen Workerへ返す。Workerは `RUNNING / SCREEN_SAVE_FAILED` warningをepisodeごとに1回記録し、baselineを更新せず次の評価周期で保存を再試行する。次の保存成功時に復旧を通知してからbaselineを更新する。保存失敗だけではCapture sessionとAudioを停止しない。

## 12. スレッドとプロセス

### 12.1 Phase 1スレッド

```text
Main/UI Thread
Mic Capture Thread
Mic Writer Thread
System Capture Thread
System Writer Thread
Screen Capture/Change Detection Thread
```

- GUI WidgetはMain Threadでのみ生成・更新する。
- WorkerはQt Signalまたはthread-safe application event channelで状態を通知する。
- 停止はcooperative cancellationとし、`QThread.terminate()` やPython thread強制終了を使用しない。
- Worker QObjectを使用する場合はworker threadへmoveした後、queued connectionで通信する。
- 音声のblocking readを止めるため、backendのclose/cancel手段と最大read時間をPoCで確認する。

Phase 1ではCaptureを別プロセスにしない。AI JobはPhase 2以降で別プロセスにする。

### 12.2 WorkerSupervisor

責務:

- Workerの作成と開始順序
- ready/start gate
- Component状態の集約
- stop要求の配布
- stop timeoutの監視
- 例外を構造化イベントへ変換
- 全音声停止など重大状態のController通知

SupervisorはWorker内部のCapture APIを直接操作せず、start/stop契約だけを扱う。

## 13. 停止とfinalize

停止順序:

1. UIで停止要求の多重実行を無効化する。
2. Sessionを `STOPPING` に保存する。
3. Screen Workerへ停止要求を送り、新しい画像保存を止める。
4. Audio Capture Workerへ停止要求を送る。
5. Capture queueの残りをWriterが書き切る。
6. WAV segmentをcloseする。
7. Sessionを `FINALIZING` にする。
8. segmentを最終WAVへ統合する。
9. WAVを再openし、format・frame数・durationを検証する。
10. manifest、`ended_at`、`duration_ms`、Component状態を保存する。
11. Sessionを `RECORDED` にする。
12. 最終保存成功後にUIを完了画面へ遷移させる。

停止timeout初期値:

- Screen Worker: 5秒
- Audio Capture Worker: 5秒
- Audio Writerのqueue drain: 30秒
- WAV統合: ファイルサイズ依存のため固定timeoutを設けず、進捗表示する

停止開始から最終保存まで、Controllerはセッション全体の進捗を0〜100%の単調増加値としてUIへ通知する。画面停止、各音声のCapture停止、queue drain、segment統合、最終WAV検証、一時ファイル整理、manifest保存、Session保存を段階名として併記する。2つの音声trackがある場合はそれぞれに進捗範囲を割り当てる。Writerが通知する実処理量はPCM frame数を基準とし、進捗通知先の例外や表示失敗によって音声保存を失敗させない。失敗したComponentがあっても残りの確定処理を続け、最終的に100%を通知してから完了イベントへ遷移する。

録音中にユーザーがアプリウィンドウを閉じた場合は、「録音を終了してアプリを閉じますか？」という確認を表示する。キャンセル時は録音を継続する。確認後の終了、またはOS終了要求を受けた場合は同じ停止処理を通す。OS終了時には確認ダイアログを表示せずbest-effortでfinalizeする。強制終了しかできない状態ではセッションを `INTERRUPTED` として残す。

Windowsのスリープ／休止状態はPhase 1の利用シナリオとして想定せず、専用のflush、resume、gap継続機能を実装しない。発生後の録音継続は保証しない。個々のCaptureがエラーを返した場合は通常のComponent障害処理を適用する。

## 14. セッション保存構造

```text
<app-root>/data/meetings/
└─ 2026-08-08_100000_開発定例_<short-id>/
   ├─ session.json
   ├─ events.jsonl
   ├─ logs/
   │  └─ session.log
   ├─ audio/
   │  ├─ manifest.json
   │  ├─ microphone.wav
   │  ├─ system.wav
   │  └─ .work/
   ├─ screenshots/
   │  ├─ events.jsonl
   │  ├─ 000001.png
   │  └─ ...
   ├─ analysis/
   └─ output/
```

- ディレクトリ名は表示用ではなく一意性を優先する。
- 会議名からWindows禁止文字、末尾dot/space、予約名を除去する。
- 同名会議は時刻とshort IDで衝突を避ける。
- 画面や音声本文を通常アプリログへ出力しない。

### 14.1 session.json

Phase 1スキーマ例:

```json
{
  "schema_version": 1,
  "id": "cda1d61c-44f2-44f0-bd9e-f6697ca4337b",
  "title": "開発定例",
  "status": "RECORDED",
  "started_at": "2026-08-08T10:00:00+09:00",
  "ended_at": "2026-08-08T11:00:00+09:00",
  "duration_ms": 3600000,
  "platform": {
    "system": "Windows",
    "release": "11",
    "app_version": "0.1.0"
  },
  "audio": {
    "microphone": {
      "device_id": "backend-stable-id",
      "device_name": "Realtek Microphone",
      "file": "audio/microphone.wav"
    },
    "system": {
      "device_id": "backend-stable-id",
      "device_name": "Speakers",
      "file": "audio/system.wav",
      "scope": "selected_output_endpoint"
    }
  },
  "screen": {
    "target_name": "Chrome - Google Meet",
    "target_kind": "window",
    "saved_images": 42
  },
  "components": {
    "microphone": {"status": "STOPPED", "error": null},
    "system_audio": {"status": "STOPPED", "error": null},
    "screen": {"status": "STOPPED", "error": null}
  },
  "retention": {
    "keep_audio": true,
    "keep_screenshots": true
  },
  "warnings": []
}
```

- `device_id` はbackendが安定IDを提供できる場合のみ再選択に使う。
- デバイス名だけで自動再選択しない。同名デバイスが複数存在しうる。
- 将来の互換性のため全JSONに `schema_version` を持たせる。

### 14.2 events.jsonl

イベント例:

```json
{"schema_version":1,"timestamp_ms":0,"type":"session_started"}
{"schema_version":1,"timestamp_ms":3,"type":"component_started","component":"microphone"}
{"schema_version":1,"timestamp_ms":10,"type":"component_started","component":"system_audio"}
{"schema_version":1,"timestamp_ms":84520,"type":"warning","code":"SCREEN_TARGET_CLOSED","message":"Selected window is no longer available"}
```

会議の発言、OCR結果、画像本文をここへ記録しない。

## 15. 設定

設定はポータブルなアプリルートの `data/settings.json` に保存し、個別セッションフォルダには保存しない。

会議データのPhase 1既定保存先は、ユーザーへの初回選択を行わず、次に固定する。

```text
<app-root>\data\meetings
```

`<app-root>` はコピーされたアプリフォルダを指す。Application層は具体的なパスを直接組み立てず `PortableAppPathProvider` から取得する。開発実行時は `summarize-meeting/` を `<app-root>` とみなし、`summarize-meeting/data/` を使用する。テストでは一時ディレクトリを注入する。

起動時に `data/`、`data/logs/`、`data/meetings/` の作成と書込みprobeを行う。書込みできない場合は録音画面へ進まず、「アプリフォルダを書込み可能な場所へコピーしてください」と表示する。

Phase 1設定:

```text
last_microphone_device_id
last_system_device_id
screen_evaluation_fps
screen_change_thresholds
retention.keep_audio
retention.keep_screenshots
log_level
```

- 起動時に設定が壊れていた場合は既定値で起動し、壊れたファイルを上書きせずbackupする。
- 機密データやAPIキーはPhase 1設定に含めない。
- デバイスが見つからなければ黙って別デバイスへ切り替えず、UIで再選択を求める。

実装は `schema_version: 1` の型付き `AppSettings` と `FileSettingsRepository` を使用する。保存時は同一ディレクトリの `settings.json.tmp` へUTF-8 JSONを書き、flush、`fsync`、`os.replace` の順でatomicに確定する。

- ファイルがない場合と、フィールドが省略されている場合は既定値で補完する。
- JSON破損、型不正、許容範囲外の値はファイル全体の破損として扱う。
- 破損ファイルは `settings.corrupt-<timestamp>.json` へ移動し、既定値で起動してUIへ通知する。退避にも失敗した場合は原本を変更せず既定値で起動する。
- `screen_evaluation_fps` は0.1から10.0、画面差分閾値は定義済み範囲だけを許可する。
- `log_level` は `DEBUG / INFO / WARNING / ERROR` だけを許可し、Bootstrapのfile loggingへ適用する。
- 前回デバイスIDは録音開始成功後に更新する。次回起動の初回列挙だけID一致で復元し、不一致なら「なし」のまま再選択を案内する。
- 画面評価fpsと差分閾値はScreen Recorder作成時にsnapshotし、録音中に設定ファイルが変わっても現在のWorkerへ動的反映しない。
- retention設定はsessionへsnapshotする。Phase 1では自動削除処理を実装しない。

## 16. UI詳細

Phase 1のUI、エラーメッセージ、ログ閲覧用のユーザー向け文言は日本語のみとする。内部error codeと構造化フィールド名は英語で統一する。

### 16.1 RecordingPage入力

- 会議名（必須、前後空白除去後1文字以上）
- マイクデバイス
- PC音声出力デバイス
- 対象ウィンドウの選択ボタン
- 保存先表示
- 会議開始ボタン

### 16.2 録音中表示

- 経過時間
- マイク名、状態ランプ、状態テキスト、レベルメーター
- PC音声名、状態ランプ、状態テキスト、レベルメーター
- 画面対象名、状態ランプ、状態テキスト、保存枚数
- warning/error banner
- 会議終了ボタン
- 停止・finalize中だけ表示する0〜100%の進捗バーと現在の処理段階

状態ランプは色だけに依存せず、アイコンと状態テキストを併記する。

| Component状態 | ランプ | 表示テキスト例 |
|---|---|---|
| `STARTING` | 黄・点滅 | 接続中 |
| `RUNNING` | 緑・点灯 | 取得中 |
| `RECONNECTING` | 黄・点滅 | 再接続中 |
| `PAUSED` | 黄・点灯 | 一時停止 |
| `STOPPING` | 黄・点灯 | 停止処理中 |
| `STOPPED` | 灰・消灯 | 停止 |
| `FAILED` | 赤・点灯 | 取得失敗 / 取得停止 |
| `NOT_CONFIGURED` | 灰・消灯 | 未選択 |

- ランプはマイク、PC音声、画面の3つを常時同じ位置に表示する。
- Componentが `FAILED` になった時点で状態ランプを赤へ変更し、他Componentは緑のまま維持する。再接続中または画面一時停止中は黄色で示す。
- 対象ウィンドウが終了した場合は画面ランプだけを赤へ変更し、全画面Captureへfallbackせず、音声録音を継続する。
- マイクまたはPC音声の片方が開始できなくても確認ダイアログを表示しない。
- 全音声Componentが停止した場合は状態ランプに加え、録音できていないことを見落とさないよう画面内bannerを表示する。

### 16.3 状態別操作

| 状態 | 入力変更 | 開始 | 終了 | ウィンドウ再選択 |
|---|---:|---:|---:|---:|
| CREATED | 可 | 可 | 不可 | 可 |
| PREPARING | 不可 | 不可 | キャンセル | 不可 |
| RECORDING | 不可 | 不可 | 可 | 画面一時停止・障害時に可 |
| STOPPING/FINALIZING | 不可 | 不可 | 不可 | 不可 |
| RECORDED | 不可 | 不可 | 不可 | 不可 |

### 16.4 エラー表示

- ユーザーが取るべき操作を1文で示す。
- 詳細例外は折りたたみまたはログへの導線にする。
- 1つのComponent失敗時に「録音全体が止まった」と誤解させない。
- 例: 「画面の取得が停止しました。音声録音は継続しています。」

## 17. エラー分類

| コード例 | 重大度 | 動作 |
|---|---|---|
| `MIC_OPEN_FAILED` | ERROR | system録音が可能なら確認なしで継続し、マイクランプを赤にする |
| `SYSTEM_AUDIO_OPEN_FAILED` | ERROR | mic録音が可能なら確認なしで継続し、PC音声ランプを赤にする |
| `AUDIO_DEVICE_DISCONNECTED` | WARNING | 同じdevice IDへの再接続を開始し、対象ランプを黄色にする |
| `AUDIO_RECONNECT_FAILED` | ERROR | 対象trackを停止して赤ランプにし、他Componentを継続する |
| `AUDIO_QUEUE_PRESSURE` | WARNING | 継続、使用率と回数を記録 |
| `AUDIO_WRITE_FAILED` | CRITICAL | 対象track停止、他Component継続 |
| `SCREEN_OPEN_FAILED` | WARNING | 音声継続、再選択を案内 |
| `SCREEN_TARGET_PAUSED` | WARNING | 黄色ランプで一時停止し、音声継続、frame復帰を待つ |
| `SCREEN_TARGET_CLOSED` | WARNING | 赤ランプで画面停止、音声継続、再選択を案内 |
| `SCREEN_SAVE_FAILED` | WARNING | baselineを更新せず継続 |
| `LOW_DISK_SPACE` | ERROR | 開始前は阻止、録音中は強い警告 |
| `SESSION_METADATA_WRITE_FAILED` | CRITICAL | 音声Writerを可能な限り継続し緊急表示 |
| `FINALIZE_FAILED` | ERROR | `.work` を保持して再試行可能にする |

例外はWorker境界で捕捉し、`error_code`、型、メッセージ、stack trace、component、時刻をログへ記録する。UIへ生stack traceを常時表示しない。

## 18. ディスク容量

開始前に概算する。

```text
bytes_per_sec = sample_rate * channels * sample_width_bytes
estimated_audio = sum(track bytes_per_sec) * expected_duration
```

Phase 1では会議予定時間入力を必須にしないため、最低空き容量の初期基準を5 GiBとし、実測後に調整する。録音中は60秒ごとに空き容量を確認する。

実装では `StorageMonitor` とOS依存の `SystemStorageProbe` を分離する。開始前checkはセッションディレクトリ作成より先に同期実行し、5 GiB未満または容量取得失敗時は録音を開始しない。録音中のcheckは専用daemon threadで行い、停止要求は `Event` で即時解除する。

空き容量不足時:

- 新規スクリーンショット保存を先に停止する。
- 音声保存は可能な限り継続する。
- 音声Writerが失敗したら対象trackをFAILEDにして即時通知する。
- 原本を自動削除して容量を作らない。

録音中に閾値未満を検出した場合は `low_disk_space` eventへ `free_bytes` と `minimum_free_bytes` を保存し、`SESSION_STORAGE` と、画面取得が構成済みなら `SCREEN` を `FAILED / LOW_DISK_SPACE` にする。Screen Workerは進行中のcapture完了後も新しいPNGを保存せず終了し、Audio Workerには停止要求を送らない。画面の再選択による保存再開も、そのセッション中は許可しない。

容量取得自体が失敗した場合は `STORAGE_CAPACITY_CHECK_FAILED` を記録して強い警告を表示する。実際の低容量を確認できていないため、この場合はScreenとAudioを自動停止しない。

## 19. 復旧設計

起動時に `status` が `PREPARING`、`RECORDING`、`STOPPING`、`FINALIZING` のセッションを検索する。

復旧手順:

1. 元セッションを変更する前に状態とファイル一覧を記録する。
2. 完了WAVがあればopenし、formatとdurationを検査する。
3. `.work` segmentを順に検査する。
4. 開けない最終segmentは除外候補としてユーザーへ明示する。
5. 開けるsegmentから `<track>.recovered.wav` を生成する。
6. 元segmentは復旧成功確認まで削除しない。
7. `session.json` を `INTERRUPTED` に更新し、復旧結果をwarningへ追加する。
8. 画面一時ファイルはdecode可能なら正式名へ復旧し、不可能なら残してログへ記録する。

完成済みWAVが存在する場合は、音声manifestがあればformat、frame数、durationを照合し、PCMデータを末尾まで読めることを確認する。manifestが未作成または項目不足の場合は、WAV headerから得たformatとframe数を使って自己整合性を検証する。検証済みの最終WAVは復旧結果の `source` を `final_wav` として採用し、同じtrackのsegmentから重複したrecovered WAVを作らない。最終WAVが破損しているかmanifestと不一致の場合はwarningを記録し、`.work` segmentからの復旧へフォールバックする。segmentから生成した結果の `source` は `segments` とする。

画面temp復旧では `screenshots/*.png.tmp` を再decodeする。同名の正式PNGがなくdecode可能な場合だけ `.tmp` を外してatomic renameし、復旧した相対パスを `session.json.recovery.screenshots` と `session_recovered` eventへ記録する。decode不能、同名正式PNGあり、rename失敗の場合はtemp原本を変更せずwarningへ記録する。

復旧処理は自動検出するが、原本削除や破損segmentの切り捨てはユーザー確認なしに行わない。

## 20. ログとプライバシー

アプリログ:

- 日次またはサイズによるrotation
- 既定INFO
- 状態、件数、時間、エラーを記録
- PCM値、発言内容、画像内容を記録しない
- ファイルパスに会議名が含まれるため、ログexport時に注意を表示する余地を残す

セッションログ:

- `<session>/logs/session.log` へ1行1JSONのUTF-8テキストで保存する。
- Component状態遷移、開始・停止・finalize、Audioのframe数・segment数・duration・queue診断、画像件数を記録する。
- Worker境界の例外は、error code、例外型、message、stack trace、Component、monotonic経過時間を記録する。
- 会議名、デバイスID・名称、画面タイトル、アプリroot、セッションrootは全フィールドとstack traceで `[REDACTED]` に置換する。
- session logを開始時に作成できなければ `FAILED_TO_START / SESSION_LOG_OPEN_FAILED` とする。録音中の追記失敗はcaptureを停止させない。
- session logの最低レベルには `settings.json` の `log_level` を適用する。

ネットワーク:

- Phase 1実行時に会議データ送信処理を持たない。
- 依存ライブラリの更新確認やtelemetryをアプリから呼び出さない。
- 将来localhostモデルサーバーを使う場合も `127.0.0.1` 固定を既定とする。

保存時暗号化:

- Phase 1ではアプリ独自の保存データ暗号化を実装しない。
- WAV、PNG、JSON、Markdownは通常ファイルとして保存する。
- アクセス制御と保存媒体の暗号化は、配置先フォルダのWindows ACL、BitLocker等のOS・ドライブ側機能へ委ねる。
- アプリフォルダをコピーすれば `data/` もコピーされるため、利用者向け文書で取扱注意を明記する。

## 21. テスト戦略

### 21.1 Unit Test

- Session状態遷移の正常・不正パターン
- 会議名のWindowsパスsanitizationと衝突回避
- JSON atomic write失敗時に旧版が残ること
- PCM float32からPCM16へのclip・変換
- RMS、peak、dBFS、無音、NaN入力
- Audio queue pressureイベント
- WAV segment作成と統合
- ScreenChangeDetectorの初回、微小変化、大変化、debounce、timeout
- screenshot連番とmetadata整合性
- monotonic timestamp変換
- 設定破損時のfallback
- PortableAppPathProviderの開発・配布・テスト時のroot解決
- アプリルートが書込み不可の場合のpreflight失敗
- アプリルートごとの単一起動lockとstale lock回復
- Audio切断、同一device再接続成功、再接続断念
- 録音中のアプリ終了確認をキャンセルした場合の継続

### 21.2 Integration Test（Fake backend）

- 2つのFake Audioを同時開始し、別WAVへ保存する
- sample rateが異なる2trackを保存する
- 一方のAudio backend例外後も他方が継続する
- Screen例外後も両Audioが継続する
- stop中にqueueをdrainする
- finalize失敗後にsegmentが残る
- 中断セッションからrecovered WAVを作る
- Controller stateがQt Signal経由でUIへ反映される

### 21.3 Windows手動試験

機材・環境情報も結果と一緒に保存する。

音声:

- 既定マイク
- USBマイク
- 既定スピーカーLoopback
- Bluetooth headset
- Teams会議
- Chrome / Google Meet
- 会議中のミュート、無音、デバイス切断
- 出力デバイス変更

画面:

- Chrome、Teams、PowerPoint
- 通常表示、最大化、リサイズ、移動
- 他ウィンドウによる遮蔽
- 最小化、復元、対象終了
- DPI 100% / 125% / 150%
- HDR on/off（利用可能な環境のみ）

障害:

- 保存先アクセス拒否
- 読取り専用フォルダへアプリ一式をコピーした場合の起動エラー
- 空き容量警告
- screenshot保存失敗
- アプリ強制終了後の復旧

## 22. Phase 1受入基準

### 22.1 15分試験

- UI操作が継続可能で、5秒以上のフリーズがない
- 2trackが別ファイルとして再生可能
- start offset推定値が記録される
- queue overflowが0
- stop後に全WAVが正常openできる
- sessionとmanifestのframe数・durationが一致する
- スライド切替が画像として保存される
- 静止画上のカーソル移動だけで継続的に保存されない

### 22.2 1時間試験

- アプリが異常終了しない
- 両Audio Componentに未説明の停止がない
- queue overflowが0
- 各trackの `frames / sample_rate` と記録経過時間の差を計測・報告する
- 最終WAVとsegmentのframe数が一致する
- 終了処理が完了し、ファイルhandleが解放される
- 画面保存数、PNG容量、CPU・メモリ・GPU使用率を報告する
- Teams / Chromeの会議操作に体感上重大な悪影響がない

数値閾値は最初の基準機RTX 4060 / RAM 64GB環境でbaselineを取得してから固定する。

## 23. 実装順序

1. `domain` の値object、状態遷移、error code
2. FileSessionRepositoryとatomic JSON/JSONL
3. Fake Audio / Fake Screen backend
4. WAV segment Writer、manifest、統合、復旧
5. Audio Workerとlevel計算
6. RecordingController / WorkerSupervisor
7. PySide6 RecordingPage
8. SoundCard device列挙とマイク録音PoC
9. SoundCard WASAPI Loopback PoC
10. 2track同時録音と同期metadata
11. ScreenChangeDetector
12. Windows Graphics Capture HWND / frame取得PoC（実装済み）
13. Screenshot Store統合
14. 部分障害と終了処理
15. 15分試験
16. 1時間試験
17. PoC結果に基づくADR更新

## 24. 確定した製品判断

2026-08-08のユーザー回答により、以下を確定する。

### D1. Windows対応範囲

- Phase 1の正式対象はWindows 11とする。
- Windows 10対応は初期スコープ外とする。

### D2. PC再生音声の範囲

- 選択した出力デバイスから再生される全音声を録音する。
- Teams / Chromeなど特定アプリだけには限定しない。
- UIに録音範囲を明記する。

### D3. 一部Audio Capture失敗時

- マイクまたはPC音声の片方が失敗しても、取得可能な片方で録音を継続する。
- 警告確認ダイアログは表示しない。
- マイクとPC音声に個別の状態ランプと状態テキストを表示する。
- 両Audio Captureが失敗した場合は録音開始失敗または録音不能bannerを表示する。

### D4. 対象ウィンドウ終了時

- 全画面Captureへ自動fallbackしない。
- 画面Captureだけを停止し、音声録音は継続する。
- 画面取得の状態ランプと状態テキストを表示する。

### D5. 生データ保持

- Phase 1では音声とスクリーンショットを既定で保持する。
- 自動削除を実装しない。

### D6. 保存先

- 初回起動時に保存先を質問しない。
- コピー配置したアプリフォルダの `<app-root>\data\meetings` を固定保存先とする。
- 設定は `<app-root>\data\settings.json`、アプリログは `<app-root>\data\logs` に保存する。
- `%LOCALAPPDATA%` やDocumentsへ暗黙にfallbackしない。
- 将来の会社指定パス対応とテストに備え、パス取得は `PortableAppPathProvider` として抽象化する。

### D7. 録音同意UI

- 社内規程向けの録音同意確認画面やcheckboxは実装しない。
- 録音状態そのものは通常の録音画面とComponent状態ランプで明示する。

### D8. アプリ配置方式

- インストールを前提にせず、フォルダをコピーして配置するポータブルアプリとする。
- Registry登録、管理者権限、Windowsサービス登録を要求しない。
- 書込み可能な配置先を前提とし、書込み不可なら起動時に明示的なエラーを表示する。

### D9. 対象ウィンドウ最小化と再選択

- 最小化中はScreenを `PAUSED` として黄色ランプで示し、音声録音を継続する。
- 復元後は画面取得を自動再開する。
- 対象終了時は赤ランプにし、音声を止めずに別ウィンドウを再選択できるようにする。

### D10. 音声デバイス切断

- 別デバイスへ自動で切り替えない。
- 同じdevice IDへの再接続だけを一定時間試行する。
- 再接続できなければ対象trackをFAILEDとし、他の記録を継続する。

### D11. Windowsスリープ／休止状態

- Phase 1では想定利用シナリオに含めない。
- スリープ前flush、復帰後の同一セッション継続など、専用処理は実装しない。
- スリープをまたいだ録音の完全性を保証しない。

### D12. 録音中のアプリ終了

- ユーザー操作でウィンドウを閉じる場合は、録音終了確認を表示する。
- OS終了時は確認を表示せず、可能な範囲でfinalizeする。

### D13. 複数起動

- 同じアプリフォルダからの同時起動を禁止する。
- 別の場所へコピーされたアプリは独立したdata rootを持つ別インスタンスとして扱う。

### D14. 保存データの暗号化

- Phase 1ではアプリ独自暗号化を実装しない。
- Windows ACL、BitLockerなど配置環境側の保護を利用する。

### D15. UI言語

- Phase 1のユーザー向けUIは日本語のみとする。
- 内部error codeと構造化データのfield名は英語とする。

## 25. PoCで決定する事項

以下はユーザー判断ではなく、記録した実測結果に基づきADRとして決める。

1. SoundCardだけでマイクとLoopbackを安定取得できるか
2. SoundCardのchannel指定、block size、read frames
3. 48 kHz固定要求と44.1 kHz fallbackの成功率
4. 2trackのstart offsetと1時間drift
5. Audio Worker停止を確実に解除できる方法
6. Windows.Graphics.CaptureをPySide6から安定利用できるbridge
7. 最小化、遮蔽、DPI、HDRでの画面取得挙動
8. 画面変更検知の閾値と保存枚数
9. PNGとWebPのCPU時間・容量比較
10. WAV segment長とfinalize時間
11. Python 3.11を維持するか3.12へ上げられるか
12. PyInstallerとNuitkaの比較はPhase 6で実施

## 26. ADR運用

PoCで決定した事項は `docs/adr/NNNN-title.md` に残す。

ADRには以下を含める。

- Context
- Decision
- Alternatives
- Evidence（実機、OS、デバイス、測定値）
- Consequences
- Status

最初に予定するADR:

- `0001-python-version.md`
- `0002-windows-audio-backend.md`
- `0003-windows-screen-capture-backend.md`
- `0004-screen-change-thresholds.md`
- `0005-screenshot-format.md`

## 27. 参照資料

- [SoundCard公式リポジトリ](https://github.com/bastibe/SoundCard)
- [Microsoft: Screen capture](https://learn.microsoft.com/en-us/windows/apps/develop/media-authoring-processing/screen-capture)
- [Microsoft: Windows.Graphics.Capture namespace](https://learn.microsoft.com/en-us/uwp/api/windows.graphics.capture)
- [Qt for Python: Threads and QObjects](https://doc.qt.io/qtforpython-6/overviews/qtdoc-threads-qobject.html)
