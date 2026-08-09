# Summarize Meeting

Teams、Google Meetなどのオンライン会議や一般的な会話について、マイク音声、PC再生音声、選択したウィンドウの重要な画面変更をローカル保存し、録音終了後に会話内容を要約するデスクトップアプリケーションです。会議では議事録として利用できます。

Phase 5「統合会話要約」の正常系PoCまで実装しています。Phase 1の記録、Phase 2のfaster-whisper文字起こし、Phase 3の話者分離、Phase 4のローカルOCR画面解析をtimestampで統合し、llama.cppで実行する既存モデルを使って根拠付きの会話要約を生成できます。

## 開発環境

- Python 3.11
- uv
- Windows 11 / Ubuntu 22.04
- UIは日本語のみ

```console
uv sync --frozen
uv run summarize-meeting
```

### Ubuntu 22.04 / WSL2

UbuntuではPython 3.11を`uv`に管理させることができます。

WSL2 + WSLgでは、GUI、マイク、Linux側の再生音声に次のOSパッケージが必要です。`libasound2`は`libportaudio2`の依存として導入されます。

```bash
sudo apt update
sudo apt install -y fontconfig fonts-noto-cjk libportaudio2 libpulse0 libegl1 libgl1
uv python install 3.11
export UV_PROJECT_ENVIRONMENT="$HOME/.local/share/uv/venvs/summarize-meeting"
uv sync --frozen
uv run summarize-meeting
```

Windows用`.venv`とLinux用仮想環境は互換性がありません。また、`/mnt/c`や`/mnt/d`上の仮想環境は多数の小ファイルI/Oが遅いため、WSLではLinux側ファイルシステムの専用環境を使用します。同じshellで後続の`uv run`を実行するか、新しいshellでも`UV_PROJECT_ENVIRONMENT="$HOME/.local/share/uv/venvs/summarize-meeting"`を設定してください。

ネイティブのUbuntu 22.04 GNOME Waylandで画面取得も使用する場合は、Qtが要求するScreenCast PortalとPipeWireを追加します。

```bash
sudo apt install -y pipewire xdg-desktop-portal xdg-desktop-portal-gnome
```

X11セッションを使用する場合だけ、Qt XCB plugin用の`libxcb-cursor0`も導入します。

```bash
sudo apt install -y libxcb-cursor0
```

システムの`ffmpeg`コマンドは使用しません。PySide6とPyAVのwheelが必要なFFmpeg共有ライブラリを同梱するため、`ffmpeg`パッケージは不要です。診断にはPySide6のQtDBusを使うため、`gdbus`を提供する`libglib2.0-bin`も必須ではありません。

日本語UIには`fonts-noto-cjk`の`Noto Sans CJK JP`を優先して使用します。日本語フォントが見つからない場合、読めないUIを起動せず、インストールコマンドを端末とエラーダイアログへ表示します。

WSLgはWayland/X11とPulseAudioの接続を提供しますが、完全なUbuntuデスクトップセッションではありません。Windows側のデスクトップやWindowsアプリの画面取得は対象外です。ネイティブWayland向けPortalパッケージをWSLへ追加しても、この制限は解消しません。

#### Ubuntu/WSL2で話者分離をGPU実行する

Ubuntu 22.04 / WSL2 x86_64では、公式の`sherpa-onnx 1.13.4+cuda12.cudnn9` wheelを使用し、segmentationとembeddingをCUDAで実行します。segmentationはCUDA向け`model.onnx`、CPUフォールバック時は`model.int8.onnx`を使用します。CUDAを利用できない場合やCUDA初期化・推論に失敗した場合は、同じ処理をCPUで1回だけ実行します。Windows版はCPU実行です。

WSL2ではWindows側のNVIDIAドライバーを使用します。WSL内へ`cuda-drivers`、`cuda`、`cuda-12-8`、`nvidia-driver-*`をインストールしないでください。WSL用リポジトリから、ドライバーを含まない`cuda-toolkit-12-8`だけを導入します。

```bash
cd /tmp
sudo apt update
sudo apt install -y curl ca-certificates zlib1g
curl -fsSLO \
  https://developer.download.nvidia.com/compute/cuda/repos/wsl-ubuntu/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb

echo \
  'deb [signed-by=/usr/share/keyrings/cuda-archive-keyring.gpg] https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/ /' \
  | sudo tee /etc/apt/sources.list.d/cudnn-ubuntu2204-x86_64.list

sudo apt update
sudo apt install -y cuda-toolkit-12-8 cudnn9-cuda-12 zlib1g

echo 'export PATH=/usr/local/cuda-12.8/bin${PATH:+:${PATH}}' \
  | sudo tee /etc/profile.d/cuda-12-8.sh
echo '/usr/local/cuda-12.8/lib64' \
  | sudo tee /etc/ld.so.conf.d/cuda-12-8.conf
sudo ldconfig
source /etc/profile.d/cuda-12-8.sh
```

