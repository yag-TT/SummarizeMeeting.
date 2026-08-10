# Summarize Meeting Phase 3 詳細設計

## 1. 目的

Phase 3では、Phase 2で文字起こししたPC音声へ話者情報を付与し、`Speaker 1`、`Speaker 2`などの話者単位で読める全文を生成する。

マイク音声は利用者本人の入力として引き続き`自分`と表示する。話者分離の対象は`audio/system_audio.wav`だけとし、会議相手の声をローカルで分離する。

## 2. 前提

- 正式対象はWindows 11とUbuntu 22.04
- 会議音声、話者埋め込み、解析結果を外部サービスへ送信しない
- Phase 2の`analysis/transcription.json`が`SUCCEEDED`
- `audio/manifest.json`と`audio/system_audio.wav`が存在する
- AI JobはGUIプロセスと分離する
- アプリは第三者へ配布しないため、再配布ライセンス対応は対象外
- 正常系を優先し、話者分離精度の数値基準は評価データ準備後に定める

## 3. 対象範囲

### 3.1 対象

- `system_audio.wav`のオフライン話者分離
- 話者数の自動推定と既知話者数指定
- 話者turnとSTT segmentのtimestamp統合
- マイク発話を`自分`として統合
- `analysis/diarization.json`生成
- `analysis/diarized_transcription.json`生成
- `analysis/speaker_names.json`生成
- 話者付き`output/transcript.md`再生成
- GUIからの実行、再実行、キャンセル
- GUIでの話者名変更
- Job状態の永続化とアプリ再起動後の表示

### 3.2 対象外

- 人物の声紋登録と実名自動特定
- 複数会議をまたぐ同一人物判定
- マイク音声内の複数人分離
- 音声分離による話者別WAV生成
- 発話テキストのGUI編集
- 重なり発話のテキストを話者ごとへ再分割
- DERの合格閾値
- リアルタイム話者分離

## 4. 技術選定

| 項目 | 採用案 |
|---|---|
| Runtime | Windows等: sherpa-onnx 1.13.2、Linux x86_64: 1.13.4+cuda12.cudnn9 |
| 方式 | offline speaker diarization |
| segmentation | GPU: `model.onnx`、CPU: `model.int8.onnx` |
| embedding第一候補 | `nemo_en_titanet_small.onnx` |
| embedding比較候補 | `3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx` |
| provider | Ubuntu/WSL2 x86_64はCUDA優先、Windows等はCPU |
| 入力 | 16 kHz、mono、float32 |
| 話者数 | 既定は自動、必要時1〜10人を指定 |
| 自動クラスタ閾値 | PoC初期値0.75 |
| min duration on/off | 0.3秒 / 0.5秒 |

sherpa-onnxは、segmentation model、speaker embedding extractor、clusteringを組み合わせたオフライン話者分離を提供する。Python APIでは既知の話者数を`num_clusters`へ指定でき、不明な場合はcluster thresholdで推定できる。

pyannote.audio `community-1`は精度面の比較候補だが、初回モデル取得にHugging Faceの利用条件承諾とアクセストークンが必要で、PyTorch、FFmpeg系の依存も大きい。Phase 3 PoCでは、ONNXモデルを固定配置できるsherpa-onnxを先行採用する。

公式例ではNeMo TitaNetを使った構成が3D-Speaker構成より短い処理時間を示しているため第一候補とする。ただし例は日本語会議音声ではないため、同一の評価音声で両embedding modelを比較して最終決定する。

Ubuntu/WSL2 x86_64ではsegmentationとembeddingをCUDAで実行する。CUDA共有ライブラリ不足やCUDA初期化・推論エラーではCPUへ1回だけフォールバックし、実providerと理由を結果へ保存する。音声decodeとclusteringなど一部工程はCPUで実行する。Windows等は従来どおりCPUを使用する。

## 5. モデル配置

```text
models/
└─ sherpa-onnx/
   └─ diarization/
      ├─ segmentation/
      │  ├─ model.onnx
      │  ├─ model.int8.onnx
      │  ├─ LICENSE
      │  └─ README.md
      └─ embedding/
         └─ nemo_en_titanet_small.onnx
```

開発用setup scriptは固定URL、固定ファイル名、SHA-256期待値を持ち、archiveまたはモデルを検証してからatomic確定する。モデルが揃っていない場合、GUIからのJob開始時に不足ファイル名とsetup手順を表示する。会議開始時にはモデル確認を行わない。

## 6. 処理構成

