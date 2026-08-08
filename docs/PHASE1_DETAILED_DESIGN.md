# 会議議事録作成ツール Phase 1 詳細設計

更新日: 2026-08-08

状態: Draft（ユーザー確認事項と実機PoC項目を含む）

対象: Phase 1「記録基盤」のWindows先行実装

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

- Windows向けPySide6デスクトップUI
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
STOPPING
STOPPED
FAILED
```

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

1. 会議名、保存先、選択デバイス、画面対象を検証する。
2. 空き容量を検査する。
3. セッションディレクトリと初期 `session.json` を作る。
4. 各Capture streamを開き、最初のread直前まで準備する。
5. Writerを先に起動し、書込み可能状態を確認する。
6. 全Workerがreadyになったら共通start gateを解放する。
7. `monotonic_origin_ns` と `started_at` を確定する。
8. セッションを `RECORDING` に遷移させる。
9. UIタイマーを開始する。

start gateは完全なサンプル同期を保証しない。各Audio Workerは最初のread完了時刻とchunk長から、そのトラックの推定開始offsetを計算し、manifestへ保存する。

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
      "frames_written": 172800000,
      "overflow_count": 0,
      "segments": []
    },
    "system": {
      "sample_rate": 48000,
      "channels": 2,
      "sample_width_bytes": 2,
      "estimated_start_offset_ms": 34,
      "frames_written": 172800000,
      "overflow_count": 0,
      "segments": []
    }
  }
}
```

音声タイムライン上の時刻は次で算出する。

```text
timestamp_sec = estimated_start_offset_sec + frame_index / sample_rate
```

終了時に `frames_written / sample_rate` とmonotonic経過時間の差を記録し、デバイスクロックdriftの診断に使用する。Phase 1では無断でstretchやsilence挿入を行わない。

## 11. 画面取得詳細

### 11.1 Windows第一候補

`Windows.Graphics.Capture` を第一PoC候補とする。理由:

- OSの安全なpickerでユーザーがウィンドウを選択できる
- アプリウィンドウ単位のCaptureが可能
- フレームにシステム相対時刻がある
- 対象がリサイズされた場合の再構築手段がある

Microsoft公式仕様ではCapture中に対象へ通知枠が表示される。UI仕様と手動試験に含める。

PythonからのWinRT / Direct3D surface受け渡し、PySide6 HWNDとのpicker連携、最小化時の挙動は実機PoC項目とする。失敗時は以下を比較する。

1. Windows.Graphics.Captureの別Python bridge
2. DXcamで対象モニターを取得してウィンドウ矩形をcrop
3. Windows固有の小さなnative helperを別プロセスとして実装

DXcam + cropは、他ウィンドウによる遮蔽、対象移動、DPI、複数モニター境界の影響を受けるため第一候補にはしない。

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

PNGは同一ディレクトリ内の一時名へ保存し、decode検証後にrenameする。メタデータイベントは画像のrename成功後に追記する。

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

アプリ終了ボタンやOS終了要求を受けた場合も、録音中なら同じ停止処理を通す。強制終了しかできない状態ではセッションを `INTERRUPTED` として残す。

## 14. セッション保存構造

```text
meetings/
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

設定はOSのユーザー別Application Data配下へ保存し、リポジトリやセッションフォルダへ保存しない。

Phase 1設定:

```text
meetings_root
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

## 16. UI詳細

### 16.1 RecordingPage入力

- 会議名（必須、前後空白除去後1文字以上）
- マイクデバイス
- PC音声出力デバイス
- 対象ウィンドウの選択ボタン
- 保存先表示
- 会議開始ボタン

### 16.2 録音中表示

- 経過時間
- マイク名、状態、レベルメーター
- PC音声名、状態、レベルメーター
- 画面対象名、状態、保存枚数
- warning/error banner
- 会議終了ボタン

### 16.3 状態別操作

