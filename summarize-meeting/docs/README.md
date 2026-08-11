# プロジェクトドキュメント

このディレクトリには、Summarize Meetingの機能、設計、内部API、保存データ、開発方法をまとめています。アプリを初めて扱う場合は、次の順序で読むと全体像を把握できます。

1. [プロジェクト概要](project-overview.md)
2. [アーキテクチャ](architecture.md)
3. [内部API・worker APIリファレンス](api-reference.md)
4. [保存データ形式](data-formats.md)
5. [開発・テストガイド](development.md)

Ubuntu 22.04へ導入する場合は、[Ubuntu 22.04へのコピー導入手順](ubuntu-install.md)も参照してください。

## 目的別の参照先

| 知りたいこと | 参照先 |
|---|---|
| アプリが提供する機能と制約 | [プロジェクト概要](project-overview.md) |
| モジュール構成、処理フロー、責務分担 | [アーキテクチャ](architecture.md) |
| Controller、Service、Repositoryの呼び出し方 | [内部API・worker APIリファレンス](api-reference.md) |
| workerのCLI引数とJSON Linesプロトコル | [内部API・worker APIリファレンス](api-reference.md#8-worker-cli) |
| llama.cppへ送るHTTPリクエスト | [内部API・worker APIリファレンス](api-reference.md#10-llamacpp-http-api) |
| `session.json`や解析JSONの内容 | [保存データ形式](data-formats.md) |
| 環境構築、モデル準備、テスト | [開発・テストガイド](development.md) |
| Ubuntu/WSL2固有の準備 | [Ubuntu導入手順](ubuntu-install.md) |

## 正本

この文書は現在の`src/summarize_meeting/`を基準にしています。スキーマ定数の正本は`domain/session.py`、設定の正本は`infrastructure/settings.py`、worker引数の正本は`processing/*_worker.py`です。実装を変更した場合は、関連文書とテストも同じ変更で更新してください。
