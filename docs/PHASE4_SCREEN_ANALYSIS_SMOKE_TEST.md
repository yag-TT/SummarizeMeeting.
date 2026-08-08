# Phase 4 Windows画面解析スモーク試験

## 1. 目的

Windows 11内蔵OCRを使い、Phase 1で保存された実スクリーンショットからtimestamp付き`analysis/screens.json`を生成できることを確認する。

実施日: 2026-08-08

## 2. 環境

- Windows 11
- Python 3.11 / uv
- Windows.Media.Ocr
- OCR language: `ja`
- OpenCV `imdecode`
- 入力セッション: `2026-08-08_191326_Phase2 物理デバイス試験_f57b5771`のコピー

原本セッションを変更しないよう、`data/.phase4-smoke/`へコピーして実行した。

## 3. 言語パック確認

`OcrEngine.available_recognizer_languages`から日本語`ja`が取得でき、`OcrEngine.is_language_supported(Language("ja"))`が利用可能であることを確認した。

## 4. worker試験

```powershell
uv run python -m summarize_meeting.processing.screen_analysis_worker `
  --session "<コピーしたセッション>" `
  --language ja
```

結果:

- 終了コード: 0
- 対象画像: 1
- 成功: 1
- 失敗: 0
- 入力event timestamp: 1600 ms
- 出力timestamp: 1600 ms
- 分類: `browser`
- title候補: `新しいタブ`を含むOCR先頭行
- OCR文字数: 325
- OCR行数: 26
- `analysis/screens.json`: atomic生成成功
- 判定: 合格

Chromeの新しいタブ、検索欄、ブックマーク等の日本語・英数字をローカルOCRで取得した。OCR誤認識はあるため、出力は補正せず原文と座標を保持する。

## 5. 自動試験範囲

- timestamp順sort
- type分類
- 重要行抽出
- path traversal拒否
- 画像単位の部分失敗
- 全失敗時の既存結果保持
- Job成功・失敗・キャンセル保存
- 会議一覧の実行可否
- GUI実行

## 6. 残る精度評価

- PowerPoint、Excel、Teams、課題管理画面での分類
- 日付、担当、決定事項の抽出率
- 15分・1時間会議の画像件数に対する処理時間
- Qwen3-VL量子化backendとの比較
