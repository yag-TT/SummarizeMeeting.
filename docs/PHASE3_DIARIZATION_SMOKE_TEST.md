# Phase 3 話者分離スモーク試験

## 1. 目的

Windows 11上で、sherpa-onnx実モデルによる話者分離と、Phase 2録音済みセッションから話者付き全文を保存する正常系を確認する。

実施日: 2026-08-08

## 2. 環境

- Windows 11
- Python 3.11 / uv
- `sherpa-onnx==1.12.39`
- `onnxruntime==1.24.4`
- segmentation: `sherpa-onnx-pyannote-segmentation-3-0/model.int8.onnx`
- embedding: `nemo_en_titanet_small.onnx`
- provider: CPU

モデルは`setup-diarization-models.ps1`で固定URLから取得し、次のSHA-256を検証した。

- segmentation archive: `24615EE884C897D9D2BA09BB4D30DA6BB1B15E685065962DB5B02E76E4996488`
- embedding model: `AD4A1802485D8B34C722D2A9D04249662F2ECE5D28A7A039063CA22F515A789E`

## 3. 公式4話者音声

sherpa-onnx公式サンプル`0-four-speakers-zh.wav`へ既知話者数4を指定した。

結果:

- 終了コード: 0
- 検出turn: 10
- 検出cluster: `[0, 1, 2, 3]`
- 判定: 合格

## 4. Phase 2セッション統合

ユーザー確認済みセッション`2026-08-08_191326_Phase2 物理デバイス試験_f57b5771`を作業用ディレクトリへコピーし、原本を変更せず、話者数1で`diarization_worker`を実行した。

結果:

- 終了コード: 0
- PC音声: 31.3秒
- manifest開始offset: 659 ms
- 検出話者: 1
- 検出turn: 1
- `analysis/diarization.json`: 生成成功
- `analysis/speaker_names.json`: 生成成功
- `analysis/diarized_transcription.json`: 生成成功
- `output/transcript.md`: `自分`と`Speaker 1`を含む話者付き全文へ再生成成功
- 判定: 合格

## 5. Windows DLL競合の確認

初回試験では`System32/onnxruntime.dll` 1.17.1が先にロードされ、sherpa-onnxが必要とするAPI 24を利用できなかった。仮想環境内のONNX Runtime 1.24.4 DLLを絶対パスで先読みする処理を追加し、通常のworkerコードパスで4話者検出とセッション統合の両方が成功することを確認した。

## 6. 自動試験

話者分離service、timestamp統合、曖昧判定、安全な相対パス、PyAV resample、Job状態、GUI実行、話者名更新、会議一覧の実行可否をユニットテスト対象とする。
