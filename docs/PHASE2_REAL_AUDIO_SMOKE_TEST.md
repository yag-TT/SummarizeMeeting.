# Phase 2 Windows実音声スモーク試験

## 1. 目的

合成済みWAVを直接workerへ渡す試験だけでなく、Windows音声デバイスからの録音、WAV確定、別プロセス文字起こし、結果保存までを連続して確認する。

周囲音を取得せず再現可能にするため、自動試験ではVB-Audio Virtual Cableだけを使用した。物理マイクと実スピーカーを使うGUI操作は最終手動確認として分離する。

## 2. 実施条件

| 項目 | 内容 |
|---|---|
| 実施日 | 2026-08-08 |
| OS | Windows 11 |
| マイク入力 | CABLE Output (VB-Audio Virtual Cable) |
| PC音声取得 | CABLE Input (VB-Audio Virtual Cable) のloopback |
| 再生先 | CABLE Input (VB-Audio Virtual Cable) |
| 入力 | Windows音声合成による約5.1秒の日本語PCM16 WAV |
| STT | faster-whisper 1.2.1、large-v3-turbo |
| 推論 | CUDA、float16、別プロセス |

入力文:

`来週の金曜日までに、テスト結果を共有します。`

## 3. 初回試験で検出した不具合

Windowsデバイスからの2トラック録音とWAV確定は成功したが、文字起こしが次のエラーで失敗した。

```text
TranscriptionError: microphoneの音声ファイル名が不正です
```

Phase 1の録音manifestはセッション起点の`audio/microphone.wav`を保存する。一方、Phase 2の文字起こしは音声フォルダ起点の`microphone.wav`だけを許可していたため、実録音セッションを入力できなかった。

文字起こし側を修正し、安全な相対パスとして次の両形式を受理するようにした。絶対パス、`..`、これ以外の階層は引き続き拒否する。

- `microphone.wav`
- `audio/microphone.wav`

長時間ベンチマーク用セッション生成ツールにも同じ互換性を追加した。

## 4. 修正後の結果

| 確認項目 | 結果 |
|---|---|
| セッション状態 | `RECORDED` |
| 録音トラック | microphone、system_audioの2件 |
| WAV形式 | 48 kHz、PCM16、2ch |
| WAV時間 | microphone 6.2秒、system_audio 6.0秒 |
| 録音overflow | 両トラック0 |
| 録音queue pressure | 両トラック0 |
| WAV検証 | 両トラック`validated: true` |
| 文字起こしJob | `SUCCEEDED` |
| 推論device | 両トラック`cuda` |
| segment数 | 2件 |
| 認識文 | 両トラックとも入力文と一致 |
| 出力 | `analysis/transcription.json`、`output/transcript.md` |

開始offsetも反映され、PC音声は0.731秒、マイクは0.861秒からの発話として共通時刻順に保存された。

録音時にSoundCard/Media Foundationから`data discontinuity in recording`警告が出る場合があったが、今回の保存結果ではoverflow、queue pressure、アプリ管理のgapは0で、両トラックの全文を認識できた。物理デバイス試験でも同警告と実際の欠落有無を確認する。

## 5. 自動試験の再現手順

指定した再生デバイスへだけWAVを流す。マイク引数には、再生先へ接続された仮想入力を指定する。出力セッションは`data/meetings/`へ保存される。

```powershell
cd summarize-meeting
uv run python -m summarize_meeting.devtools.real_audio_smoke `
  --source-wave data\meetings\stt-smoke-ja\audio\system.wav `
  --microphone "CABLE Output" `
  --loopback "CABLE Input" `
  --speaker "CABLE Input" `
  --title "Phase2 virtual cable end-to-end"
```

## 6. 物理デバイスで残る手動確認

1. `uv run summarize-meeting`でGUIを起動する。
2. 物理マイクと、実際にテスト音声を流すスピーカーのPC音声を選択する。
3. 「録音終了後に自動で文字起こし」をONにする。
4. 録音を開始し、マイクへ既知の文を話し、PCでは別の既知の文を再生する。
5. マイクとPC音声のランプが緑で、メーターが独立して動くことを確認する。
6. 録音を終了し、保存処理に続いて文字起こしが自動実行されることを確認する。
7. Jobが「完了」になり、両方の文、発話元、timestampが出力されることを確認する。
8. `audio/manifest.json`でoverflow、queue pressure、gap、duration driftを確認する。

この手動確認は物理マイクから周囲音を取得するため、ユーザーがテスト音声だけを扱える状態で実行する。
