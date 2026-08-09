# 依存ライブラリ・外部ツール一覧

Summarize MeetingをWindows 11またはUbuntu 22.04で開発・実行するための依存関係をまとめます。Pythonパッケージ、OSパッケージ、推論モデル、llama.cpp、任意のGPUランタイムは導入方法と更新主体が異なります。

## 依存関係の管理元

| 対象 | 管理元 | 用途 |
|---|---|---|
| Pythonの直接依存・開発依存 | `summarize-meeting/pyproject.toml` | 許容するバージョン範囲とOS条件 |
| Pythonの完全な解決結果 | `summarize-meeting/uv.lock` | `uv sync --frozen`が再現する厳密な依存関係 |
| Pythonバージョン | `summarize-meeting/.python-version` | Python 3.11 |
| UbuntuのCI依存 | `summarize-meeting/.github/workflows/ci.yml` | Qtと音声ライブラリをoffscreenテストするための共有ライブラリ |
| 実行環境診断 | `summarize-meeting/scripts/doctor.py` | Portal、音声、モデル、CUDA、書込権限の確認 |
| OCR・話者分離モデル | `summarize-meeting/scripts/setup_models.py` | 固定URL／revisionとSHA-256検証付き取得 |

`uv.lock`を更新しない限り、環境構築には`uv sync --frozen`を使用します。`pip install`で個別に追加するとロック済み環境との差分が生じるため使用しません。

## 最短セットアップ

### Windows 11

WindowsのCPU実行では、OS側にFFmpeg、Tesseract、CUDAを追加する必要はありません。

```powershell
uv python install 3.11
uv sync --frozen
uv run python scripts/setup_models.py all
uv run python scripts/doctor.py
uv run summarize-meeting
```

### Ubuntu 22.04 / WSL2 + WSLg

```bash
sudo apt update
sudo apt install -y fontconfig fonts-noto-cjk libportaudio2 libpulse0 libegl1 libgl1

uv python install 3.11
export UV_PROJECT_ENVIRONMENT="$HOME/.local/share/uv/venvs/summarize-meeting"
uv sync --frozen
uv run python scripts/setup_models.py all
uv run python scripts/doctor.py
uv run summarize-meeting
```

Windows用`.venv`とLinux用仮想環境は互換性がありません。また、Windowsドライブをマウントした`/mnt/c`や`/mnt/d`上の仮想環境は小ファイルI/Oが遅いため、WSLではLinux側ファイルシステムの専用環境を使用します。`libasound2`は`libportaudio2`の依存として導入されます。WSLgはWayland/X11とPulseAudioのserverを提供するため、Linux GUIと音声だけを利用する場合に`pipewire`やデスクトップPortalをUbuntu側へ追加する必要はありません。WindowsデスクトップやWindowsアプリの画面取得はWSLgの対象外です。

### Ubuntu 22.04 GNOME Wayland

ネイティブUbuntuのGNOME Waylandで画面取得も使用する場合は次を追加します。

```bash
sudo apt install -y pipewire xdg-desktop-portal xdg-desktop-portal-gnome
```

X11セッションではQt XCB plugin用に`libxcb-cursor0`も導入します。

```bash
sudo apt install -y libxcb-cursor0
```

`pulseaudio-utils`は必須ではありませんが、`pactl info`を使った音声サーバー名の追加診断を有効にする場合だけ導入します。デスクトップがPipeWireを音声サーバーとして使用し、PulseAudio互換層がまだない場合だけ`pipewire-pulse`も導入します。

## uvとPython

| 項目 | 要件 | 備考 |
|---|---|---|
| Git | 開発時のみ | repositoryの取得と変更管理。配布済みフォルダの実行だけなら不要 |
| uv | ロックファイルを扱える現行版 | 開発確認時はuv 0.11.18を使用 |
| Python | 3.11 | `.python-version`で固定。開発確認時は3.11.15 |
| 仮想環境 | `.venv/` | `uv sync`が作成・更新 |
| ビルドバックエンド | `uv_build>=0.11.18,<0.12.0` | editable installを含むプロジェクトビルド用 |

