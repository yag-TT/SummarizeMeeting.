# Phase 2 STT スモーク試験

## 1. 実施概要

| 項目 | 内容 |
|---|---|
| 実施日 | 2026-08-08 |
| OS | Windows 11 |
| GPU | NVIDIA RTX 4060、CTranslate2から1 deviceを認識 |
| STT | faster-whisper 1.2.1 |
| モデル | large-v3-turbo |
| 言語 | ja |
| 入力 | Windows音声合成による日本語WAV、約5秒を2トラック |

入力文:

- microphone: `今回の会議では、文字起こし機能の動作を確認します。`
- system_audio: `来週の金曜日までに、テスト結果を共有します。`

開始offset:

- microphone: 250ms
- system_audio: 1500ms

## 2. 初回実行

モデルは`models/faster-whisper/`へ正常に取得された。取得後のモデル関連ファイルは8ファイル、約1.62GBだった。

CUDA deviceは認識されたが、推論開始時に`cublas64_12.dll`をロードできずGPU推論は失敗した。この環境ではCUDA deviceの検出だけでは実行可能性を保証できないことを確認した。

## 3. 修正

CUDA関連のruntime errorを検出した場合、同じJob内でモデルをCPU `int8`として再ロードし、一度だけ再試行するようにした。通常の音声・推論エラーはフォールバック対象にせず、そのままJob失敗とする。

## 4. 再試験結果

CPUフォールバック後にJobは正常完了した。

- `analysis/transcription.json`: 生成成功
- `output/transcript.md`: 生成成功
- microphone認識結果: 入力文と一致
- system認識結果: 入力文と一致
- microphone出力開始時刻: 0.250秒
- system出力開始時刻: 1.500秒
- JSON segment数: 2
- Markdown発話数: 2
- worker終了コード: 0

## 5. 判定

Phase 2の短時間正常系として、以下を確認できた。

- 実モデルの取得
- 2トラックのローカル文字起こし
- CUDA runtime不足時のCPU継続
- セッション共通時刻へのoffset反映
- timestamp順統合
- JSONとMarkdownの生成

## 6. GPU再試験

- 旧検証では同梱CUDAを使用したが、現行実装はOSへ導入した公式CUDA 12 / cuDNN 9だけを自動検出し、利用不可時はCPUへフォールバックする
- GPU `float16`で同じスモーク試験を再実行し、約5秒×2トラックを8.78秒で正常処理した
- 両トラックの`runtime_device`が`cuda`であることをJSONで確認した
- GPU試験でも2つの日本語認識結果と開始offsetが維持された

## 7. 残る課題

- 実マイク・実PC再生音声で認識品質を確認する
- 1時間試験の結果は [Phase 2 STT 1時間ベンチマーク](PHASE2_STT_1H_BENCHMARK.md) を参照する
- Windows音声デバイスを通した試験結果は [Phase 2 Windows実音声スモーク試験](PHASE2_REAL_AUDIO_SMOKE_TEST.md) を参照する