```text
GUI process
  MainWindow
    -> DiarizationController
         -> child Python process
              -> AudioPreprocessor
              -> SherpaOnnxDiarizationBackend
              -> DiarizationService
                   -> diarization.json
                   -> diarized_transcription.json
                   -> speaker_names.json
                   -> transcript.md
```

録音、STT、話者分離は同時実行しない。話者分離中は新しい録音とSTT実行を無効化する。モデルと音声配列はworkerだけで保持し、Job終了時にプロセスごと解放する。

## 7. 入力検証

Job開始前に以下を確認する。

1. セッションディレクトリが`data/meetings/`配下にある
2. `session.json`が`RECORDED`
3. `analysis/transcription.json`が`SUCCEEDED`
4. `audio/manifest.json`がschema version 3で、`system_audio`trackがある
5. trackのfileが`system_audio.wav`形式の安全な単純ファイル名である
6. WAVが存在し、PCMとして最後まで読み取れる
7. WAV時間が0秒より大きい
8. STT出力に少なくとも1つのsystem segmentがある

マイクだけの録音には話者分離を実行しない。PC音声が無音またはsystem segmentが0件の場合は、原本とPhase 2出力を変更せずJobを失敗とする。

## 8. 音声前処理

sherpa-onnxのモデル入力へ合わせ、`system_audio.wav`を16 kHz、mono、float32へ変換する。

1. PyAVでWAVをchunk単位にdecodeする
2. `AudioResampler`でmono化と16 kHz resampleを行う
3. 変換chunkを連結してcontiguousなfloat32配列にする
4. 無音と空配列を拒否する
5. worker内でsherpa-onnxへ渡し、Job終了時にプロセスごと解放する

派生WAVは作らず、`analysis/.work/`へ中間音声を残さない。sherpa-onnx APIが全サンプル配列を要求するため、1時間入力は約230MBを基本目安とし、連結時の一時配列を含むpeak memoryは約460MBを許容する。

Windowsでは`System32`の古い`onnxruntime.dll`がDLL検索で優先される場合がある。workerは仮想環境内のONNX Runtime 1.24.4 DLLを絶対パスで先読みしてからsherpa-onnxをimportし、Phase 2依存およびOS側DLLと隔離する。

## 9. 話者数とクラスタ設定

GUIで以下を選択できる。

- `自動`（既定）
- `1人`〜`10人`

`自動`は`num_clusters=-1`とcluster thresholdを使用する。PoC初期値は0.75とし、設定ファイルへは保存せず実装定数とする。評価後に変更できるようbackend configへ閉じ込める。

既知話者数を指定した場合はcluster thresholdより`num_clusters`を優先する。検出話者数が指定数と一致しなくてもworkerを異常終了させず、結果とwarningへ記録する。

## 10. timestamp変換

話者分離結果は`system_audio.wav`先頭を0秒とする。`audio/manifest.json`の`estimated_start_offset_ms`を加算し、セッション共通時刻へ変換する。

```text
session_start = diarization_start + system_start_offset_ms / 1000
session_end   = diarization_end   + system_start_offset_ms / 1000
```

小数第3位へ丸め、`start`、`end`、`speaker_id`の順で安定sortする。変換後も`0 <= start <= end`を検証する。

クラスタ番号は推論ごとに変わり得るため、最初のturn開始時刻が早い順に正規化し、`speaker_01`、`speaker_02`のIDを付ける。同時刻の場合は元クラスタ番号をtie breakerにする。

## 11. STTとの統合

`analysis/transcription.json`は変更しない。新しい`analysis/diarized_transcription.json`を生成する。

### 11.1 マイクsegment

- `speaker_id`: `self`
- `speaker_name`: `自分`
- `assignment`: `microphone`
- 話者分離turnとの照合は行わない

### 11.2 PC音声segment

各STT segmentと話者turnの重複時間をspeakerごとに合計し、最長のspeakerを割り当てる。

```text
overlap = max(0, min(stt.end, turn.end) - max(stt.start, turn.start))
```

- 最大overlapが正の場合: `dominant_overlap`
- overlapがない場合、前後0.75秒以内の最も近いturn: `nearest_turn`
- 候補がない場合: `unknown`

各segmentへ候補speakerとoverlap秒数を保存する。次のどちらかなら`ambiguous: true`とする。

- 第一候補のoverlapがsegment時間の50%未満
- 第二候補のoverlapがsegment時間の25%以上

Phase 3ではSTT segment内の文字列を分割しない。1つのSTT segment内で話者が交代した場合はdominant speakerを表示し、曖昧フラグを残す。word timestampを使った細分化は精度改善工程で検討する。