通常の`uv sync --frozen`はruntimeと`dev`依存を導入します。実行専用環境でテスト・lintツールを省く場合は次を使用できます。

```console
uv sync --frozen --no-dev
```

完全なインストール一覧は手作業で複製せず、`uv.lock`を正とします。現在の環境では次のコマンドで確認できます。

```console
uv tree --frozen
uv export --frozen --no-hashes --format requirements-txt
```

## uvが直接導入するruntimeライブラリ

解決バージョンは現在の`uv.lock`に基づきます。`pyproject.toml`の範囲指定を変更しただけでは環境は更新されず、意図的なlock更新が必要です。

| ライブラリ | 宣言 | lock解決 | 使用箇所 |
|---|---:|---:|---|
| PySide6 | `>=6.11.1` | 6.11.1 | GUI、Qt Multimedia画面・ウィンドウ取得、Portal連携 |
| SoundCard | `>=0.4.6` | 0.4.6 | マイク録音、WASAPI loopback、PulseAudio monitor source |
| sounddevice | `>=0.5.5` | 0.5.5 | SoundCardで物理マイクを開始できない場合の限定フォールバック |
| av | `>=18.0.0` | 18.0.0 | 話者分離用の音声decode。faster-whisperからも使用 |
| NumPy | `>=2.2,<2.4` | 2.3.5 | 音声バッファ、画像フレーム、数値処理 |
| faster-whisper | `>=1.2.1` | 1.2.1 | ローカル文字起こし |
| huggingface-hub | `>=0.34` | 1.27.0 | Whisper・PaddleOCRモデルの取得とローカル配置 |
| ONNX Runtime | `==1.24.4` | 1.24.4 | OCRなどのONNX推論。通常版なのでCPU実行 |
| PaddleOCR | `==3.7.0` | 3.7.0 | PP-OCRv6 mediumによる日本語・英語OCR |
| sherpa-onnx | `==1.13.2` | 1.13.2 | 話者分離推論 |
| sherpa-onnx-core | `==1.13.2` | 1.13.2 | Windows AMD64だけに追加するsherpa-onnx native runtime |
| psutil | `>=7.0` | 7.2.2 | workerと子孫プロセスの共通終了処理 |

### 主な間接依存

`uv sync`は上表だけでなく、各ライブラリが必要とする間接依存も`uv.lock`どおり導入します。

| 機能グループ | 主な間接依存 |
|---|---|
| Qt | `pyside6-addons`、`pyside6-essentials`、`shiboken6` |
| 文字起こし | `ctranslate2`、`tokenizers`、`tqdm`、`PyYAML` |
| OCR/PaddleX | `paddlex`、`opencv-contrib-python`、`Pillow`、`pandas`、`Shapely`、`pyclipper`、`pypdfium2`、`python-bidi`、`pydantic`、`modelscope` |
| ONNX Runtime | `flatbuffers`、`protobuf`、`sympy`、`packaging` |
| 音声 | `cffi`、`pycparser` |
| モデル取得・HTTP | `httpx`、`requests`、`aiohttp`、`certifi`、`filelock`、`fsspec` |

PaddleOCRの依存としてPaddleX、OpenCV、PDF関連ライブラリなども導入されます。本アプリはOCR推論をONNX Runtimeで実行するため、`paddlepaddle`や`onnxruntime-gpu`を直接依存にはしていません。

## 開発・テスト用ライブラリ

| ライブラリ | 宣言 | lock解決 | 用途 |
|---|---:|---:|---|
| pytest | `>=9.1.1` | 9.1.1 | 単体・統合テスト |
| pytest-qt | `>=4.5.0` | 4.5.0 | PySide6 UIとsignalのテスト |
| Ruff | `>=0.16.2` | 0.16.2 | lint、import順、Python 3.11向け静的検査 |

