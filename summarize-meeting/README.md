# Summarize Meeting

Teams、Google Meetなどのオンライン会議について、マイク音声、PC再生音声、選択したウィンドウの重要な画面変更をローカル保存し、会議終了後に議事録を生成するデスクトップアプリケーションです。

現在はPhase 1「記録基盤」のPoC段階です。Windows 11でマイクとPC音声の別トラック録音、音量メーター、Windows Graphics Captureによる選択ウィンドウの画面変更保存、セッションJSON生成を試せます。文字起こし、話者分離、画面理解、議事録生成はまだ実装していません。

## 開発環境

- Python 3.11
- uv
- Windows 11先行
- Phase 1のUIは日本語のみ

```powershell
uv sync
uv run summarize-meeting
```

初回の依存取得後、アプリ画面で会議名、マイク、PC音声、取得画面を選択して「会議開始」を押します。PC音声は選択した出力デバイスから再生される全音声が対象です。

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
└─ screenshots/
   ├─ events.jsonl
   └─ 000001.png ...
```

アプリ起動時に正常終了していないセッションを検出すると、復旧確認を表示します。復旧時は元の `.work` segmentを変更せず、`audio/microphone.recovered.wav` や `audio/system.recovered.wav` を新しく生成します。

録音中に音声デバイスが切断された場合は、別デバイスへ切り替えず、同じdevice IDへ最大5回、約10秒間再接続を試します。切断区間は `audio/manifest.json` の `gaps` に記録されます。

会議開始前に保存先の空き容量を確認し、5 GiB未満では録音を開始しません。録音中は60秒ごとに確認し、5 GiB未満になった場合は新しい画面保存を停止して音声録音を優先します。データを自動削除して容量を確保することはありません。

前回使用したマイクとPC音声のdevice ID、画面変更検知設定、保持方針、ログレベルは `data/settings.json` に保存します。壊れた設定は `data/settings.corrupt-<timestamp>.json` へ退避し、既定値で起動します。保存済みdevice IDが見つからない場合、別デバイスへ自動切替はしません。

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

実機録音の検証手順は [Phase 1 PoC手動検証](../docs/PHASE1_POC_MANUAL_TEST.md) を参照してください。

## 配置方針

完成版はインストーラーを使わず、アプリフォルダをコピーして利用するポータブル構成を予定しています。会議データ、設定、ログはアプリフォルダ内の `data/` に保存します。

## 現在のPoC制約

- Windows Graphics Captureの複数モニター、DPI、HDR、保護コンテンツ、Windowsロック、Remote Desktopは実機評価中です。
- Windowsスリープ／休止状態をまたぐ録音は対象外です。

## ドキュメント

- [引き継ぎ資料](../docs/CODEX_HANDOFF_MEETING_MINUTES_TOOL.md)
- [Phase 1詳細設計](../docs/PHASE1_DETAILED_DESIGN.md)
