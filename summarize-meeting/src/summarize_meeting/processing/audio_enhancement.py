from __future__ import annotations

import hashlib
import json
import math
import os
import wave
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

import numpy as np

ProgressCallback = Callable[[int, str], None]
RatioCallback = Callable[[float], None]

MODEL_NAME = "dpdfnet2_48khz_hr.onnx"
MODEL_SHA256 = "0B399F8A58DC4D70D8CD97541F5C39869406145193B957D00A03B66070944928"
OUTPUT_SAMPLE_RATE = 48_000
HIGHPASS_HZ = 80
TARGET_LUFS = -20
TARGET_LRA = 7
TRUE_PEAK_DBFS = -1.5


class AudioEnhancementError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class WaveQuality:
    sample_rate: int
    channels: int
    sample_width: int
    frame_count: int
    duration_seconds: float
    rms_dbfs: float
    peak_dbfs: float
    clipped_samples_percent: float


class AudioEnhancementBackend(Protocol):
    @property
    def model_name(self) -> str: ...

    @property
    def model_sha256(self) -> str: ...

    def enhance(
        self,
        source: Path,
        output: Path,
        *,
        progress_callback: RatioCallback | None = None,
    ) -> None: ...


class SherpaDpdfNetBackend:
    def __init__(self, model_path: Path, *, num_threads: int = 2) -> None:
        if num_threads <= 0:
            raise ValueError("num_threads must be positive")
        self._model_path = model_path.resolve()
        self._num_threads = num_threads

    @property
    def model_name(self) -> str:
        return self._model_path.name

    @property
    def model_sha256(self) -> str:
        return MODEL_SHA256

    def enhance(
        self,
        source: Path,
        output: Path,
        *,
        progress_callback: RatioCallback | None = None,
    ) -> None:
        self._validate_model()
        denoised = output.with_name(f".{output.name}.denoised.tmp.wav")
        _remove_file(denoised)
        try:
            self._denoise(source, denoised, progress_callback=progress_callback)
            self._apply_filters(denoised, output, progress_callback=progress_callback)
        finally:
            _remove_file(denoised)

    def _validate_model(self) -> None:
        if not self._model_path.is_file():
            raise AudioEnhancementError(
                "音声改善モデルがありません: "
                f"{self._model_path} / .\\scripts\\setup-audio-enhancement-model.ps1 "
                "を実行してください"
            )
        digest = hashlib.sha256()
        try:
            with self._model_path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError as exc:
            raise AudioEnhancementError(f"音声改善モデルを読み込めません: {exc}") from exc
        if digest.hexdigest().upper() != MODEL_SHA256:
            raise AudioEnhancementError("音声改善モデルのSHA-256が一致しません")

    def _denoise(
        self,
        source: Path,
        output: Path,
        *,
        progress_callback: RatioCallback | None,
    ) -> None:
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise AudioEnhancementError("sherpa-onnxを読み込めません") from exc

        config = sherpa_onnx.OnlineSpeechDenoiserConfig(
            model=sherpa_onnx.OfflineSpeechDenoiserModelConfig(
                dpdfnet=sherpa_onnx.OfflineSpeechDenoiserDpdfNetModelConfig(
                    model=str(self._model_path)
                ),
                num_threads=self._num_threads,
                debug=False,
                provider="cpu",
            )
        )
        if not config.validate():
            raise AudioEnhancementError("音声改善モデル設定が不正です")
        denoiser = sherpa_onnx.OnlineSpeechDenoiser(config)
        frame_shift = int(denoiser.frame_shift_in_samples)
        if int(denoiser.sample_rate) != OUTPUT_SAMPLE_RATE or frame_shift <= 0:
            raise AudioEnhancementError("音声改善モデルの音声形式が想定外です")

        try:
            source_stream = wave.open(str(source), "rb")  # noqa: SIM115 - closed below
        except (OSError, EOFError, wave.Error) as exc:
            raise AudioEnhancementError(f"マイク音声を開けません: {exc}") from exc
        with source_stream:
            rate = source_stream.getframerate()
            channels = source_stream.getnchannels()
            sample_width = source_stream.getsampwidth()
            total_frames = source_stream.getnframes()
            if rate != OUTPUT_SAMPLE_RATE or channels != 1 or sample_width != 2:
                raise AudioEnhancementError(
                    "音声改善は48 kHz・モノラル・PCM16のマイク音声に対応しています"
                )
            try:
                destination = wave.open(str(output), "wb")  # noqa: SIM115 - closed below
            except (OSError, EOFError, wave.Error) as exc:
                raise AudioEnhancementError(f"処理中WAVを作成できません: {exc}") from exc
            with destination:
                destination.setnchannels(1)
                destination.setsampwidth(2)
                destination.setframerate(OUTPUT_SAMPLE_RATE)
                consumed = 0
                written = 0
                last_percent = -1
                while consumed < total_frames:
                    raw = source_stream.readframes(min(frame_shift, total_frames - consumed))
                    values = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
                    consumed += len(values)
                    if len(values) < frame_shift:
                        values = np.pad(values, (0, frame_shift - len(values)))
                    denoised_audio = denoiser.run(
                        np.ascontiguousarray(values), OUTPUT_SAMPLE_RATE
                    )
                    written += _write_limited_samples(
                        destination,
                        denoised_audio.samples,
                        remaining=total_frames - written,
                    )
                    percent = math.floor(consumed * 70 / max(1, total_frames))
                    if progress_callback is not None and percent != last_percent:
                        progress_callback(percent / 100)
                        last_percent = percent
                flushed = denoiser.flush()
                written += _write_limited_samples(
                    destination,
                    flushed.samples,
                    remaining=total_frames - written,
                )
                if written != total_frames:
                    raise AudioEnhancementError(
                        f"ノイズ除去後の長さが一致しません: {written}/{total_frames}"
                    )

    @staticmethod
    def _apply_filters(
        source: Path,
        output: Path,
        *,
        progress_callback: RatioCallback | None,
    ) -> None:
        try:
            import av

            input_container = av.open(str(source), "r")
            output_container = av.open(str(output), "w", format="wav")
        except Exception as exc:
            raise AudioEnhancementError(f"音声フィルターを開始できません: {exc}") from exc
        try:
            input_stream = input_container.streams.audio[0]
            total_frames = _wave_info(source).frame_count
            output_stream = output_container.add_stream("pcm_s16le", rate=OUTPUT_SAMPLE_RATE)
            output_stream.layout = "mono"
            graph = av.filter.Graph()
            source_filter = graph.add_abuffer(template=input_stream)
            highpass = graph.add("highpass", args=f"f={HIGHPASS_HZ}")
            loudnorm = graph.add(
                "loudnorm",
                args=f"I={TARGET_LUFS}:LRA={TARGET_LRA}:TP={TRUE_PEAK_DBFS}",
            )
            audio_format = graph.add(
                "aformat",
                args=(
                    f"sample_fmts=s16:sample_rates={OUTPUT_SAMPLE_RATE}:"
                    "channel_layouts=mono"
                ),
            )
            sink = graph.add("abuffersink")
            source_filter.link_to(highpass)
            highpass.link_to(loudnorm)
            loudnorm.link_to(audio_format)
            audio_format.link_to(sink)
            graph.configure()
            decoded_frames = 0

            def drain() -> None:
                while True:
                    try:
                        frame = sink.pull()
                    except (BlockingIOError, EOFError):
                        return
                    for packet in output_stream.encode(frame):
                        output_container.mux(packet)

            for frame in input_container.decode(audio=0):
                decoded_frames += frame.samples
                source_filter.push(frame)
                drain()
                if progress_callback is not None:
                    progress_callback(0.7 + 0.29 * min(1.0, decoded_frames / total_frames))
            source_filter.push(None)
            drain()
            for packet in output_stream.encode(None):
                output_container.mux(packet)
        except AudioEnhancementError:
            raise
        except Exception as exc:
            raise AudioEnhancementError(f"音声フィルター処理に失敗しました: {exc}") from exc
        finally:
            input_container.close()
            output_container.close()
        if progress_callback is not None:
            progress_callback(0.99)