依存同期と話者分離モデルの準備後、GPU、Toolkit、必要な共有ライブラリ、sherpa-onnxのモデル初期化を確認します。

```bash
uv sync --frozen
uv run python scripts/setup_models.py diarization
nvidia-smi
nvcc --version
ldconfig -p | grep -E \
  'libcudart.so.12|libcublas.so.12|libcublasLt.so.12|libcurand.so.10|libcufft.so.11|libcudnn.so.9'
uv run python scripts/doctor.py
```

`doctor.py`の`speaker-diarization`が`OK`かつ`provider=cuda`ならGPU話者分離を使用できます。CUDA ToolkitまたはcuDNNがない環境でもアプリは起動し、`WARN`と理由を表示してCPU話者分離を使用します。実行結果の`analysis/diarization.json`には、実際に使用した`provider`（`cuda`または`cpu`）とフォールバック理由を保存します。

画面取得は両OSともQt Multimediaを使用します。WindowsとX11では画面またはウィンドウを選択できます。Ubuntu 22.04 Waylandでは「開始時にOSダイアログで共有画面を選択」を選び、会議開始後にXDG Desktop Portalの共有対象選択へ応答します。許可は録音ごとに必要です。Portalを拒否した場合や共有対象が終了した場合は画面取得だけが停止し、音声録音は継続します。ヘッドレス、SSHのみ、ロック画面での取得は対象外です。

初回の依存取得後、アプリ画面で会議名、マイク、PC音声、取得画面を選択して「会議開始」を押します。PC音声は選択した出力デバイスから再生される全音声が対象です。

マイク音声は原音を`audio/microphone.wav`へ保存し、文字起こしもこの原音を使用します。派生WAVは生成しません。

録音終了後、画面下部の「文字起こし」から実行します。既定モデルは`large-v3-turbo`、言語は日本語です。初回実行時はモデルを`models/faster-whisper/`へ取得するため時間とインターネット接続が必要です。会議音声と文字起こし結果は外部サービスへ送信しません。

「録音終了後に自動で文字起こし」をONにすると、正常に確定した録音だけを会議終了後に自動処理します。既定はOFFです。録音確定エラー、音声不足、アプリ終了時には自動実行せず、録音済みセッションから手動で再実行できます。

「解析対象」には`data/meetings/`内の現行形式（`session.json` schema version 2）の録音済みセッションだけが新しい順に表示されます。壊れたデータや旧形式は一覧・復旧・解析の対象になりません。

文字起こしJobの開始・成功・失敗・キャンセルは`analysis/jobs.json`へatomic保存します。実行中にアプリが終了して`RUNNING`が残った場合、次回起動時は「前回中断」として表示し、再実行できます。

文字起こし完了後は「話者分離」から話者数を`自動`または`1人`〜`10人`で選び、PC音声の話者を分離できます。マイク発話は`自分`、PC音声は`Speaker 1`などで表示されます。完了後に話者名を編集して保存すると、話者付きJSONと`transcript.md`を推論なしで再生成します。

録音済みセッションにスクリーンショットがある場合は「画面解析」を実行できます。両OSともPaddleOCR 3.7とPP-OCRv6 mediumのONNXモデルを使用し、日本語・英語を含むOCR結果、画面種別、タイトル候補、重要行を外部サービスへ送信せず`analysis/screens.json`へ保存します。

文字起こし完了後は「会話要約」を実行できます。会議、雑談、相談、インタビューなどの種類を問わず、話者付き文字起こしを優先して、全体要約・主な話題・会話の要点を生成します。明示的な合意、今後の対応、未解決事項は存在する場合だけ出力します。画面解析結果も任意で統合し、`analysis/timeline.json`、`analysis/minutes.json`、`output/minutes.md`へ保存します。LAN内のllama.cpp serverとサーバーへ導入済みのモデルを使用し、アプリからLLMをダウンロードしません。

llama.cpp serverでモデルをロードし、OpenAI互換APIをLANへ公開します。LLM接続先に既定値はありません。`data/settings.json`の`llm.base_url`へHTTPまたはHTTPSのOpenAI互換API URLを設定し、アプリを再起動してください。

```json
{
  "schema_version": 1,
  "llm": {
    "base_url": "http://llm-host:8081/v1"
  }
}
```

```console
llama-server --host 0.0.0.0 --port 8081 --model <model.gguf> --ctx-size 16384
uv run summarize-meeting
```

