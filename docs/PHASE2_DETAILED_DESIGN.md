# Summarize Meeting Phase 2 詳細設計

## 1. 目的

Phase 2では、Phase 1で保存したマイク音声とPC音声をローカルで文字起こしし、後続の話者分離・画面解析・議事録生成から再利用できる中間JSONと、ユーザーが確認できる仮の全文Markdownを生成する。

Phase 1の長時間・異常系評価は継続課題として残すが、正常系で記録されたWAVを入力できることをPhase 2着手条件とする。

## 2. 対象範囲

### 2.1 対象

- `audio/microphone.wav`の文字起こし
- `audio/system.wav`の文字起こし
- `audio/manifest.json`の開始offsetを使ったセッション共通時刻への変換
- 2トラックのtimestamp順統合
- `analysis/transcription.json`生成
- `output/transcript.md`仮生成
- GUIからの実行、再実行、キャンセル
- 録音済みセッション一覧からの解析対象選択
- 録音正常終了後の文字起こし自動実行設定
- AI処理の別プロセス実行

### 2.2 対象外

- Speaker 1、Speaker 2への話者分離
- 発話の重複解消やマイクへの回り込み除去
- 文字起こし結果のGUI編集
- モデルを配布物へ同梱する仕組み

PC音声の話者表示はPhase 3まで暫定的に`PC音声`とする。マイク側は`自分`とする。

## 3. 技術選定

| 項目 | 採用 |
|---|---|
| STT runtime | faster-whisper 1.2系 |
| 既定モデル | `large-v3-turbo` |
| 言語 | `ja`固定 |
| task | transcribe |
| VAD | faster-whisper内蔵VADを有効化 |
| beam size | 5 |
| device | CUDA認識時は`cuda`、それ以外は`cpu`。CUDA runtime不足時はCPUへ再試行 |
| compute type | CUDA時は`float16`、CPU時は`int8` |

モデルは初回実行時に取得し、`<app_root>/models/faster-whisper/`へ保持する。モデル取得以外の処理はローカルで完結し、会議音声や文字起こし結果を外部サービスへ送信しない。

CPU動作を必須とする。GPUはOSへ公式のNVIDIA CUDA 12とcuDNN 9が導入され、CTranslate2から利用可能と判定された場合だけ使用する。初期化または推論に失敗した場合はCPUへ再試行する。CUDA DLL archiveやランタイムをアプリ内へ同梱しない。

本アプリは配布しない方針のため、モデルおよび依存ライブラリの再配布ライセンス確認とモデル同梱方式は対象外とする。将来、第三者への配布へ方針変更する場合は、配布工程の開始前に再度必須課題として扱う。

## 4. 処理構成

```text
GUI process
  MainWindow
    -> TranscriptionController
         -> psutilで管理するchild Python process
              -> FasterWhisperBackend
              -> TranscriptionService
                   -> transcription.json
                   -> transcript.md
```

録音とAI処理を同時実行しない。文字起こし中は新しい録音開始操作を無効化する。AIモデルは子プロセスだけでロードし、ジョブ終了時にプロセスごと終了してVRAMとネイティブruntimeを解放する。

開発環境では`python -m summarize_meeting.processing.transcription_worker`を起動する。配布用exeにおけるworker起動方法はPhase 6のパッケージ方式に合わせてadapterを差し替える。

## 5. 入力

必須入力はセッションディレクトリ内の以下とする。

```text
audio/
├─ manifest.json
├─ microphone.wav  # 選択・録音された場合
└─ system.wav      # 選択・録音された場合
```

少なくとも一方の正常な最終WAVが必要である。`manifest.json`の`tracks`にある`file`と`estimated_start_offset_ms`を使用する。offsetが`null`の場合は0msとして扱う。

manifestからディレクトリ外のファイルを参照できないよう、`file`はbasenameだけを許可する。

## 6. 時刻統合

faster-whisperが返すsegment時刻は各WAVの先頭基準である。出力時刻は次式でセッション開始基準へ変換する。

```text
session_start_seconds = estimated_start_offset_ms / 1000 + segment.start
session_end_seconds   = estimated_start_offset_ms / 1000 + segment.end
```

変換後は小数第3位へ丸め、`start`、`end`、`source`の順に安定sortする。Phase 2ではマイクとPC音声の重複発話を両方保持する。

## 7. transcription.json

保存先は`analysis/transcription.json`とする。UTF-8、改行LF、atomic replaceで保存する。

```json
{
  "schema_version": 1,
  "status": "SUCCEEDED",
  "model": "large-v3-turbo",
  "requested_language": "ja",
  "completed_at": "2026-08-08T18:00:00.000+09:00",
  "tracks": [
    {
      "source": "microphone",
      "file": "microphone.wav",
      "start_offset_ms": 12,
      "detected_language": "ja",
      "language_probability": 0.99,
      "duration_seconds": 3600.0,
      "segment_count": 120,
      "runtime_device": "cuda"
    }
  ],
  "segments": [
    {
      "start": 12.4,
      "end": 18.75,
      "source": "microphone",
      "text": "今回の仕様について確認します",
      "avg_logprob": -0.15,
      "no_speech_prob": 0.01
    }
  ]
}
```

成功結果だけを正式JSONへ保存する。失敗やキャンセルで既存の成功結果を破壊しない。

### 7.1 jobs.json