class AudioEnhancementService:
    def __init__(self, backend: AudioEnhancementBackend) -> None:
        self._backend = backend

    def run(
        self,
        session_directory: Path,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> Path:
        session_directory = session_directory.resolve()
        source = _resolve_microphone_source(session_directory)
        output = session_directory / "audio" / "microphone.enhanced.wav"
        temporary = output.with_name(f".{output.name}.tmp")
        metadata = session_directory / "analysis" / "audio_enhancement.json"
        _remove_file(temporary)
        self._notify(progress_callback, 0, "マイク音声とモデルを確認しています")
        last_progress = 0

        def on_backend_progress(ratio: float) -> None:
            nonlocal last_progress
            percent = 5 + round(90 * min(1.0, max(0.0, ratio)))
            if percent == last_progress:
                return
            last_progress = percent
            self._notify(
                progress_callback,
                percent,
                "マイク音声のノイズと音量を改善しています",
            )

        try:
            self._backend.enhance(
                source,
                temporary,
                progress_callback=on_backend_progress,
            )
            source_quality = _wave_info(source)
            output_quality = _wave_info(temporary)
            _validate_output(source_quality, output_quality)
            os.replace(temporary, output)
            payload = {
                "schema_version": 1,
                "status": "SUCCEEDED",
                "source_file": source.relative_to(session_directory).as_posix(),
                "output_file": "audio/microphone.enhanced.wav",
                "model": self._backend.model_name,
                "model_sha256": self._backend.model_sha256,
                "processed_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "filters": {
                    "highpass_hz": HIGHPASS_HZ,
                    "target_lufs": TARGET_LUFS,
                    "target_lra": TARGET_LRA,
                    "true_peak_dbfs": TRUE_PEAK_DBFS,
                },
                "source_quality": asdict(source_quality),
                "output_quality": asdict(output_quality),
            }
            _write_json_atomic(metadata, payload)
        except Exception:
            _remove_file(temporary)
            raise
        self._notify(progress_callback, 100, "マイク音声の改善が完了しました")
        return output

    @staticmethod
    def _notify(callback: ProgressCallback | None, percent: int, message: str) -> None:
        if callback is not None:
            callback(min(100, max(0, percent)), message)


