# Summarize Meeting

Teams、Google Meetなどのオンライン会議について、マイク音声、PC再生音声、選択したウィンドウの重要な画面変更をローカル保存し、会議終了後に議事録を生成するデスクトップアプリケーションです。

Phase 5「統合議事録」の正常系PoCまで実装しています。Phase 1の記録、Phase 2のfaster-whisper文字起こし、Phase 3の話者分離、Phase 4のローカルOCR画面解析をtimestampで統合し、既存LM Studioモデルを使って根拠付き議事録を生成できます。

## 開発環境

- Python 3.11
- uv
- Windows 11 / Ubuntu 22.04
- UIは日本語のみ

```console
uv sync
uv run summarize-meeting
```

### Ubuntu 22.04

UbuntuではPython 3.11を`uv`に管理させることができます。Waylandの画面共有と音声入出力に必要なOSパッケージを先に導入します。

```bash
sudo apt update
sudo apt install pipewire xdg-desktop-portal xdg-desktop-portal-gnome ffmpeg \
  libportaudio2 libpulse0 libxcb-cursor0
uv python install 3.11
uv sync --frozen
uv run summarize-meeting
```

画面取得は両OSともQt Multimediaを使用します。WindowsとX11では画面またはウィンドウを選択できます。Ubuntu 22.04 Waylandでは「開始時にOSダイアログで共有画面を選択」を選び、会議開始後にXDG Desktop Portalの共有対象選択へ応答します。許可は録音ごとに必要です。Portalを拒否した場合や共有対象が終了した場合は画面取得だけが停止し、音声録音は継続します。ヘッドレス、SSHのみ、ロック画面での取得は対象外です。

初回の依存取得後、アプリ画面で会議名、マイク、PC音声、取得画面を選択して「会議開始」を押します。PC音声は選択した出力デバイスから再生される全音声が対象です。

マイク音声は原音を`audio/microphone.wav`へ保存し、文字起こしもこの原音を使用します。派生WAVは生成しません。

録音終了後、画面下部の「文字起こし」から実行します。既定モデルは`large-v3-turbo`、言語は日本語です。初回実行時はモデルを`models/faster-whisper/`へ取得するため時間とインターネット接続が必要です。会議音声と文字起こし結果は外部サービスへ送信しません。

「録音終了後に自動で文字起こし」をONにすると、正常に確定した録音だけを会議終了後に自動処理します。既定はOFFです。録音確定エラー、音声不足、アプリ終了時には自動実行せず、録音済みセッションから手動で再実行できます。

「解析対象」には`data/meetings/`内の録音済みセッションが新しい順に表示されます。アプリを再起動した後でも過去の会議を選択し、文字起こしの実行・再実行ができます。壊れた`session.json`が混在しても、そのフォルダ名を使って他のセッションとともに一覧表示します。

文字起こしJobの開始・成功・失敗・キャンセルは`analysis/jobs.json`へatomic保存します。実行中にアプリが終了して`RUNNING`が残った場合、次回起動時は「前回中断」として表示し、再実行できます。

文字起こし完了後は「話者分離」から話者数を`自動`または`1人`〜`10人`で選び、PC音声の話者を分離できます。マイク発話は`自分`、PC音声は`Speaker 1`などで表示されます。完了後に話者名を編集して保存すると、話者付きJSONと`transcript.md`を推論なしで再生成します。

録音済みセッションにスクリーンショットがある場合は「画面解析」を実行できます。両OSともPaddleOCR 3.7とPP-OCRv6 mediumのONNXモデルを使用し、日本語・英語を含むOCR結果、画面種別、タイトル候補、重要行を外部サービスへ送信せず`analysis/screens.json`へ保存します。

文字起こし完了後は「議事録生成」を実行できます。話者付き文字起こしを優先し、画面解析結果を任意で統合して`analysis/timeline.json`、`analysis/minutes.json`、`output/minutes.md`を生成します。LM StudioのLocal ServerとPCへ導入済みのモデルを使用し、アプリからLLMをダウンロードしません。

LM Studio側でLocal Serverを起動し、モデルを1つロードします。複数モデルをロードする場合はモデルIDを環境変数で指定します。

```console
lms server start --port 1234
lms load <既存model-key> --identifier summarize-meeting --context-length 16384
uv run summarize-meeting
```