## 12. 出力スキーマ

### 12.1 `analysis/diarization.json`

```json
{
  "schema_version": 1,
  "status": "SUCCEEDED",
  "completed_at": "2026-08-08T20:00:00.000+09:00",
  "runtime": "sherpa-onnx",
  "segmentation_model": "sherpa-onnx-pyannote-segmentation-3-0/model.int8.onnx",
  "embedding_model": "nemo_en_titanet_small.onnx",
  "provider": "cpu",
  "source": {
    "file": "system_audio.wav",
    "start_offset_ms": 659,
    "duration_seconds": 313.3
  },
  "config": {
    "speaker_count": "auto",
    "cluster_threshold": 0.75,
    "min_duration_on": 0.3,
    "min_duration_off": 0.5
  },
  "speakers": [
    {"id": "speaker_01", "default_name": "Speaker 1", "turn_count": 8}
  ],
  "turns": [
    {
      "start": 1.120,
      "end": 4.850,
      "audio_start": 0.461,
      "audio_end": 4.191,
      "speaker_id": "speaker_01"
    }
  ],
  "warnings": []
}
```

### 12.2 `analysis/speaker_names.json`

```json
{
  "schema_version": 1,
  "updated_at": "2026-08-08T20:05:00.000+09:00",
  "names": {
    "speaker_01": "田中",
    "speaker_02": "佐藤"
  }
}
```

このファイルだけをGUI編集対象とする。未設定または空白だけの名前は`Speaker N`へ戻す。名前はtrim後80文字までとし、改行と制御文字を拒否する。

### 12.3 `analysis/diarized_transcription.json`

```json
{
  "schema_version": 1,
  "status": "SUCCEEDED",
  "source_transcription_schema_version": 1,
  "segments": [
    {
      "start": 17.539,
      "end": 21.879,
      "source": "system_audio",
      "speaker_id": "speaker_01",
      "speaker_name": "田中",
      "assignment": "dominant_overlap",
      "ambiguous": false,
      "overlap_candidates": [
        {"speaker_id": "speaker_01", "seconds": 4.340}
      ],
      "text": "来週の金曜日までに、テスト結果を共有します。"
    }
  ]
}
```

## 13. `transcript.md`再生成

話者分離成功後は、マイクとPC音声を共通時刻順に並べ、同じ`output/transcript.md`をatomic更新する。

```markdown
# Transcript

## 00:00:02.920
**自分**
確認を始めます。

## 00:00:17.539
**田中**
来週の金曜日までに、テスト結果を共有します。
```

話者名変更時は推論を再実行せず、`speaker_names.json`、`diarized_transcription.json`、`transcript.md`を再生成する。途中失敗に備え、それぞれtempへ書いた後に置換する。既存の成功済み出力は、新しい一式の生成が完了するまで保持する。

## 14. GUI設計

解析領域へ「話者分離」groupを追加する。

| UI | 動作 |
|---|---|
| 話者数 | `自動`、`1人`〜`10人` |
| 実行 / 再実行 | worker開始 |
| キャンセル | 実行中workerを停止 |
| progress bar | 前処理、モデル準備、推論、統合、保存を表示 |
| 状態 | 未実行、実行中、完了、失敗、キャンセル、前回中断 |
| 話者一覧 | `Speaker N`と編集可能な表示名 |
| 名前を保存 | mappingと話者付き出力をatomic更新 |

実行ボタンはSTT成功済みかつsystem trackがある場合だけ有効にする。話者分離中は録音開始、STT、解析対象切替、話者名編集を無効化する。GUI event loopではモデル取得、音声変換、推論を行わない。

再実行時は、クラスタIDの人物対応が変化する可能性があるため、既存の話者名mappingを既定名へ戻す。確認ダイアログは出さず、実行前から画面内へ「再実行すると話者名はリセットされます」と表示する。

## 15. Job状態とworker protocol

`analysis/jobs.json`へ`diarization`Jobを保存する。Phase 2の`transcription`Jobは保持する。

```json
{
  "jobs": {
    "transcription": {"status": "SUCCEEDED"},
    "diarization": {
      "job": "diarization",
      "status": "RUNNING",
      "attempt_id": "uuid",
      "started_at": "...",
      "model": "sherpa-onnx-pyannote-segmentation-3-0+nemo_en_titanet_small",
      "speaker_count": "auto"
    }
  }
}
```

workerはstdoutへUTF-8 JSON Linesだけを出力する。

```json
{"type":"progress","percent":35,"message":"PC音声から話者を分離しています"}
{"type":"result","diarization_path":".../analysis/diarization.json","transcript_path":".../output/transcript.md"}
```