def _resolve_microphone_source(session_directory: Path) -> Path:
    manifest_path = session_directory / "audio" / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AudioEnhancementError(f"音声manifestを読み込めません: {exc}") from exc
    tracks = manifest.get("tracks") if isinstance(manifest, dict) else None
    track = tracks.get("microphone") if isinstance(tracks, dict) else None
    value = track.get("file") if isinstance(track, dict) else None
    if not isinstance(value, str) or not value:
        raise AudioEnhancementError("音声manifestにマイク音声がありません")
    relative = Path(value)
    if relative.is_absolute():
        raise AudioEnhancementError("マイク音声ファイル名が不正です")
    if relative.parts == (relative.name,):
        source = session_directory / "audio" / relative
    elif relative.parts == ("audio", relative.name):
        source = session_directory / relative
    else:
        raise AudioEnhancementError("マイク音声ファイル名が不正です")
    if source.resolve().parent != (session_directory / "audio").resolve():
        raise AudioEnhancementError("マイク音声ファイル名が不正です")
    if not source.is_file():
        raise AudioEnhancementError(f"マイク音声ファイルが見つかりません: {source.name}")
    return source


def _wave_info(path: Path) -> WaveQuality:
    try:
        stream = wave.open(str(path), "rb")  # noqa: SIM115 - closed below
    except (OSError, EOFError, wave.Error) as exc:
        raise AudioEnhancementError(f"WAVを検証できません: {path.name}: {exc}") from exc
    with stream:
        sample_rate = stream.getframerate()
        channels = stream.getnchannels()
        sample_width = stream.getsampwidth()
        frame_count = stream.getnframes()
        if sample_width != 2:
            raise AudioEnhancementError(f"PCM16ではないWAVです: {path.name}")
        sum_squares = 0.0
        peak = 0.0
        clipped = 0
        sample_count = 0
        while True:
            raw = stream.readframes(48_000)
            if not raw:
                break
            values = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
            sum_squares += float(np.dot(values, values))
            peak = max(peak, float(np.max(np.abs(values), initial=0.0)))
            clipped += int(np.count_nonzero(np.abs(values) >= 32760 / 32768))
            sample_count += len(values)
    rms = math.sqrt(sum_squares / sample_count) if sample_count else 0.0
    return WaveQuality(
        sample_rate=sample_rate,
        channels=channels,
        sample_width=sample_width,
        frame_count=frame_count,
        duration_seconds=round(frame_count / sample_rate, 6) if sample_rate else 0.0,
        rms_dbfs=round(20 * math.log10(max(rms, 1e-12)), 3),
        peak_dbfs=round(20 * math.log10(max(peak, 1e-12)), 3),
        clipped_samples_percent=(
            round(100 * clipped / sample_count, 6) if sample_count else 0.0
        ),
    )


def _validate_output(source: WaveQuality, output: WaveQuality) -> None:
    if output.sample_rate != OUTPUT_SAMPLE_RATE or output.channels != 1 or output.sample_width != 2:
        raise AudioEnhancementError("改善版WAVの音声形式が不正です")
    if abs(output.duration_seconds - source.duration_seconds) > 0.02:
        raise AudioEnhancementError("改善版WAVの長さが原音と一致しません")
    if output.clipped_samples_percent > 0:
        raise AudioEnhancementError("改善版WAVでクリッピングを検出しました")


def _write_limited_samples(destination: wave.Wave_write, values: object, *, remaining: int) -> int:
    if remaining <= 0:
        return 0
    samples = np.asarray(values, dtype=np.float32)[:remaining]
    if not len(samples):
        return 0
    pcm = np.rint(np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
    destination.writeframesraw(pcm.tobytes())
    return len(samples)


def _write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _remove_file(path: Path) -> None:
    with suppress(OSError):
        path.unlink(missing_ok=True)