```console
uv run --frozen ruff check .
uv run --frozen pytest -q
```

## OS側のツールとライブラリ

### Ubuntu

| パッケージ／機能 | 必須度 | 使用目的 |
|---|---|---|
| `pipewire` | Waylandで必須 | QtとPortal間の画面ストリーム |
| `xdg-desktop-portal` | Waylandで必須 | OSの画面共有許可・対象選択API |
| `xdg-desktop-portal-gnome` | GNOME Waylandで必須 | GNOME用Portal backend |
| `libpulse0` | 音声で必須 | SoundCardのPulseAudio互換API |
| `libportaudio2` | マイクfallbackで必須 | sounddeviceのPortAudio runtime |
| `libasound2` | 音声runtime（間接導入） | ALSA共有ライブラリ。`libportaudio2`が依存 |
| `fontconfig` | 日本語UIで必須 | Qtからシステムフォントを検索し、診断で日本語font familyを確認 |
| `fonts-noto-cjk` | 日本語UIで必須 | `Noto Sans CJK JP`などの日本語グリフ |
| `libegl1`、`libgl1` | Qt runtime | Qt GUI/Multimediaの描画共有ライブラリ。CIでも導入 |
| `libxcb-cursor0` | X11の場合のみ | Qt XCB cursor plugin。Wayland/WSLgでは不要 |
| `pulseaudio-utils` | 任意 | `pactl`による音声サーバー診断 |
| `pipewire-pulse` | 構成依存 | PipeWireをPulseAudio互換サーバーとして使う場合 |

PortalとPipeWireはパッケージが存在するだけでなく、ログイン中のデスクトップセッションでserviceが動作している必要があります。SSHのみ、ヘッドレス、ロック画面は画面取得対象外です。

システムの`ffmpeg`コマンドは使用しません。PySide6 6.11.1とPyAV 18.0.0のLinux wheel内に、使用するFFmpeg共有ライブラリが含まれます。`gdbus`も使用せず、Portal診断は直接依存のPySide6に含まれるQtDBusで行います。このため`ffmpeg`と`libglib2.0-bin`は標準セットアップへ含めません。

### Windows

| 機能 | 提供元 | 備考 |
|---|---|---|
| WASAPI | Windows | SoundCardがマイクとloopbackに使用 |
| Qt Multimedia | PySide6 wheel | 画面・ウィンドウ取得。WinRT/WGCやMSSは使用しない |
| PortAudio | sounddevice wheel | SoundCardで物理マイクを開始できない場合だけ使用 |
| PowerShell | Windows | 手動セットアップ例で利用可能だが、モデル準備やアプリ実行の必須実装ではない |

Tesseract、MSS、WinRT Python package、独自CUDA DLL archive、`taskkill`は不要です。
通常利用する依存はwheelで配布されるため、Visual Studio Build ToolsやGCCを標準セットアップの必須条件にはしていません。

## uvでは導入しない推論モデル

モデルはサイズが大きく、Python wheelとは更新・検証方法が異なるため`uv sync`では取得しません。

| 機能 | モデル | 配置先 | 取得方法 |
|---|---|---|---|
| 文字起こし | faster-whisper `large-v3-turbo` | `models/faster-whisper/` | 初回文字起こし時に取得 |
| 話者分離 | pyannote segmentation 3.0 int8、NeMo TitaNet small | `models/sherpa-onnx/diarization/` | `setup_models.py diarization` |
| OCR検出 | `PP-OCRv6_medium_det` ONNX | `models/paddleocr/PP-OCRv6_medium_det/` | `setup_models.py ocr` |
| OCR認識 | `PP-OCRv6_medium_rec` ONNX | `models/paddleocr/PP-OCRv6_medium_rec/` | `setup_models.py ocr` |

話者分離は固定URL、OCRは固定Hugging Face revisionから取得し、必要なONNXファイルをSHA-256で検証します。全モデルをまとめて準備するコマンドは次のとおりです。