接続先の既定値は`http://127.0.0.1:1234/v1`です。変更する場合もlocalhostだけを許可します。複数モデルをロードする場合は、起動環境へ`SUMMARIZE_MEETING_LLM_MODEL=summarize-meeting`を設定します。

初回は固定URLとSHA-256検証付きスクリプトで、CPU話者分離モデルを`models/sherpa-onnx/diarization/`へ配置します。

```console
uv run python scripts/setup_models.py diarization
```

OCRモデルは固定revisionから取得し、ONNXファイルのSHA-256を検証して`models/paddleocr/`へ配置します。

```console
uv run python scripts/setup_models.py ocr
```

全モデルをまとめて準備する場合は`uv run python scripts/setup_models.py all`を実行します。モデル不足のままオフラインで解析した場合も、同じコマンドを案内します。

CPUだけで全機能を実行できます。GPUはOSへ公式のNVIDIA CUDA 12とcuDNN 9が導入され、CTranslate2から利用可能と判定された場合だけ文字起こしに使用します。GPU初期化に失敗した場合はCPUへ自動フォールバックします。CUDA DLLや非公式runtime archiveはアプリへ同梱しません。

環境の非破壊診断は次のコマンドで実行できます。

```console
uv run python scripts/doctor.py
```

OS、デスクトップセッション、Portal、PipeWire/PulseAudio、音声デバイス、OCRモデル、CUDA、保存先の書込権限を表示します。

記録データは次へ保存されます。

```text
data/meetings/<session>/
├─ session.json
├─ events.jsonl
├─ audio/
│  ├─ microphone.wav
│  ├─ system.wav
│  ├─ manifest.json
│  └─ .work/
├─ screenshots/
│  ├─ events.jsonl
│  └─ 000001.png ...
├─ analysis/
│  ├─ jobs.json
│  ├─ transcription.json
│  ├─ diarization.json
│  ├─ diarized_transcription.json
│  ├─ speaker_names.json
│  ├─ screens.json
│  ├─ timeline.json
│  └─ minutes.json
└─ output/
   ├─ transcript.md
   └─ minutes.md
```

文字起こしworkerだけを実行する場合:

```console
uv run python -m summarize_meeting.processing.transcription_worker --session "<data/meetings/セッション>" --models-dir "<アプリルート/models>"
```

話者分離workerだけを実行する場合:

```console
uv run python -m summarize_meeting.processing.diarization_worker --session "<data/meetings/セッション>" --models-dir "<アプリルート/models>" --speaker-count 2
```

話者数を自動推定する場合は`--speaker-count`を省略します。

画面解析workerだけを実行する場合:

```console
uv run python -m summarize_meeting.processing.screen_analysis_worker --session "<data/meetings/セッション>" --models-dir "<アプリルート/models>" --language ja
```

議事録生成workerだけを実行する場合:

```console
uv run python -m summarize_meeting.processing.minutes_worker --session "<data/meetings/セッション>" --base-url http://127.0.0.1:1234/v1 --model summarize-meeting
```

アプリ起動時に正常終了していないセッションを検出すると、復旧確認を表示します。復旧時は元の `.work` segmentを変更せず、`audio/microphone.recovered.wav` や `audio/system.recovered.wav` を新しく生成します。

録音中に音声デバイスが切断された場合は、別デバイスへ切り替えず、同じdevice IDへ最大5回、約10秒間再接続を試します。切断区間は `audio/manifest.json` の `gaps` に記録されます。

PC音声loopbackにはSoundCardを使用します。WindowsではWASAPI loopback、UbuntuではPulseAudio互換のmonitor sourceを列挙します。物理マイクの音声形式をSoundCardで開始できない場合は、同名のsounddevice入力へ自動的にフォールバックします。

会議開始前に保存先の空き容量を確認し、5 GiB未満では録音を開始しません。録音中は60秒ごとに確認し、5 GiB未満になった場合は新しい画面保存を停止して音声録音を優先します。データを自動削除して容量を確保することはありません。

前回使用したマイクとPC音声のdevice ID、画面変更検知設定、保持方針、自動文字起こし、ログレベルは `data/settings.json` に保存します。壊れた設定は `data/settings.corrupt-<timestamp>.json` へ退避し、既定値で起動します。保存済みdevice IDが見つからない場合、別デバイスへ自動切替はしません。

