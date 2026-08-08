# Summarize Meeting

Teams、Google Meetなどのオンライン会議について、マイク音声、PC再生音声、選択したウィンドウの重要な画面変更をローカル保存し、会議終了後に議事録を生成するデスクトップアプリケーションです。

現在はPhase 1「記録基盤」の設計・PoC段階です。文字起こし、話者分離、画面理解、議事録生成はまだ実装していません。

## 開発環境

- Python 3.11
- uv
- Windows先行

```powershell
uv sync
uv run summarize-meeting
```

現時点のエントリポイントは `uv init` が生成した仮実装です。

## ドキュメント

- [引き継ぎ資料](../docs/CODEX_HANDOFF_MEETING_MINUTES_TOOL.md)
- [Phase 1詳細設計](../docs/PHASE1_DETAILED_DESIGN.md)