```console
uv run python scripts/setup_models.py all
```

`--force`を付けると既存モデルを再取得・再検証します。

```console
uv run python scripts/setup_models.py all --force
```

## llama.cpp server

llama.cppとGGUFモデルは`uv`や`setup_models.py`では導入しません。会話要約の生成には、同一PCまたはLAN内の別PCでOpenAI互換APIを公開する`llama-server`が必要です。

```console
llama-server --host 0.0.0.0 --port 8081 --model <model.gguf> --ctx-size 16384
```

アプリの既定接続先は`http://192.168.1.158:8081/v1`です。変更する場合は次の環境変数を使用します。

| 環境変数 | 用途 |
|---|---|
| `SUMMARIZE_MEETING_LLM_URL` | OpenAI互換APIのbase URL |
| `SUMMARIZE_MEETING_LLM_MODEL` | `/v1/models`に複数モデルがある場合のmodel ID |

HTTP接続も許可していますが、文字起こし内容は暗号化されません。llama.cppが利用できない場合でも録音、文字起こし、話者分離、画面解析は実行でき、会話要約だけが利用できません。

## 任意のNVIDIA GPU

CPUだけで全機能を実行できます。GPUを使用するのはfaster-whisper/CTranslate2による文字起こしだけです。

- OSへ公式NVIDIA driver、CUDA 12、cuDNN 9を導入する。
- `ctranslate2.get_cuda_device_count()`が1台以上を返した場合だけCUDAを選択する。
- GPUでは`float16`、CPUでは`int8`を使用する。
- CUDA初期化や推論に失敗した場合はCPUへ自動フォールバックする。
- OCRは通常の`onnxruntime`を使うためCPU実行である。
- CUDA runtimeや非公式DLL archiveをアプリへ同梱しない。

## ネットワーク接続が必要なタイミング

| 操作 | 接続先 | オフライン時 |
|---|---|---|
| `uv sync` | Python package index / uv cache | cacheがなければ失敗 |
| 初回文字起こし | Hugging Face | Whisperモデルが配置済みなら不要 |
| `setup_models.py ocr` | Hugging Face | 検証済みOCRモデルが配置済みなら不要 |
| `setup_models.py diarization` | GitHub Releases | 話者分離モデルが配置済みなら不要 |
| 会話要約 | 設定されたllama.cpp server | 接続できない場合は会話要約だけ失敗 |

録音データ、OCR画像、文字起こし内容をクラウド推論サービスへ送信する実装はありません。LAN上のllama.cppを使用する場合だけ、会話要約に必要な内容をそのserverへ送信します。

## 診断とトラブルシュート

```console
uv run python scripts/doctor.py
```

診断対象は次のとおりです。

- Windows/Linux、Python 3.11以上
- `data/`への書込権限
- Wayland/X11とdisplayの有無
- ネイティブWaylandではPipeWire、Portal package、Portal API
- WSLgではWayland/PulseAudio接続と画面取得の制約
- Ubuntuパッケージの導入状態
- SoundCardから見えるマイクとloopback
- PulseAudio互換サーバー名（`pactl`がある場合）
- PaddleOCRモデルのSHA-256
- CTranslate2から見えるCUDA device数

依存環境を更新した後は、次の順番で確認します。

```console
uv sync --frozen
uv run python scripts/doctor.py
uv run --frozen ruff check .
uv run --frozen pytest -q
```

## 依存関係を更新する場合

1. `pyproject.toml`の直接依存またはversion範囲を変更する。
2. `uv lock`で`uv.lock`を更新する。
3. Windows 11とUbuntu 22.04で`uv sync --frozen`を確認する。
4. `doctor.py`、Ruff、全pytestを実行する。
5. OSパッケージやモデル要件が変わった場合は、この資料、README、CI、診断コードを同時に更新する。

特定パッケージだけを更新する場合は、影響範囲を限定するため次の形式を使用します。

```console
uv lock --upgrade-package <package-name>
```