`audio/manifest.json` には2track共通のmonotonic origin、各trackの推定開始offset、WAV時間、再接続gapを除く稼働時間、duration drift、queue最大使用率、pressure回数、overflow回数を保存します。診断値に基づく音声の自動伸縮や無音挿入は行いません。

スクリーンショットはtempへ書き込んで再decode検証した後にatomic確定します。一時的な画像保存失敗では画面Captureと音声を止めず、baselineを維持して再試行します。異常終了後に残った正常なPNG tempは起動時復旧の対象です。

## 開発時の検証

```console
uv run ruff check .
uv run pytest -q
```

短い録音済みセッションから、長時間STT試験用の反復音声セッションを生成する場合:

```console
uv run python -m summarize_meeting.devtools.benchmark_session --source-session data/meetings/stt-smoke-ja --output-session data/meetings/stt-benchmark-1h --duration-seconds 3600
```

出力先がすでに存在する場合は上書きしません。生成物は`data/`配下へ置き、Gitには含めません。

仮想音声デバイスなどの指定デバイスだけを使い、実音声録音から文字起こしまでを試験する場合:

```console
uv run python -m summarize_meeting.devtools.real_audio_smoke --source-wave data/meetings/stt-smoke-ja/audio/system.wav --microphone "Virtual microphone" --loopback "Monitor source" --speaker "Virtual speaker"
```

指定名は各デバイス一覧で1件に絞れる部分文字列にします。このコマンドは指定した再生先へWAVを流し、録音セッションを`data/meetings/`へ保存します。

録音・文字起こし済みセッションのPhase 2正常系を一括検証する場合:

```console
uv run python -m summarize_meeting.devtools.validate_phase2_session --session "data/meetings/<対象セッション>" --expect-microphone "マイクへ話した確認文" --expect-system "PCで再生した確認文"
```

成功時は終了コード0と`passed: true`を返します。WAVはストリーミング検査するため、長時間セッションでもファイル全体をメモリへ読み込みません。

実機録音の検証手順は [Phase 1 PoC手動検証](../docs/PHASE1_POC_MANUAL_TEST.md) を参照してください。

## 配置方針

完成版はインストーラーを使わず、アプリフォルダをコピーして利用するポータブル構成を予定しています。会議データ、設定、ログはアプリフォルダ内の `data/` に保存します。

## 現在のPoC制約

- Waylandでは共有対象を列挙・永続化せず、録音開始ごとにPortalのOSダイアログで選択します。
- Portalなし、ヘッドレス、SSHのみ、ロック画面、保護コンテンツは画面取得対象外です。
- Windows/X11の複数モニター、DPI、HDR、対象終了時の挙動は実機評価が必要です。
- OSのスリープ／休止状態をまたぐ録音は対象外です。

## ドキュメント

- [引き継ぎ資料](../docs/CODEX_HANDOFF_MEETING_MINUTES_TOOL.md)
- [Phase 1詳細設計](../docs/PHASE1_DETAILED_DESIGN.md)
- [Phase 2詳細設計](../docs/PHASE2_DETAILED_DESIGN.md)
- [Phase 2 STTスモーク試験](../docs/PHASE2_STT_SMOKE_TEST.md)
- [Phase 2 STT 1時間ベンチマーク](../docs/PHASE2_STT_1H_BENCHMARK.md)
- [Phase 2 実音声スモーク試験](../docs/PHASE2_REAL_AUDIO_SMOKE_TEST.md)
- [Phase 3詳細設計](../docs/PHASE3_DETAILED_DESIGN.md)
- [Phase 3話者分離スモーク試験](../docs/PHASE3_DIARIZATION_SMOKE_TEST.md)
- [Phase 4詳細設計](../docs/PHASE4_DETAILED_DESIGN.md)
- [Phase 4 画面解析スモーク試験](../docs/PHASE4_SCREEN_ANALYSIS_SMOKE_TEST.md)
- [Phase 5詳細設計](../docs/PHASE5_DETAILED_DESIGN.md)
- [Phase 5統合議事録スモーク試験](../docs/PHASE5_MINUTES_SMOKE_TEST.md)
