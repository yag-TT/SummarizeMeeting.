# マイク音声改善 詳細設計

## 目的

録音経路の安定性を維持したまま、Brio 100のマイク録音に含まれる環境ノイズと低い発話音量を録音終了後に改善する。原音は証跡・再処理用として変更しない。

## データフロー

1. `audio/manifest.json`から`microphone` trackの原音を安全な相対パスとして解決する。
2. 48 kHz・モノラル・PCM16を検証する。
3. sherpa-onnx DPDFNet 48 kHzモデルへ480 sampleずつ渡し、一定メモリで一時WAVへノイズ除去結果を書く。
4. PyAVで80 Hzハイパス、-20 LUFS、LRA 7、true peak -1.5 dBFSを適用する。
5. 形式、長さ、クリッピングを検証して`audio/microphone.enhanced.wav`へatomic確定する。
6. 処理条件と原音・改善版の品質指標を`analysis/audio_enhancement.json`へatomic保存する。
7. 文字起こしは成功メタデータと改善版WAVの両方が有効な場合だけ改善版を使用する。

## ジョブと障害動作

- GUIから子プロセスworkerを起動し、進捗、成功、失敗、キャンセルを`analysis/jobs.json`へ保存する。
- 新規録音は保存確定後に自動改善し、自動文字起こしが有効なら改善完了後に開始する。
- モデル不足、破損WAV、処理失敗では原音と以前の正常な改善版を変更しない。自動文字起こしは原音へフォールバックする。
- 手動再実行では既存文字起こしを自動更新せず、利用者へ再実行を案内する。

## モデルと配置

- `sherpa-onnx==1.13.2`
- Windowsネイティブランタイムとして`sherpa-onnx-core==1.13.2`
- `models/sherpa-onnx/speech-enhancement/dpdfnet2_48khz_hr.onnx`
- SHA-256: `0B399F8A58DC4D70D8CD97541F5C39869406145193B957D00A03B66070944928`
- モデルはリポジトリへ含めず、`scripts/setup-audio-enhancement-model.ps1`で取得・検証する。