文字起こしの各実行状態は`analysis/jobs.json`へatomic保存する。Job開始前に`RUNNING`を確定し、worker終了後に同じ`attempt_id`の状態を終端状態へ更新する。

```json
{
  "schema_version": 1,
  "jobs": {
    "transcription": {
      "job": "transcription",
      "status": "SUCCEEDED",
      "attempt_id": "uuid",
      "started_at": "2026-08-08T18:00:00.000+09:00",
      "ended_at": "2026-08-08T18:01:00.000+09:00",
      "model": "large-v3-turbo",
      "language": "ja",
      "output_path": "output/transcript.md",
      "error_message": null
    }
  }
}
```

状態は`RUNNING / SUCCEEDED / FAILED / CANCELED`を保存する。Job開始前のセッションはファイル不存在を`NOT_STARTED`として扱う。アプリ異常終了後に`RUNNING`が残っている場合は「前回中断」と表示し、ユーザーによる再実行を許可する。

既存の成功済み`transcription.json`と`transcript.md`が揃っている場合は、その利用可能な結果をJob試行状態より優先して「完了」と表示する。再実行失敗で以前の成功結果を失わないためである。

## 8. transcript.md

保存先は`output/transcript.md`とする。Phase 3で話者情報を統合した際に同じファイルを再生成する。

```markdown
# Transcript

## 00:04:12.340
**自分**
今回の仕様について確認します。

## 00:04:19.100
**PC音声**
こちらでは来週までに対応できます。
```

時刻は`HH:MM:SS.mmm`とし、1時間を超えてもhoursを繰り上げる。

## 9. UIとJob状態

録音画面下部に文字起こし行を追加する。

`data/meetings/`直下のセッションを新しい順に読み、会議日時、会議名、文字起こし状態を「解析対象」selectorへ表示する。アプリ再起動後も過去セッションを選択できる。`session.json`が壊れている場合はフォルダ名と`UNKNOWN`状態で一覧へ残し、他の正常セッションの読取りを継続する。

- 未実行: 実行ボタン
- 実行中: 進捗表示、キャンセルボタン
- 完了: 再実行ボタン
- 失敗: エラー表示、再実行ボタン
- キャンセル: 再実行ボタン

録音が正常に確定し、manifestと少なくとも1つのWAVが存在する場合だけ実行可能にする。初回モデル取得中は進捗率を算出できないため、「モデルを準備しています」と表示する。

「録音終了後に自動で文字起こし」は既定OFFとし、`data/settings.json`の`auto_transcribe_after_recording`へ保存する。ONの場合も、録音結果が`RECORDED`で、録音確定エラーがなく、manifestと少なくとも1つの最終WAVが存在する場合だけ自動実行する。アプリ終了要求・Windows終了要求がある場合は起動しない。条件を満たさないセッションは一覧へ残し、手動再実行を許可する。

## 10. エラー処理

以下はJob失敗としてGUIへ返す。

- manifest不存在・JSON破損
- 対応トラック不存在
- WAV不存在
- 不正なファイル参照またはoffset
- モデル取得失敗
- CUDA / CTranslate2初期化失敗
- STT推論失敗
- JSON / Markdown保存失敗

失敗しても録音済み原本と既存の成功済み出力は削除しない。再実行を許可する。

CUDAデバイスを認識していても必要なCUDA runtime DLLをロードできない場合は、同じJob内でCPU `int8`へ一度だけ切り替えて再試行する。CPUでも失敗した場合はJob失敗とする。

## 11. 正常系受入条件

基準端末はWindows 11、RTX 4060 8GB、RAM 64GBとする。

### 11.1 短時間試験

- マイクのみ、PC音声のみ、2トラックの各ケースでJobが完了する
- GUIが推論中も応答する
- `transcription.json`と`transcript.md`が生成される
- 日本語発話がtimestamp付きで出力される
- track開始offsetが出力segmentへ反映される
- 2トラックのsegmentが共通時刻順に並ぶ
- 再実行で出力を更新できる
- キャンセル後に再実行できる

### 11.2 1時間試験

- 1時間会議の2トラックを異常終了せず処理できる
- GPUメモリ不足にならない
- Job終了後にworkerプロセスが残らない
- GUIプロセスのGPU使用量がJob終了後に戻る
- JSONの全segmentで`0 <= start <= end`を満たす
- Markdownの発話数がJSONのsegment数と一致する

合成音声によるworker単体試験の結果は [Phase 2 STT 1時間ベンチマーク](PHASE2_STT_1H_BENCHMARK.md) に記録する。Windows音声デバイスを通した結果は [Phase 2 Windows実音声スモーク試験](PHASE2_REAL_AUDIO_SMOKE_TEST.md) に記録する。物理マイク・実PC音声を使うGUI経由の確認は別途行う。

STT精度の数値閾値は社内会議音声の評価データと正解文を準備してから決定する。

## 12. Phase 2完了状況

2026-08-08に以下を完了し、正常系を目的としたPhase 2を完了とする。

- 仮想Windows音声デバイスによる2トラック録音から自動文字起こしまでの試験
- 1時間×2トラックの処理時間、GPUメモリ、segment構造の測定
- Brio 100物理マイク、実PC出力、画面取得を使ったGUI正常系試験
- SoundCard非互換マイクのsounddevice WASAPIフォールバック確認
- JSON、Markdown、永続Job状態の整合確認

実会議音声の認識精度は、評価音声と正解文を用意した後に別途評価する。再配布ライセンス確認は、配布しない方針のため対象に含めない。