| 状態 | 入力変更 | 開始 | 終了 | ウィンドウ再選択 |
|---|---:|---:|---:|---:|
| CREATED | 可 | 可 | 不可 | 可 |
| PREPARING | 不可 | 不可 | キャンセル | 不可 |
| RECORDING | 不可 | 不可 | 可 | 画面障害時のみ候補 |
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
| `MIC_OPEN_FAILED` | ERROR | system録音が可能なら確認後に継続可能 |
| `SYSTEM_AUDIO_OPEN_FAILED` | ERROR | mic録音が可能なら確認後に継続可能 |
| `AUDIO_QUEUE_PRESSURE` | WARNING | 継続、使用率と回数を記録 |
| `AUDIO_WRITE_FAILED` | CRITICAL | 対象track停止、他Component継続 |
| `SCREEN_OPEN_FAILED` | WARNING | 音声継続、再選択を案内 |
| `SCREEN_TARGET_CLOSED` | WARNING | 画面停止、音声継続 |
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

空き容量不足時:

- 新規スクリーンショット保存を先に停止する。
- 音声保存は可能な限り継続する。
- 音声Writerが失敗したら対象trackをFAILEDにして即時通知する。
- 原本を自動削除して容量を作らない。

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

復旧処理は自動検出するが、原本削除や破損segmentの切り捨てはユーザー確認なしに行わない。

## 20. ログとプライバシー

アプリログ:

- 日次またはサイズによるrotation
- 既定INFO
- デバイス名、状態、件数、時間、エラーを記録
- PCM値、発言内容、画像内容を記録しない
- ファイルパスに会議名が含まれるため、ログexport時に注意を表示する余地を残す

ネットワーク:

- Phase 1実行時に会議データ送信処理を持たない。
- 依存ライブラリの更新確認やtelemetryをアプリから呼び出さない。
- 将来localhostモデルサーバーを使う場合も `127.0.0.1` 固定を既定とする。

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
- 空き容量警告
- screenshot保存失敗
- アプリ強制終了後の復旧
- Windowsスリープ・復帰

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
12. Windows Graphics Capture picker / frame取得PoC
13. Screenshot Store統合
14. 部分障害と終了処理
15. 15分試験
16. 1時間試験
17. PoC結果に基づくADR更新

## 24. ユーザー確認事項

以下は技術PoCでは決められず、製品動作に影響するため確認が必要である。本書では暫定案も示す。

### Q1. Windowsの最低対応バージョン

- 不明点: 「Windows」の具体的な最低バージョンが未定義。
- 影響: Windows.Graphics.Capture、配布、DPI/HDR、テスト範囲。
- 暫定案: Phase 1はWindows 11を正式対象とし、Windows 10は後から要否を判断する。

### Q2. PC再生音声の範囲

- 不明点: 選択スピーカーの全音声でよいか、Teams / Chromeだけに限定する必要があるか。
- 影響: 標準WASAPI Loopbackは選択エンドポイント全体を記録する。
- 暫定案: 全音声を記録し、UIに明記する。特定アプリ限定は初期スコープ外。

### Q3. 一部Captureが開始できない場合

- 不明点: マイクまたはPC音声の片方が開始できない状態で会議開始を許可するか。
- 影響: データ完全性と、緊急時に残る側だけでも記録する利便性のtrade-off。
- 暫定案: 警告dialogで欠けるtrackを明示し、ユーザーが確認した場合だけdegraded recordingを許可する。画面だけの失敗では音声開始を阻止しない。

### Q4. 対象ウィンドウが最小化・終了された場合

- 不明点: 自動で画面全体へfallbackするか、画面Captureを停止するか。
- 影響: 意図しない画面や機密情報の保存リスク。
- 暫定案: 自動画面全体fallbackは禁止。画面Captureだけを停止して警告し、音声を継続する。再選択機能はPoC後に追加判断する。

### Q5. 生データ保持の既定値

- 不明点: 音声・画像を既定で保持するか、議事録生成後に削除するか。
- 影響: プライバシー、障害復旧、再解析。
- 暫定案: Phase 1では両方保持し、自動削除を実装しない。

### Q6. 本番の既定保存先

- 不明点: Documents配下、会社指定フォルダ、ユーザー選択のどれを既定にするか。
- 影響: 容量、OneDrive同期、会社の情報管理規則。
- 暫定案: 初回起動時にユーザーへ保存先選択を求め、その後は記憶する。開発時はリポジトリ外のユーザー指定一時フォルダを使う。

### Q7. 録音同意UI

- 不明点: 会議参加者の同意確認や社内規程に合わせた確認checkboxが必要か。
- 影響: 製品UIと運用手順。法務判断は本ツールの技術設計だけでは決められない。
- 暫定案: 録音中であることを常時明示し、開始時確認文を設定可能にする余地を残す。

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
