# Summarize Meeting

Teams、Google Meetなどのオンライン会議について、マイク音声、PC再生音声、選択したウィンドウの重要な画面変更をローカル保存し、会議終了後に議事録を生成するデスクトップアプリケーションです。

現在はPhase 2「STT」のPoC段階です。Phase 1の記録機能に加え、録音終了後にfaster-whisperを別プロセスで実行し、マイクとPC音声の文字起こし、timestamp付きJSON、仮の`transcript.md`生成を試せます。話者分離、画面理解、議事録生成はまだ実装していません。

## 開発環境

- Python 3.11
- uv
- Windows 11先行
- UIは日本語のみ

```powershell
uv sync
uv run summarize-meeting
```

初回の依存取得後、アプリ画面で会議名、マイク、PC音声、取得画面を選択して「会議開始」を押します。PC音声は選択した出力デバイスから再生される全音声が対象です。

録音終了後、画面下部の「文字起こし」から実行します。既定モデルは`large-v3-turbo`、言語は日本語です。初回実行時はモデルを`models/faster-whisper/`へ取得するため時間とインターネット接続が必要です。会議音声と文字起こし結果は外部サービスへ送信しません。

「録音終了後に自動で文字起こし」をONにすると、正常に確定した録音だけを会議終了後に自動処理します。既定はOFFです。録音確定エラー、音声不足、アプリ終了時には自動実行せず、録音済みセッションから手動で再実行できます。

「解析対象」には`data/meetings/`内の録音済みセッションが新しい順に表示されます。アプリを再起動した後でも過去の会議を選択し、文字起こしの実行・再実行ができます。壊れた`session.json`が混在しても、そのフォルダ名を使って他のセッションとともに一覧表示します。

文字起こしJobの開始・成功・失敗・キャンセルは`analysis/jobs.json`へatomic保存します。実行中にアプリが終了して`RUNNING`が残った場合、次回起動時は「前回中断」として表示し、再実行できます。

RTX GPUを使う開発環境では、CUDA 12のcuBLASとcuDNN 9をアプリ内へ準備します。スクリプトは固定したarchiveをSHA-256検証後に`runtime/cuda/bin/`へ展開します。本アプリは第三者へ配布せず、ローカル環境でのみ使用します。

```powershell
.\scripts\setup-cuda-runtime.ps1
```

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
│  └─ transcription.json
└─ output/
   └─ transcript.md
```

文字起こしworkerだけを実行する場合:

```powershell
uv run python -m summarize_meeting.processing.transcription_worker `
  --session "<data/meetings/セッション>" `
  --models-dir "<アプリルート/models>" `
  --cuda-runtime-dir "<アプリルート/runtime/cuda/bin>"
```

アプリ起動時に正常終了していないセッションを検出すると、復旧確認を表示します。復旧時は元の `.work` segmentを変更せず、`audio/microphone.recovered.wav` や `audio/system.recovered.wav` を新しく生成します。

録音中に音声デバイスが切断された場合は、別デバイスへ切り替えず、同じdevice IDへ最大5回、約10秒間再接続を試します。切断区間は `audio/manifest.json` の `gaps` に記録されます。

PC音声loopbackにはSoundCardを使用します。物理マイクの音声形式をSoundCardで開始できない場合は、同名のsounddevice Windows WASAPI入力へ自動的にフォールバックします。

会議開始前に保存先の空き容量を確認し、5 GiB未満では録音を開始しません。録音中は60秒ごとに確認し、5 GiB未満になった場合は新しい画面保存を停止して音声録音を優先します。データを自動削除して容量を確保することはありません。

前回使用したマイクとPC音声のdevice ID、画面変更検知設定、保持方針、自動文字起こし、ログレベルは `data/settings.json` に保存します。壊れた設定は `data/settings.corrupt-<timestamp>.json` へ退避し、既定値で起動します。保存済みdevice IDが見つからない場合、別デバイスへ自動切替はしません。

`audio/manifest.json` には2track共通のmonotonic origin、各trackの推定開始offset、WAV時間、再接続gapを除く稼働時間、duration drift、queue最大使用率、pressure回数、overflow回数を保存します。診断値に基づく音声の自動伸縮や無音挿入は行いません。

スクリーンショットはtempへ書き込んで再decode検証した後にatomic確定します。一時的な画像保存失敗では画面Captureと音声を止めず、baselineを維持して再試行します。異常終了後に残った正常なPNG tempは起動時復旧の対象です。

## 開発時の検証

```powershell
uv run ruff check .
uv run pytest -q
```

対話デスクトップ上でWGCの自己ウィンドウ統合テストも実行する場合:

```powershell
$env:SUMMARIZE_MEETING_RUN_WGC_TESTS = "1"
uv run pytest -q tests/integration/test_wgc_backend.py
```

短い録音済みセッションから、長時間STT試験用の反復音声セッションを生成する場合:

```powershell
uv run python -m summarize_meeting.devtools.benchmark_session `
  --source-session data\meetings\stt-smoke-ja `
  --output-session data\meetings\stt-benchmark-1h `
  --duration-seconds 3600
```

出力先がすでに存在する場合は上書きしません。生成物は`data/`配下へ置き、Gitには含めません。

VB-Audio Virtual Cableなどの指定デバイスだけを使い、Windows録音から文字起こしまでを試験する場合:

```powershell
uv run python -m summarize_meeting.devtools.real_audio_smoke `
  --source-wave data\meetings\stt-smoke-ja\audio\system.wav `
  --microphone "CABLE Output" `
  --loopback "CABLE Input" `
  --speaker "CABLE Input"
```

指定名は各デバイス一覧で1件に絞れる部分文字列にします。このコマンドは指定した再生先へWAVを流し、録音セッションを`data/meetings/`へ保存します。

録音・文字起こし済みセッションのPhase 2正常系を一括検証する場合:

```powershell
uv run python -m summarize_meeting.devtools.validate_phase2_session `
  --session "data\meetings\<対象セッション>" `
  --expect-microphone "マイクへ話した確認文" `
  --expect-system "PCで再生した確認文"
```

成功時は終了コード0と`passed: true`を返します。WAVはストリーミング検査するため、長時間セッションでもファイル全体をメモリへ読み込みません。

実機録音の検証手順は [Phase 1 PoC手動検証](../docs/PHASE1_POC_MANUAL_TEST.md) を参照してください。

## 配置方針

完成版はインストーラーを使わず、アプリフォルダをコピーして利用するポータブル構成を予定しています。会議データ、設定、ログはアプリフォルダ内の `data/` に保存します。

## 現在のPoC制約

- Windows Graphics Captureの複数モニター、DPI、HDR、保護コンテンツ、Windowsロック、Remote Desktopは実機評価中です。
- Windowsスリープ／休止状態をまたぐ録音は対象外です。

## ドキュメント

- [引き継ぎ資料](../docs/CODEX_HANDOFF_MEETING_MINUTES_TOOL.md)
- [Phase 1詳細設計](../docs/PHASE1_DETAILED_DESIGN.md)
- [Phase 2詳細設計](../docs/PHASE2_DETAILED_DESIGN.md)
- [Phase 2 STTスモーク試験](../docs/PHASE2_STT_SMOKE_TEST.md)
- [Phase 2 STT 1時間ベンチマーク](../docs/PHASE2_STT_1H_BENCHMARK.md)
- [Phase 2 Windows実音声スモーク試験](../docs/PHASE2_REAL_AUDIO_SMOKE_TEST.md)