起動環境へ`SUMMARIZE_MEETING_LLM_URL`を設定した場合は、`data/settings.json`より環境変数を優先します。HTTPの場合は文字起こし内容が暗号化されず送信されます。`GET /v1/models`でモデルが1つだけ見える場合は自動選択します。複数モデルが見える場合は`SUMMARIZE_MEETING_LLM_MODEL`で使用するモデルIDを指定します。エンドポイントが未設定の場合も録音や各解析は使用できますが、会話要約は「対象なし」と表示され、設定と再起動の案内が表示されます。

初回は固定URLとSHA-256検証付きスクリプトで、CPU/GPU話者分離モデルを`models/sherpa-onnx/diarization/`へ配置します。

```console
uv run python scripts/setup_models.py diarization
```

OCRモデルは固定revisionから取得し、ONNXファイルのSHA-256を検証して`models/paddleocr/`へ配置します。

```console
uv run python scripts/setup_models.py ocr
```

全モデルをまとめて準備する場合は`uv run python scripts/setup_models.py all`を実行します。モデル不足のままオフラインで解析した場合も、同じコマンドを案内します。

CPUだけで全機能を実行できます。文字起こしはCTranslate2、Ubuntu/WSL2 x86_64の話者分離は公式sherpa-onnx GPU wheelからCUDAを利用し、それぞれGPU初期化に失敗した場合はCPUへ自動フォールバックします。CUDA DLLや非公式runtime archiveはアプリへ同梱しません。

環境の非破壊診断は次のコマンドで実行できます。

```console
uv run python scripts/doctor.py
```

OS、デスクトップセッション、Portal、PipeWire/PulseAudio、音声デバイス、OCRモデル、文字起こし用CUDA、話者分離用GPU wheel・CUDA共有ライブラリ・モデル初期化、保存先の書込権限を表示します。

記録データは次へ保存されます。

データ形式は現行版だけをサポートします。`session.json`と`audio/manifest.json`はschema version 2です。manifest内のtrack名は`microphone`または`system`、`file`は`microphone.wav`のような`audio/`基準の単純なファイル名です。旧形式からの移行・読み替えは行いません。

```text
data/meetings/<session>/
├─ session.json
├─ events.jsonl
├─ audio/
│  ├─ microphone.wav
│  ├─ system.wav
│  ├─ manifest.json
│  └─ .work/
├─ screenshots/
│  ├─ events.jsonl
│  └─ 000001.png ...
├─ analysis/
│  ├─ jobs.json
│  ├─ transcription.json
│  ├─ diarization.json
│  ├─ diarized_transcription.json
│  ├─ speaker_names.json
│  ├─ screens.json
│  ├─ timeline.json
│  └─ minutes.json
└─ output/
   ├─ transcript.md
   └─ minutes.md
```

文字起こしworkerだけを実行する場合:

```console
uv run python -m summarize_meeting.processing.transcription_worker --session "<data/meetings/セッション>" --models-dir "<アプリルート/models>"
```

話者分離workerだけを実行する場合:

```console
uv run python -m summarize_meeting.processing.diarization_worker --session "<data/meetings/セッション>" --models-dir "<アプリルート/models>" --speaker-count 2
```

話者数を自動推定する場合は`--speaker-count`を省略します。

画面解析workerだけを実行する場合:

```console
uv run python -m summarize_meeting.processing.screen_analysis_worker --session "<data/meetings/セッション>" --models-dir "<アプリルート/models>" --language ja
```

会話要約workerだけを実行する場合:

```console
uv run python -m summarize_meeting.processing.minutes_worker --session "<data/meetings/セッション>" --base-url http://llm-host:8081/v1
```

アプリ起動時に正常終了していないセッションを検出すると、復旧確認を表示します。復旧時は元の `.work` segmentを変更せず、`audio/microphone.recovered.wav` や `audio/system.recovered.wav` を新しく生成します。

録音中に音声デバイスが切断された場合は、別デバイスへ切り替えず、同じdevice IDへ最大5回、約10秒間再接続を試します。切断区間は `audio/manifest.json` の `gaps` に記録されます。

PC音声loopbackにはSoundCardを使用します。WindowsではWASAPI loopback、UbuntuではPulseAudio互換のmonitor sourceを列挙します。物理マイクはWindowsではsounddeviceのWASAPI入力を優先し、開始できない場合だけSoundCardへフォールバックします。UbuntuではSoundCardのPulseAudio入力を優先し、開始できない場合は同名のsounddevice入力を試します。

会議開始前に保存先の空き容量を確認し、5 GiB未満では録音を開始しません。録音中は60秒ごとに確認し、5 GiB未満になった場合は新しい画面保存を停止して音声録音を優先します。データを自動削除して容量を確保することはありません。

