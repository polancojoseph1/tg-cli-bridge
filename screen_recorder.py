"""Screen recording module using ffmpeg + avfoundation (macOS).

Provides start/stop controls for headless screen capture.
The AI or user can trigger recordings via /record and /stoprecord commands,
or the AI can start/stop recordings programmatically during tasks.
"""

import logging
import os
import signal
import subprocess
import time

logger = logging.getLogger("bridge.recorder")

_proc: subprocess.Popen | None = None
_output_path: str | None = None
_start_time: float | None = None

MAX_DURATION = 120  # seconds — hard cap to prevent disk fill
FRAMERATE = 15
CRF = 28  # compression quality (lower = better, bigger)


def start(output_dir: str = "/tmp", max_duration: int = MAX_DURATION) -> str | None:
    """Start screen recording. Returns the output file path, or None if already recording."""
    global _proc, _output_path, _start_time

    if is_recording():
        return None

    timestamp = int(time.time())
    mp4_path = os.path.join(output_dir, f"screenrec_{timestamp}.mp4")

    cmd = [
        "ffmpeg",
        "-f", "avfoundation",
        "-framerate", str(FRAMERATE),
        "-capture_cursor", "1",
        "-i", "1:none",          # Capture screen 0, no audio
        "-t", str(max_duration),
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "ultrafast",
        "-crf", str(CRF),
        "-y",
        mp4_path,
    ]

    try:
        _proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _output_path = mp4_path
        _start_time = time.time()
        logger.info("Screen recording started: %s (PID %d)", mp4_path, _proc.pid)
        return mp4_path
    except Exception as e:
        logger.error("Failed to start screen recording: %s", e)
        _proc = None
        _output_path = None
        _start_time = None
        return None


def stop() -> str | None:
    """Stop the current recording. Returns the mp4 path if successful, None otherwise."""
    global _proc, _output_path, _start_time

    if not is_recording():
        return None

    path = _output_path

    # SIGINT tells ffmpeg to finalize the file cleanly
    try:
        _proc.send_signal(signal.SIGINT)
        _proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        _proc.kill()
        _proc.wait()
    except Exception as e:
        logger.error("Error stopping ffmpeg: %s", e)
        try:
            _proc.kill()
            _proc.wait()
        except Exception:
            pass

    elapsed = time.time() - _start_time if _start_time else 0
    logger.info("Screen recording stopped after %.1fs: %s", elapsed, path)

    _proc = None
    _output_path = None
    _start_time = None

    # Verify the file exists and has content
    if path and os.path.isfile(path) and os.path.getsize(path) > 1024:
        return path

    logger.warning("Recording file missing or too small: %s", path)
    return None


def is_recording() -> bool:
    """Check if a recording is currently in progress."""
    return _proc is not None and _proc.poll() is None


def status() -> str:
    """Human-readable recording status."""
    if is_recording():
        elapsed = time.time() - _start_time if _start_time else 0
        return f"Recording in progress ({elapsed:.0f}s, file: {_output_path})"
    return "Not recording"


def get_elapsed() -> float:
    """Seconds elapsed since recording started. 0 if not recording."""
    if is_recording() and _start_time:
        return time.time() - _start_time
    return 0.0
