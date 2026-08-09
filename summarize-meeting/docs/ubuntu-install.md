# Ubuntu 22.04へコピーして使用する

この手順は、Summarize Meetingを別のUbuntu 22.04 x86_64環境、またはUbuntu 22.04を使用するWSL2環境へコピーして導入する場合を対象とします。Windowsで作成した仮想環境はコピーせず、Ubuntu側で作り直します。

## 1. コピー対象

Gitから取得できる場合は、UbuntuのLinuxファイルシステム上（例: `~/apps/summarize-meeting`）へcloneする方法を推奨します。フォルダーを直接コピーする場合、次の生成物はコピーしないでください。

- `.venv/`、`.venv-wsl/`: OSごとに作り直す仮想環境
- `.pytest_cache/`、`.ruff_cache/`、`__pycache__/`: キャッシュ
- `runtime/`: ローカル実行時の生成物

`data/`には設定と録音済み会議、`models/`にはダウンロード済みモデルが入ります。新規環境として始める場合はコピー不要です。過去の会議やモデルを引き継ぐ場合だけコピーしてください。

WSLでは、プロジェクト本体もLinuxファイルシステムへ置くとファイルI/Oが速くなります。Windowsドライブ上で使用する場合でも、仮想環境はセットアップスクリプトが`$HOME/.local/share/uv/venvs/summarize-meeting`へ作成します。

## 2. OSパッケージを導入する

次の操作だけは管理者権限が必要です。

```bash
sudo apt update
sudo apt install -y \
  ca-certificates curl fontconfig fonts-noto-cjk \
  libportaudio2 libpulse0 libegl1 libgl1
```

ネイティブUbuntuのWaylandセッションで画面取得する場合は追加します。WSLgでは不要です。

```bash
sudo apt install -y pipewire xdg-desktop-portal xdg-desktop-portal-gnome
```

X11でQtのXCB pluginが`libxcb-cursor.so.0`不足を報告した場合だけ追加します。

```bash
sudo apt install -y libxcb-cursor0
```

システムの`ffmpeg`パッケージは不要です。

## 3. uvを導入する

`uv --version`が成功する場合、この手順は不要です。未導入の場合は一般ユーザーで公式インストーラーを実行し、新しいshellを開きます。

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
exec "$SHELL" -l
uv --version
```

Python 3.11は`uv`が導入するため、Ubuntuの`python3.11` aptパッケージは不要です。

## 4. アプリとモデルを準備する

コピー先のプロジェクトディレクトリで実行します。通常は話者分離と画面解析のモデルを準備する`all`を使用します。

```bash
cd ~/apps/summarize-meeting
bash scripts/setup_ubuntu.sh --models all
```

選択できるモデル範囲は次のとおりです。

- `all`: 話者分離とOCR。全機能を使用する通常の導入
- `diarization`: 話者分離だけ
- `ocr`: 画面解析だけ
- `none`: モデルをダウンロードしない

依存関係とモデルの取得にはインターネット接続が必要です。`uv.lock`に従って依存関係を固定し、モデルはSHA-256を検証して配置します。

## 5. LLMエンドポイントを設定する

会話要約を使用する場合だけ設定します。`data/settings.json`を作成し、実際のOpenAI互換API URLへ置き換えてください。

```bash
mkdir -p data
nano data/settings.json
```

```json
{
  "schema_version": 1,
  "llm": {
    "base_url": "http://llm-server.local:8081/v1"
  }
}
```

HTTP接続では文字起こし内容が暗号化されません。利用できる場合はHTTPSを使用してください。`SUMMARIZE_MEETING_LLM_URL`環境変数を設定すると`settings.json`より優先されます。エンドポイントを設定しなくてもアプリは起動でき、会話要約だけが「対象なし」になります。

既存の`data/settings.json`をコピーした場合、この作業は不要です。設定ファイルの変更はアプリ再起動後に反映されます。

## 6. 起動する

```bash
bash scripts/run_ubuntu.sh
```

`run_ubuntu.sh`はLinux専用仮想環境を選択してから`uv run summarize-meeting`を実行します。手動で実行する場合は、同じshellで次の環境変数を設定してください。

```bash
export UV_PROJECT_ENVIRONMENT="$HOME/.local/share/uv/venvs/summarize-meeting"
uv run --frozen summarize-meeting
```

## 7. 動作確認

```bash
export UV_PROJECT_ENVIRONMENT="$HOME/.local/share/uv/venvs/summarize-meeting"
uv run python scripts/doctor.py
```

最低限、次を確認します。

- `platform`、`storage`、`japanese-font`が`OK`
- マイクを使用する場合、`audio`に入力デバイスが表示される
- 話者分離がCPUの場合、`speaker-diarization`は`provider=cpu`
- CUDA構成済みの場合、`speaker-diarization`は`provider=cuda`

WSL2でWindowsアプリの画面やWindows側のPC音声を直接取得することはできません。WSLgで見えるLinux GUIと、WSLへ提供された音声デバイスが対象です。

## GPU話者分離を有効にする場合

CPUだけでも起動と全解析が可能です。GPUを使用する場合は、基本セットアップ後に[READMEのCUDA導入手順](../README.md#ubuntuwsl2で話者分離をgpu実行する)を実行してください。

WSL2ではWindows側のNVIDIAドライバーだけを使用します。WSL内へ`cuda-drivers`、`cuda`、`cuda-12-8`、`nvidia-driver-*`を導入してはいけません。`cuda-toolkit-12-8`と`cudnn9-cuda-12`を使用します。

## 更新するとき

ソースと`uv.lock`を更新した後、同じセットアップコマンドを再実行します。既存の`data/`と`models/`はそのまま利用できます。

```bash
bash scripts/setup_ubuntu.sh --models all
```

依存関係を変更したい場合も`pip install`を個別に実行せず、`pyproject.toml`と`uv.lock`を更新してください。