前回使用したマイクとPC音声のdevice ID、画面変更検知設定、保持方針、LLMエンドポイント、自動文字起こし、ログレベルは `data/settings.json` に保存します。壊れた設定は `data/settings.corrupt-<timestamp>.json` へ退避し、既定値で起動します。保存済みdevice IDが見つからない場合、別デバイスへ自動切替はしません。

`audio/manifest.json` には2track共通のmonotonic origin、各trackの推定開始offset、WAV時間、再接続gapを除く稼働時間、duration drift、queue最大使用率、pressure回数、overflow回数を保存します。診断値に基づく音声の自動伸縮や無音挿入は行いません。

スクリーンショットはtempへ書き込んで再decode検証した後にatomic確定します。一時的な画像保存失敗では画面Captureと音声を止めず、baselineを維持して再試行します。異常終了後に残った正常なPNG tempは起動時復旧の対象です。

## 開発時の検証

```console
uv run ruff check .
uv run pytest -q
```

短い録音済みセッションから、長時間STT試験用の反復音声セッションを生成する場合:

```console
uv run python -m summarize_meeting.devtools.benchmark_session --source-session data/meetings/stt-smoke-ja --output-session data/meetings/stt-benchmark-1h --duration-seconds 3600
```

出力先がすでに存在する場合は上書きしません。生成物は`data/`配下へ置き、Gitには含めません。

仮想音声デバイスなどの指定デバイスだけを使い、実音声録音から文字起こしまでを試験する場合:

```console
uv run python -m summarize_meeting.devtools.real_audio_smoke --source-wave data/meetings/stt-smoke-ja/audio/system.wav --microphone "Virtual microphone" --loopback "Monitor source" --speaker "Virtual speaker"
```

指定名は各デバイス一覧で1件に絞れる部分文字列にします。このコマンドは指定した再生先へWAVを流し、録音セッションを`data/meetings/`へ保存します。

録音・文字起こし済みセッションのPhase 2正常系を一括検証する場合:

```console
uv run python -m summarize_meeting.devtools.validate_phase2_session --session "data/meetings/<対象セッション>" --expect-microphone "マイクへ話した確認文" --expect-system "PCで再生した確認文"
```

成功時は終了コード0と`passed: true`を返します。WAVはストリーミング検査するため、長時間セッションでもファイル全体をメモリへ読み込みません。

実機録音の検証手順は [Phase 1 PoC手動検証](../docs/PHASE1_POC_MANUAL_TEST.md) を参照してください。

## 配置方針

完成版はインストーラーを使わず、アプリフォルダをコピーして利用するポータブル構成を予定しています。会議データ、設定、ログはアプリフォルダ内の `data/` に保存します。

## 現在のPoC制約

- Waylandでは共有対象を列挙・永続化せず、録音開始ごとにPortalのOSダイアログで選択します。
- Portalなし、ヘッドレス、SSHのみ、ロック画面、保護コンテンツは画面取得対象外です。
- Windows/X11の複数モニター、DPI、HDR、対象終了時の挙動は実機評価が必要です。
- OSのスリープ／休止状態をまたぐ録音は対象外です。

## ドキュメント

- [依存ライブラリ・外部ツール一覧](../docs/DEPENDENCIES_AND_TOOLS.md)
- [引き継ぎ資料](../docs/CODEX_HANDOFF_MEETING_MINUTES_TOOL.md)
- [Phase 1詳細設計](../docs/PHASE1_DETAILED_DESIGN.md)
- [Phase 2詳細設計](../docs/PHASE2_DETAILED_DESIGN.md)
- [Phase 2 STTスモーク試験](../docs/PHASE2_STT_SMOKE_TEST.md)
- [Phase 2 STT 1時間ベンチマーク](../docs/PHASE2_STT_1H_BENCHMARK.md)
- [Phase 2 実音声スモーク試験](../docs/PHASE2_REAL_AUDIO_SMOKE_TEST.md)
- [Phase 3詳細設計](../docs/PHASE3_DETAILED_DESIGN.md)
- [Phase 3話者分離スモーク試験](../docs/PHASE3_DIARIZATION_SMOKE_TEST.md)
- [Phase 4詳細設計](../docs/PHASE4_DETAILED_DESIGN.md)
- [Phase 4 画面解析スモーク試験](../docs/PHASE4_SCREEN_ANALYSIS_SMOKE_TEST.md)
- [Phase 5詳細設計](../docs/PHASE5_DETAILED_DESIGN.md)
- [Phase 5統合議事録スモーク試験](../docs/PHASE5_MINUTES_SMOKE_TEST.md)