診断は標準エラーまたはアプリログへ出し、音声、発話文、話者名をログへ含めない。worker環境へ`PYTHONUTF8=1`を設定し、Windowsではconsole windowを表示しない。

## 16. キャンセル、再実行、失敗

- キャンセルはworkerプロセスをterminateする
- キャンセル後も録音原本、STT結果、直前の成功済み話者分離結果を保持する
- 次回起動時に`RUNNING`が残っていれば`INTERRUPTED`として再実行可能にする
- モデル不足、入力不正、前処理失敗、推論失敗、保存失敗を区別して表示する
- worker異常終了時は末尾20行以内のsanitized診断だけをGUIへ表示する
- tempと`.work`は次回起動時に安全な派生ファイルだけcleanupする
- 再実行成功時だけ前回結果を置換する

Phase 3失敗によってPhase 2の`transcript.md`を空にしない。話者分離出力がない場合、Phase 2形式の`自分` / `PC音声`表示を引き続き利用できる。

## 17. テスト

### 17.1 単体テスト

- 48 kHz stereoから16 kHz monoへの変換
- system start offsetの加算
- cluster番号の初出順正規化
- STT segmentとturnのoverlap計算
- dominant、nearest、unknownの割当
- ambiguous判定
- マイクsegmentの`self`固定
- 話者名のvalidationとdefault復帰
- JSONとMarkdownのatomic生成
- 安全でない入力パスの拒否
- 既存成功結果を失敗時に保持

### 17.2 worker統合テスト

- fake backendでprogressとresultをJSON Lines出力
- キャンセル後にJobが`CANCELED`
- worker異常終了でJobが`FAILED`
- アプリ再起動時に`RUNNING`を`INTERRUPTED`表示
- transcription Jobとdiarization Jobの併存

### 17.3 実モデルPoC

1. 公式の複数話者テストWAVでPython APIが完走する
2. 既知話者数指定で指定数のspeakerを生成する
3. 自動話者数でthreshold別の検出数を比較する
4. NeMo TitaNetと3D-Speakerの処理時間、speaker数、turn境界を比較する
5. 日本語の複数人会話で話者交代を目視確認する
6. 1時間system音声で処理時間とRAMを測定する

## 18. 正常系受入条件

- GUIから話者分離Jobを開始、キャンセル、再実行できる
- 推論中もGUIが応答する
- system_audio.wavから1人以上のspeakerとturnが生成される
- 全turnで`0 <= start <= end`
- manifestのstart offsetがturnへ反映される
- 全マイクsegmentが`自分`
- 全system segmentにspeakerまたは`unknown`が付く
- JSONとMarkdownのsegment数が一致する
- GUIで話者名を変更すると推論なしで出力へ反映される
- worker終了後にプロセスとRAMが解放される
- 失敗、キャンセル、再実行で録音原本とSTT結果を失わない
- アプリ再起動後もJob状態と話者名を復元できる

話者分離精度のDER閾値は、正解speaker turnを持つ日本語評価データを準備した後に追加する。

## 19. 実装順序

1. sherpa-onnx依存とモデルsetup script
2. 音声前処理Portと実装
3. 話者分離domain model、schema、overlap統合service
4. fake backendによる単体テスト
5. sherpa-onnx worker PoC
6. `DiarizationController`とJob永続化
7. GUIの実行、進捗、キャンセル
8. 話者名編集と出力再生成
9. 複数話者の短時間正常系試験
10. 1時間性能試験

## 20. PoCで確定する調整値

以下は実装を止める不明点ではなく、実モデルPoCの測定結果で確定する。

- NeMo TitaNetと3D-Speakerのどちらを既定embedding modelにするか
- 自動話者数のcluster thresholdを0.75から変更するか
- CPU thread数
- 1時間入力の処理時間と最大RAM
- `nearest_turn`許容時間0.75秒
- ambiguous判定の50% / 25%閾値

## 21. 参考資料

- [sherpa-onnx Speaker Diarization](https://k2-fsa.github.io/sherpa/onnx/speaker-diarization/)
- [sherpa-onnx Python API example](https://k2-fsa.github.io/sherpa/onnx/speaker-diarization/python.html)
- [sherpa-onnx pre-trained diarization models](https://k2-fsa.github.io/sherpa/onnx/speaker-diarization/models.html)
- [sherpa-onnx GitHub](https://github.com/k2-fsa/sherpa-onnx)
- [pyannote.audio GitHub](https://github.com/pyannote/pyannote-audio)
