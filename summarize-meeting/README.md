# Summarize Meeting

Teams、Google Meetなどのオンライン会議について、マイク音声、PC再生音声、選択したウィンドウの重要な画面変更をローカル保存し、会議終了後に議事録を生成するデスクトップアプリケーションです。

現在はPhase 1「記録基盤」のPoC段階です。Windows 11でマイクとPC音声の別トラック録音、音量メーター、表示中ウィンドウの画面変更保存、セッションJSON生成を試せます。文字起こし、話者分離、画面理解、議事録生成はまだ実装していません。

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

## 開発時の検証

```powershell
uv run ruff check .
uv run pytest -q
```

実機録音の検証手順は [Phase 1 PoC手動検証](../docs/PHASE1_POC_MANUAL_TEST.md) を参照してください。

## 配置方針

完成版はインストーラーを使わず、アプリフォルダをコピーして利用するポータブル構成を予定しています。会議データ、設定、ログはアプリフォルダ内の `data/` に保存します。

## 現在のPoC制約

- 画面取得は最終候補のWindows Graphics Captureではなく、選択ウィンドウの表示矩形をMSSで取得する暫定Adapterです。
- 対象ウィンドウが他のウィンドウに隠れると、隠した側の内容が画像へ入る場合があります。
- Windowsスリープ／休止状態をまたぐ録音は対象外です。

## ドキュメント

- [引き継ぎ資料](../docs/CODEX_HANDOFF_MEETING_MINUTES_TOOL.md)
- [Phase 1詳細設計](../docs/PHASE1_DETAILED_DESIGN.md)
