# Phase 5 統合議事録スモーク試験

## 1. 目的

実際の物理マイク、実スピーカー、WGC画面取得から作成したセッションについて、話者付き文字起こしと画面解析をtimestampで統合し、既存LM Studioモデルから`timeline.json`、`minutes.json`、`minutes.md`を生成できることを確認する。

実施日: 2026-08-08

## 2. 環境

- Windows 11
- Python 3.11 / uv
- 入力: `2026-08-08_191326_Phase2 物理デバイス試験_f57b5771`
- LLM server: LM Studio Local Server `127.0.0.1:1234`
- モデル: PCへ導入済みの`qwen/qwen3.6-35b-a3b`
- API identifier: `summarize-meeting`
- context: 16,384
- Structured Output: JSON Schema
- 外部API送信: なし

モデル比較では導入済みGemma 4 12Bも実行した。小さな入力では正常終了したが要約品質が低かったため、この環境の推奨はQwen 3.6とした。これはアプリの固定依存ではなく、ユーザー環境で選択する既存モデルである。

## 3. 入力準備

Phase 3とPhase 4 workerを実行した。

```powershell
uv run python -m summarize_meeting.processing.diarization_worker `
  --session "data\meetings\2026-08-08_191326_Phase2 物理デバイス試験_f57b5771" `
  --models-dir models

uv run python -m summarize_meeting.processing.screen_analysis_worker `
  --session "data\meetings\2026-08-08_191326_Phase2 物理デバイス試験_f57b5771" `
  --language ja
```

結果:

- 話者分離: 成功
- 画面解析: 1画像中1成功
- 画面timestamp: 1,600 ms
- 話者付き発話: 2件
- PC音声発話: `来週の金曜日までに、テスト結果を共有します。`

## 4. LM Studio準備

```powershell
lms server start --port 1234
lms load qwen/qwen3.6-35b-a3b `
  --identifier summarize-meeting `
  --context-length 16384 `
  --ttl 600
```

モデルは既にPCへ導入済みのものを使用し、新規ダウンロードしていない。TTLにより10分未使用で解放する設定とした。

## 5. worker試験

```powershell
uv run python -m summarize_meeting.processing.minutes_worker `
  --session "data\meetings\2026-08-08_191326_Phase2 物理デバイス試験_f57b5771" `
  --base-url http://127.0.0.1:1234/v1 `
  --model summarize-meeting
```

結果:

- 終了コード: 0
- runtime: `LM Studio local API`
- timeline speech: 2件
- timeline screen: 1件
- transcript source: `analysis/diarized_transcription.json`
- screen source: `analysis/screens.json`
- chunk count: 1
- `analysis/timeline.json`: 生成成功
- `analysis/minutes.json`: 生成成功
- `output/minutes.md`: 生成成功
- 判定: 合格

## 6. 内容検証

生成後検証により次を確認した。

- 参加者はtimelineの`自分`、`Speaker 1`から決定し、LLMの創作値を使わない
- TODO内容は`テスト結果を共有する`
- 担当者は根拠に明示がないため`不明`
- 期限は原文どおり`来週の金曜日`
- OCRだけから推測された決定事項は除外
- 存在しない絶対日付へ変換しない
- timelineにないevidence IDを持つ項目を除外
- Markdownに必須9節を出力

初回のGemma試験で、根拠ID付きでも誤った絶対日付とOCR由来の架空決定が生成されることを確認した。この結果を受け、プロンプトだけでなく決定的な生成後検証を実装した。

## 7. 自動試験

```text
152 passed, 1 skipped
```

対象:

- timeline統合
- 通常文字起こしfallback
- 画面解析なし
- map-reduce
- LM Studio Structured Output request
- localhost制約
- 根拠検証
- 期限補正
- 生成制御token除去
- Job状態
- Phase 1〜4回帰

## 8. 手動GUI確認

1. LM Studio Local Serverを起動し、モデルを1つロードする
2. 必要なら`SUMMARIZE_MEETING_LLM_MODEL`を設定してアプリを起動する
3. 対象セッションを選ぶ
4. 「議事録生成」を押す
5. 状態が`実行中`から`完了`になることを確認する
6. `analysis/timeline.json`、`analysis/minutes.json`、`output/minutes.md`を確認する
7. 「再実行」でgeneration IDとcompleted_atが更新されることを確認する

## 9. 残る評価

- 15分、1時間会議の分割生成時間
- 実名設定後の話者名反映
- PowerPoint等の重要画面を含む参考情報生成
- 異なる既存モデル間の品質比較
- GUIからのモデル選択
