#!/usr/bin/env python3
"""Native screen recorder for OpenScreen on Linux (X11), driven like the Windows WGC helper.

Chromium on X11 always composites the pointer into desktop capture — ``cursor: "never"`` is
ignored on both getUserMedia and getDisplayMedia — so a browser-captured Linux recording can never
use OpenScreen's editable cursor overlay. This helper records with ffmpeg's ``x11grab`` instead,
which can leave the pointer out (``-draw_mouse 0``); the X11 cursor sampler records what the
editor needs to draw its own.

Protocol (identical to the Windows helper, so the same main-process handlers drive it):
  argv[1]  JSON config (outputPath, sourceType, windowHandle, fps, display bounds, audio flags…)
  stdout   "Recording started" once frames are flowing,
           "Recording stopped. Output path: <path>" when the file is final
  stdin    "pause" | "resume" | "stop", one per line

ffmpeg cannot pause, so each pause ends a segment and each resume starts the next; "stop" joins
the segments losslessly (concat demuxer, stream copy) into the requested .mp4.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

START_TIMEOUT_S = 12.0
STOP_TIMEOUT_S = 15.0


def say(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def fail(message: str, code: int = 1) -> int:
    sys.stderr.write(f"openscreen-linux-capture: {message}\n")
    sys.stderr.flush()
    return code


def even(value: float) -> int:
    return max(2, int(round(value)) // 2 * 2)


def build_ffmpeg_args(config: dict, segment_path: str) -> list[str]:
    """The ffmpeg command for one segment. Pure, so it is unit-testable."""
    display = os.environ.get("DISPLAY", ":0")
    fps = int(config.get("fps") or 30)
    draw_mouse = "1" if config.get("captureCursor") else "0"
    args = ["ffmpeg", "-hide_banner", "-loglevel", "info", "-y"]

    # Video: a window by XID, or a display rectangle in physical pixels (Electron bounds are in
    # DIPs; on X11 one scale factor applies to the whole screen).
    args += ["-thread_queue_size", "1024", "-f", "x11grab", "-draw_mouse", draw_mouse,
             "-framerate", str(fps)]
    window = str(config.get("windowHandle") or "").strip()
    if config.get("sourceType") == "window" and window.isdigit():
        args += ["-window_id", window, "-i", display]
    else:
        scale = float(config.get("displayScaleFactor") or 1.0)
        x = int(round(float(config.get("displayX") or 0) * scale))
        y = int(round(float(config.get("displayY") or 0) * scale))
        w = even(float(config.get("displayW") or 1920) * scale)
        h = even(float(config.get("displayH") or 1080) * scale)
        args += ["-video_size", f"{w}x{h}", "-i", f"{display}+{x},{y}"]

    # Audio: the default sink's monitor (what you hear) and the default source (the mic).
    audio_inputs: list[str] = []
    if config.get("captureSystemAudio"):
        audio_inputs.append("@DEFAULT_MONITOR@")
    if config.get("captureMic"):
        audio_inputs.append("@DEFAULT_SOURCE@")
    for device in audio_inputs:
        args += ["-thread_queue_size", "1024", "-f", "pulse", "-i", device]

    # Keep the frame inside the requested output size, even dimensions for yuv420p.
    max_w = int(config.get("videoWidth") or 0)
    max_h = int(config.get("videoHeight") or 0)
    if max_w > 0 and max_h > 0:
        vf = (f"scale='min({max_w},iw)':'min({max_h},ih)':force_original_aspect_ratio=decrease,"
              "scale=trunc(iw/2)*2:trunc(ih/2)*2")
    else:
        vf = "scale=trunc(iw/2)*2:trunc(ih/2)*2"

    # One filter graph for every case: video always scaled; audio mixed (both), gained (mic
    # only), or passed through (system only). Input 0 is the screen; audio inputs follow.
    mic_gain = float(config.get("microphoneGain") or 1.0)
    graph = [f"[0:v]{vf}[v]"]
    if len(audio_inputs) == 2:
        graph.append(f"[2:a]volume={mic_gain}[mic]")
        graph.append("[1:a][mic]amix=inputs=2:duration=longest:normalize=0[a]")
    elif audio_inputs == ["@DEFAULT_SOURCE@"]:
        graph.append(f"[1:a]volume={mic_gain}[a]")
    elif audio_inputs:
        graph.append("[1:a]anull[a]")
    args += ["-filter_complex", ";".join(graph), "-map", "[v]"]
    if audio_inputs:
        args += ["-map", "[a]"]

    args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
             "-g", str(fps * 2)]
    if audio_inputs:
        args += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    args += [segment_path]
    return args


class Segment:
    def __init__(self, config: dict, path: str) -> None:
        self.path = path
        self.log: list[str] = []
        self.started = threading.Event()
        self.proc = subprocess.Popen(
            build_ffmpeg_args(config, path),
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self) -> None:
        assert self.proc.stderr is not None
        buf = b""
        while True:
            chunk = self.proc.stderr.read1(4096) if hasattr(self.proc.stderr, "read1") else self.proc.stderr.read(4096)
            if not chunk:
                return
            buf += chunk
            text = buf.decode("utf-8", "replace")
            if "frame=" in text or "Press [q]" in text:
                self.started.set()
            # keep the tail only — enough for an error message
            lines = text.replace("\r", "\n").splitlines()
            self.log = lines[-40:]
            buf = buf[-8192:]

    def wait_started(self) -> None:
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.started.is_set():
                return
            if self.proc.poll() is not None:
                raise RuntimeError("ffmpeg exited before recording started:\n" + "\n".join(self.log[-12:]))
            time.sleep(0.02)
        raise RuntimeError("timed out waiting for ffmpeg to start recording")

    def finish(self) -> None:
        """Stop gracefully ('q') so the container is finalized; kill only as a last resort."""
        if self.proc.poll() is None:
            try:
                assert self.proc.stdin is not None
                self.proc.stdin.write(b"q")
                self.proc.stdin.flush()
                self.proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            try:
                self.proc.wait(timeout=STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()


def join_segments(segments: list[str], output: str) -> None:
    usable = [s for s in segments if os.path.exists(s) and os.path.getsize(s) > 0]
    if not usable:
        raise RuntimeError("no video was recorded")
    list_path = output + ".segments.txt"
    with open(list_path, "w", encoding="utf-8") as fh:
        for seg in usable:
            fh.write("file '" + seg.replace("'", "'\\''") + "'\n")
    try:
        done = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
             "-i", list_path, "-c", "copy", "-movflags", "+faststart", output],
            capture_output=True, text=True,
        )
        if done.returncode != 0:
            raise RuntimeError("joining the recording failed: " + done.stderr.strip()[-600:])
    finally:
        for path in [list_path, *segments]:
            try:
                os.remove(path)
            except OSError:
                pass


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return fail("missing JSON config argument")
    try:
        config = json.loads(argv[1])
    except json.JSONDecodeError as exc:
        return fail(f"bad config: {exc}")
    if shutil.which("ffmpeg") is None:
        return fail("ffmpeg is not installed")
    output = str(config.get("outputPath") or "").strip()
    if not output:
        return fail("config has no outputPath")
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    stem = os.path.splitext(output)[0]

    segments: list[str] = []
    current: Segment | None = None

    def start_segment() -> Segment:
        path = f"{stem}.part{len(segments)}.mkv"
        segments.append(path)
        seg = Segment(config, path)
        seg.wait_started()
        return seg

    try:
        current = start_segment()
    except Exception as exc:  # noqa: BLE001
        return fail(str(exc))
    say("Recording started")

    for raw in sys.stdin:
        command = raw.strip().lower()
        if command == "pause" and current is not None:
            current.finish()
            current = None
        elif command == "resume" and current is None:
            try:
                current = start_segment()
            except Exception as exc:  # noqa: BLE001
                return fail(str(exc))
        elif command == "stop":
            break
    if current is not None:
        current.finish()

    try:
        join_segments(segments, output)
    except Exception as exc:  # noqa: BLE001
        return fail(str(exc))
    say(f"Recording stopped. Output path: {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
