# Phase 2 STT 1時間ベンチマーク

## 1. 実施概要

| 項目 | 内容 |
|---|---|
| 実施日 | 2026-08-08 |
| OS | Windows 11 |
| GPUメモリ | `nvidia-smi`で16,376 MiBを認識 |
| STT | faster-whisper 1.2.1 |
| モデル | large-v3-turbo |
| 実行方式 | CUDA、float16、workerを別プロセスで直接実行 |
| 入力 | Windows音声合成による日本語WAVを反復した1時間音声、2トラック |

短時間スモーク試験で使用した約5秒の日本語音声を、開発用セッション生成ツールで各トラック3,600秒へ反復した。実会議音声の内容や無音分布を再現したものではなく、長時間入力に対する処理の完走、リソース使用量、出力構造を確認する試験である。

開始offset:

- microphone: 250ms
- system_audio: 1500ms

入力音声の合計時間は2時間である。実際の会議時間としては、同時に記録された2トラックの1時間会議に相当する。

## 2. 実行結果

| 測定値 | 結果 |
|---|---:|
| worker終了コード | 0 |
| 処理時間 | 203.88秒（約3分24秒） |
| 入力音声合計に対する処理倍率 | 約35.3倍速 |
| 開始前GPUメモリ使用量 | 1,077 MiB |
| 最大GPUメモリ使用量 | 3,656 MiB |
| GPUメモリ増加量の概算 | 2,579 MiB |
| 最大GPU使用率 | 100% |
| 終了後GPUメモリ使用量 | 1,093 MiB |
| JSON segment数 | 444 |
| Markdown発話数 | 444 |
| 不正な時刻範囲を持つsegment | 0 |

GPUメモリはWindows環境で`nvidia-smi`から取得したGPU全体の値であり、worker単体の厳密な割当量ではない。プロセスのworking setは今回の計測方法では信頼できる値を取得できなかったため、判定には使用していない。

## 3. 出力検証

- `analysis/transcription.json`のJob状態が`SUCCEEDED`
- microphoneとsystem_audioの`runtime_device`が両方とも`cuda`
- 全444 segmentで`0 <= start <= end`
- `output/transcript.md`の発話見出し数がJSON segment数と一致
- 最初のsegment開始時刻が0.250秒で、microphoneの開始offsetを反映
- 最後のsegment終了時刻が3601.800秒で、system_audioの開始offsetを含む範囲内
- worker終了後に対象Pythonプロセスが残らない
- 終了後のGPUメモリ使用量が開始前とほぼ同じ水準へ戻る
- workerの標準エラー出力が空

## 4. 判定

Phase 2の1時間正常系受入条件のうち、worker単体で確認できる項目を満たした。

- 1時間の2トラックをGPUメモリ不足や異常終了なしで処理できた
- 出力segmentの時刻範囲とJSON・Markdown間の件数整合性を確認できた
- workerプロセスとGPUメモリがJob終了後に解放された

## 5. 制約と残る確認

- 反復した合成音声のため、実会議音声に対する認識精度の判定には使えない
- workerを直接実行したため、同じ試験中のGUI操作応答性は測定していない
- GPUメモリはGPU全体の使用量であり、他プロセスの影響を完全には分離していない
- 実マイクと実際のPC再生音声を同時取得し、自動文字起こしまで通す手動試験が必要
- 再配布ライセンス確認は、アプリを配布しない方針のため対象外

## 6. 再現手順

既存の短時間セッションを反復して、長時間試験用セッションを生成できる。出力先がすでに存在する場合は上書きしない。

```powershell
cd summarize-meeting
uv run python -m summarize_meeting.devtools.benchmark_session `
  --source-session data\meetings\stt-smoke-ja `
  --output-session data\meetings\stt-benchmark-1h `
  --duration-seconds 3600
```

生成した会議データは`data/`配下にあり、Gitの管理対象には含めない。
