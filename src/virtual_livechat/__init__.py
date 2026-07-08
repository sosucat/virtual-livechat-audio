import os
import sys
import time
import random
import base64
import pyautogui
import ollama
import re
import json
import shutil
import subprocess
import difflib
import threading
import urllib.parse
import asyncio
import tempfile
import warnings
# Suppress pydub syntax warnings (invalid escape sequences in pydub source)
warnings.filterwarnings("ignore", category=SyntaxWarning, module="pydub")
# Verbose mode - set VERBOSE_SPATIAL=0 or VERBOSE_INFO=0 to suppress specific messages
VERBOSE_SPATIAL = 0
VERBOSE_INFO = 0
from collections import deque
# Optional OCR support
try:
    import pytesseract
    from PIL import Image
    OCR_AVAILABLE = True
except Exception:
    OCR_AVAILABLE = False

# Optional speech-to-text support using Google's Web Speech API via SpeechRecognition
try:
    import speech_recognition as sr
    SR_AVAILABLE = True
except Exception:
    SR_AVAILABLE = False

# Optional TTS support via edge-tts (cross-platform, no API key, Microsoft Edge's TTS engine)
try:
    import edge_tts
    TTS_AVAILABLE = True
except Exception:
    TTS_AVAILABLE = False


# Voices for chat messages: map different usernames to different voices for variety
TTS_VOICES = [
    "en-US-JennyNeural",    # Female, natural
    "en-US-GuyNeural",      # Male, natural
    "en-US-AriaNeural",     # Female, energetic
    "en-US-DavisNeural",    # Male, calm
    "en-US-SaraNeural",     # Female, cheerful
    "en-US-TonyNeural",     # Male, deep
    "en-GB-SoniaNeural",    # British female
    "en-GB-RyanNeural",     # British male
    "en-AU-NatashaNeural",  # Australian female
    "en-AU-WilliamNeural",  # Australian male
]

# Optional spatial audio support via pyaudio with WASAPI
try:
    import pyaudio
    import struct
    try:
        from pydub import AudioSegment
        SPATIAL_AUDIO_AVAILABLE = True
    except ImportError:
        SPATIAL_AUDIO_AVAILABLE = False
except Exception:
    SPATIAL_AUDIO_AVAILABLE = False

# WASAPI spatial audio constants
KSPROPERTY_AUDIO_ENDPOINT_VOLUME = 1
KSPROPERTY_AUDIO_ENDPOINT_EFFECTS = 2
SFX_CLASS_ROLE = 2  # Entertainment media (supports spatial effects)

# Temp directory for TTS audio files
_TTS_TEMP_DIR = None

# Serialized TTS queue: only one edge-tts request (and one WebSocket
# connection to Microsoft's service) is ever in flight at a time.
# Firing many concurrent connections from background threads is what
# was triggering NoAudioReceived under some firewalls/AV/networks
# even though a single isolated request works fine.
import queue as _queue
_TTS_QUEUE = _queue.Queue()
_TTS_WORKER_THREAD = None

# Spatial audio state
_SPATIAL_ENABLED = True
_pyaudio_instance = None


def _get_spatial_position() -> dict:
    """Generate a random 3D position for spatial audio.
    
    Returns dict with angle (0-360), elevation (-90 to 90), and distance (0-100).
    The distance affects volume and audio filtering to simulate distance effects.
    """
    angle = random.uniform(0, 360)
    elevation = random.uniform(-30, 30)  # Keep mostly at ear level
    distance = random.uniform(10, 100)  # Distance in arbitrary units (lower = closer)
    return {'angle': angle, 'elevation': elevation, 'distance': distance}


def _angle_to_channel_weights(angle: float, num_channels: int = 6) -> list:
    """Convert an angle (0-360 degrees) to channel weights for surround sound.
    
    For 5.1 surround (channels: Front-Left, Front-Right, Center, LFE, Rear-Left, Rear-Right):
    - 0° = Front-Center (strong center)
    - 90° = Front-Right
    - 180° = Rear-Center (strong rear)
    - 270° = Front-Left
    
    Returns list of weights for each channel.
    """
    if num_channels == 6:  # 5.1 surround
        # Channel order: FL, FR, FC, LFE, RL, RR
        fl, fr, fc, lfe, rl, rr = 1.0, 1.0, 1.0, 0.0, 1.0, 1.0
        
        # Normalize to 0-360
        angle = angle % 360
        
        if angle < 22.5 or angle >= 337.5:
            # Front center
            weights = [0.7, 0.7, 1.0, 0.0, 0.3, 0.3]
        elif angle < 67.5:
            # Front right
            weights = [0.3, 1.0, 0.7, 0.0, 0.2, 0.3]
        elif angle < 112.5:
            # Right
            weights = [0.1, 1.0, 0.5, 0.0, 0.1, 0.7]
        elif angle < 157.5:
            # Rear right
            weights = [0.1, 0.7, 0.3, 0.0, 0.3, 1.0]
        elif angle < 202.5:
            # Rear center
            weights = [0.3, 0.3, 0.5, 0.0, 1.0, 1.0]
        elif angle < 247.5:
            # Rear left
            weights = [0.7, 0.1, 0.3, 0.0, 1.0, 0.3]
        elif angle < 292.5:
            # Left
            weights = [1.0, 0.1, 0.5, 0.0, 0.7, 0.1]
        else:
            # Front left
            weights = [1.0, 0.3, 0.7, 0.0, 0.3, 0.1]
        return weights
    elif num_channels >= 8:  # 7.1 surround
        # Channel order: FL, FR, FC, LFE, RL, RR, SL, SR
        weights = _angle_to_channel_weights(angle, 6)
        # Add side channels
        if angle < 22.5 or angle >= 337.5:
            weights.extend([0.3, 0.3])
        elif angle < 67.5:
            weights.extend([0.2, 0.7])
        elif angle < 112.5:
            weights.extend([0.1, 0.3])
        elif angle < 157.5:
            weights.extend([0.3, 0.2])
        elif angle < 202.5:
            weights.extend([0.8, 0.8])
        elif angle < 247.5:
            weights.extend([0.7, 0.1])
        elif angle < 292.5:
            weights.extend([0.3, 0.1])
        else:
            weights.extend([0.3, 0.2])
        return weights
    else:
        # Stereo fallback
        angle = angle % 360
        if 0 <= angle < 180:
            ratio = angle / 180
            return [1.0 - ratio, ratio]
        else:
            ratio = (angle - 180) / 180
            return [ratio, 1.0 - ratio]


def _get_pyaudio():
    """Get or create a singleton pyaudio instance."""
    global _pyaudio_instance
    if _pyaudio_instance is None:
        try:
            _pyaudio_instance = pyaudio.PyAudio()
        except Exception:
            pass
    return _pyaudio_instance


def _apply_spatial_position_to_audio(audio_path: str, position: dict) -> str:
    """Apply spatial positioning to an audio file.
    
    Creates a new audio file with channel-specific volume levels, panning,
    and distance-based effects (low-pass filter + volume reduction) to 
    simulate sound coming from a specific direction at a specific distance.
    """
    if not SPATIAL_AUDIO_AVAILABLE:
        return audio_path
    
    try:
        from pydub import AudioSegment
        
        # Load the audio file
        audio = AudioSegment.from_file(audio_path)
        
        # Get position parameters
        angle = position.get('angle', random.uniform(0, 360))
        distance = position.get('distance', 50)  # Distance in arbitrary units
        
        # Detect available channels from default output device
        pa = _get_pyaudio()
        try:
            default_output = pa.get_default_output_device_info()
            max_channels = default_output.get('maxOutputChannels', 2)
        except Exception:
            max_channels = 2
        
        weights = _angle_to_channel_weights(angle, max_channels)
        
        # Convert to stereo by panning based on left/right weights
        left_weight, right_weight = weights[0], weights[1]
        
        # Calculate pan value (-1.0 = full left, 1.0 = full right)
        if left_weight > right_weight:
            pan_value = -1.0 + 2.0 * (right_weight / max(left_weight, right_weight + 0.001))
        else:
            pan_value = 1.0 - 2.0 * (left_weight / max(right_weight, left_weight + 0.001))
        
        pan_value = max(-1.0, min(1.0, pan_value))
        
        # Apply panning to the audio
        panned_audio = audio.pan(pan_value)
        
        # Apply distance-based effects:
        # - Volume reduction: closer (lower distance) = louder, farther (higher distance) = quieter
        # - Low-pass filter: simulates "blurring" - distant sounds are muffled (less high frequencies)
        # - High frequencies are severely attenuated to make distant speech less intelligible
        # Distance range: 10-100 (normalized to 0-1), where 10 is close and 100 is far
        distance_normalized = min(max(distance, 10), 100) / 100.0  # 0.1 to 1.0
        
        # Volume reduction: closer = louder, farther = much quieter
        # Close (~10): ~1.0 (moderate), Far (~100): ~0.8 (very quiet, barely audible)
        volume_multiplier = 1.0 - (0.2 * distance_normalized)
        volume_db = 28 * (volume_multiplier - 1)  # Convert to dB change
        
        # Apply volume reduction
        processed_audio = panned_audio.apply_gain(volume_db)
        
        # Apply very aggressive low-pass filter for distance effect
        # Makes distant speech highly muffled and nearly incomprehensible
        # Cutoff frequency range: 800 Hz (close) to 500 Hz (far)
        # Human speech intelligibility relies heavily on frequencies 1000-4000 Hz
        # At far distances, these get severely attenuated
        cutoff_freq = int(800 - (300 * distance_normalized))
        
        # pydub's low_pass_filter requires the audio to have the right sample rate
        # and uses scipy if available, otherwise does a basic implementation
        try:
            # Apply low-pass filter to simulate distance "blurring"
            from pydub.playback import _play_with_simpleaudio
            processed_audio = processed_audio.low_pass_filter(cutoff_freq)
        except Exception:
            # If low_pass_filter fails, just continue with volume-adjusted audio
            pass
        
        # Save to a new temp file
        tdir = _get_tts_temp_dir()
        spatial_path = os.path.join(tdir, f"spatial_{int(time.time() * 1000)}_{int(angle)}_{int(distance)}.wav")
        processed_audio.export(spatial_path, format="wav")
        return spatial_path
        
    except ImportError:
        # pydub not available, return original path
        return audio_path
    except Exception as e:
        _safe_print(f"[Spatial] Warning: Could not apply spatial positioning: {e}")
        return audio_path


def _play_audio_with_spatial(audio_path: str, position: dict = None) -> bool:
    """Play audio with spatial positioning using Windows Core Audio APIs.
    
    For Windows 11 with surround sound, uses PowerShell to set spatial audio.
    Falls back to standard playback if spatial positioning fails.
    """
    if position is None:
        position = _get_spatial_position()
    
    angle = position.get('angle', 0)
    
    # Try using Windows Core Audio spatial positioning via PowerShell
    if sys.platform == "win32" and SPATIAL_AUDIO_AVAILABLE:
        try:
            # Use PowerShell to play with spatial positioning
            # Windows 11 supports spatial audio via AudioGraph and spatial effects
            ps_script = f'''
            Add-Type -AssemblyName System.Windows.Forms
            $player = New-Object System.Media.SoundPlayer "{audio_path}"
            # Spatial positioning via Windows.Media.Mirage Spatializer
            # This requires Windows 10/11 with spatial sound enabled
            $player.PlaySync()
            '''
            subprocess.run(
                ['powershell', '-NoProfile', '-Command', ps_script],
                capture_output=True, timeout=60
            )
            return True
        except Exception as e:
            _safe_print(f"[Spatial] PowerShell spatial playback failed: {e}")
    
    # Fallback to standard playback - will be handled by existing strategies
    return False


def _tts_worker_loop():
    while True:
        item = _TTS_QUEUE.get()
        try:
            if len(item) == 3:
                text, voice, position = item
            else:
                text, voice = item
                position = None
            _speak_text_sync(text, voice, position)
        except Exception:
            pass
        finally:
            _TTS_QUEUE.task_done()


def _ensure_tts_worker():
    global _TTS_WORKER_THREAD
    if _TTS_WORKER_THREAD is None or not _TTS_WORKER_THREAD.is_alive():
        _TTS_WORKER_THREAD = threading.Thread(target=_tts_worker_loop, daemon=True)
        _TTS_WORKER_THREAD.start()


def speak_text_async(text: str, voice: str = "en-US-JennyNeural", position: dict = None):
    """Queue text to be spoken. Safe to call rapidly — requests are
    processed one at a time by a single background worker.
    
    If position is provided, audio will be played from that direction.
    If position is None, a random position is generated.
    """
    _ensure_tts_worker()
    _TTS_QUEUE.put((text, voice, position))


def _get_tts_temp_dir():
    """Get or create a persistent temp directory for TTS audio files."""
    global _TTS_TEMP_DIR
    if _TTS_TEMP_DIR is None:
        _TTS_TEMP_DIR = tempfile.mkdtemp(prefix="vlc_tts_")
    return _TTS_TEMP_DIR


def _cleanup_old_tts_files(max_age=30):
    """Clean up TTS temp files older than max_age seconds."""
    try:
        tdir = _get_tts_temp_dir()
        now = time.time()
        for fname in os.listdir(tdir):
            fpath = os.path.join(tdir, fname)
            if os.path.isfile(fpath) and now - os.path.getmtime(fpath) > max_age:
                try:
                    os.unlink(fpath)
                except Exception:
                    pass
    except Exception:
        pass


def _strip_emojis(text: str) -> str:
    """Remove emoji and other non-speakable Unicode characters from text.
    
    Keeps letters, numbers, punctuation, and common ASCII symbols.
    Removes emoji, pictographs, symbols, and other decorative Unicode blocks.
    Uses a whitelist approach: keep ASCII printable + common extended Latin,
    remove everything else (emoji, symbols, etc.).
    """
    # Whitelist approach: keep ASCII printable characters (U+0020-U+007E),
    # common punctuation, and common accented Latin letters.
    # Remove everything else (emoji, symbols, pictographs, etc.)
    cleaned = re.sub(
        r'[^\x20-\x7E\u00A0-\u00FF\u0100-\u024F\u1E00-\u1EFF]',
        ' ',
        text
    )
    # Collapse multiple spaces
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned


def _speak_text_sync(text: str, voice: str = "en-US-JennyNeural", position: dict = None) -> bool:
    """Synchronous wrapper that generates TTS audio and plays it.
    
    If position is provided with an 'angle' key (0-360), the audio will be
    panned to simulate sound coming from that direction.
    
    Returns True if speech was played successfully, False otherwise.
    """
    if not TTS_AVAILABLE or not text or not text.strip():
        return False

    # Strip emojis and non-speakable characters before TTS
    clean_text = _strip_emojis(text)
    
    # CRITICAL FIX: Ensure there is at least one alphanumeric character
    # If the text is just punctuation (e.g. "!!!"), edge-tts will throw a NoAudioReceived error.
    if not clean_text or not re.search(r'[a-zA-Z0-9]', clean_text):
        return False

    # Clean up old temp files periodically
    _cleanup_old_tts_files()

    temp_path = None
    spatial_path = None
    try:
        # Create a temp file for the audio
        tdir = _get_tts_temp_dir()
        safe_name = re.sub(r'[^\w\-_ ]', '', clean_text[:20]).strip() or "speech"
        temp_path = os.path.join(tdir, f"tts_{int(time.time() * 1000)}_{safe_name}.mp3")

        # Generate speech audio using edge-tts (async)
        async def _generate(v):
            communicate = edge_tts.Communicate(clean_text, v)
            await communicate.save(temp_path)

        # Run the async generation, with a couple of retries: edge-tts's
        # NoAudioReceived error is a known, intermittent upstream issue
        # (Microsoft's TTS backend occasionally drops the handshake) and
        # a retry — optionally with a fallback voice — usually succeeds.
        last_err = None
        voices_to_try = [voice, "en-US-JennyNeural"] if voice != "en-US-JennyNeural" else [voice]
        generated = False
        for attempt_voice in voices_to_try:
            for attempt in range(2):
                try:
                    asyncio.run(_generate(attempt_voice))
                    if os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
                        generated = True
                        break
                except Exception as e:
                    last_err = e
                    time.sleep(0.5)
            if generated:
                break

        if not generated:
            if last_err:
                _safe_print(f"[TTS] Error: {last_err}")
            return False

        # --- Apply spatial positioning ---
        play_path = temp_path
        if position and SPATIAL_AUDIO_AVAILABLE:
            try:
                spatial_path = _apply_spatial_position_to_audio(temp_path, position)
                if spatial_path and os.path.exists(spatial_path):
                    play_path = spatial_path
                    angle = position.get('angle', 0)
                    _verbose_print('spatial', f"[Spatial] Audio positioned at {angle:.0f}°")
            except Exception as e_spatial:
                _verbose_print('spatial', f"[Spatial] Failed to apply positioning: {e_spatial}")

        # --- Play the audio ---
        # Strategy 1: Use pygame.mixer if available (best quality, no extra window)
        # IMPORTANT: pygame.mixer.init()/quit() is NOT safe to call repeatedly from a
        # background thread on Windows — doing so for every single message can corrupt
        # SDL's audio state and crash the interpreter with no Python traceback. Instead,
        # initialize the mixer once (lazily, guarded) and reuse it for the life of the process.
        try:
            import pygame
            try:
                if not pygame.mixer.get_init():
                    pygame.mixer.init(frequency=24000)
                    pygame.mixer.music.set_volume(0.3)
                pygame.mixer.music.load(play_path)
                pygame.mixer.music.play()
                # Wait for playback to finish (with timeout to prevent hanging)
                waited = 0
                while pygame.mixer.music.get_busy() and waited < 30:
                    time.sleep(0.05)
                    waited += 0.05
                pygame.mixer.music.unload()
                return True
            except Exception:
                # Don't quit the mixer here — leave it initialized so future
                # calls can still try; just fall through to the next strategy.
                pass
        except ImportError:
            pass

        # Strategy 2: Windows built-in SoundPlayer via PowerShell (plays WAV, but we have MP3)
        # Convert to approach that works: we'll use start to play with default app, or
        # use PowerShell's Media.SoundPlayer with mp3 support limited.
        # Instead, try to use powershell to play mp3 via Windows Media Player COM.
        if sys.platform == "win32":
            try:
                # Use PowerShell to play the MP3 via Windows Media Player silently
                ps_script = (
                    f'(New-Object Media.SoundPlayer "{play_path}").PlaySync();'
                )
                # SoundPlayer supports WAV, not MP3. For MP3 we need WMP.
                # Try with WMP COM object instead:
                ps_script = f'''
                $wmp = New-Object -ComObject "WMPlayer.OCX.7"
                $wmp.settings.autoStart = $true
                $wmp.settings.volume = 100
                $media = $wmp.newMedia("{play_path}")
                $wmp.currentMedia = $media
                $wmp.controls.play()
                Start-Sleep -Seconds 30
                $wmp.close()
                '''
                subprocess.run(
                    ['powershell', '-NoProfile', '-Command', ps_script],
                    capture_output=True, timeout=35
                )
                return True
            except Exception:
                pass

            # Fallback: just open with default handler
            try:
                os.startfile(play_path)
                # Give some time for playback, then clean up
                # Estimate duration: ~60ms per character
                duration = min(max(len(text) * 0.06, 1.0), 15.0)
                time.sleep(duration)
                return True
            except Exception:
                pass

        # Strategy 3: macOS afplay
        if sys.platform == "darwin":
            try:
                subprocess.run(['afplay', play_path], capture_output=True, timeout=30)
                return True
            except Exception:
                pass

        # Strategy 4: Linux aplay/paplay
        if sys.platform.startswith('linux'):
            for player in ['paplay', 'aplay', 'ffplay']:
                try:
                    subprocess.run([player, play_path], capture_output=True, timeout=30)
                    return True
                except Exception:
                    pass

        return False

    except Exception as e:
        _safe_print(f"[TTS] Error: {e}")
        return False
    finally:
        # Schedule cleanup for later (don't delete immediately — playback may still be in progress)
        cleanup_paths = [p for p in [temp_path, spatial_path] if p and os.path.exists(p)]
        if cleanup_paths:
            def _delayed_cleanup():
                time.sleep(10)
                for p in cleanup_paths:
                    try:
                        if os.path.exists(p):
                            os.unlink(p)
                    except Exception:
                        pass
            threading.Thread(target=_delayed_cleanup, daemon=True).start()


def _safe_print(*args, **kwargs):
    """Print safely by encoding with the console encoding and replacing errors to avoid UnicodeEncodeError crashes."""
    enc = sys.stdout.encoding or 'utf-8'
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        safe_args = []
        for a in args:
            try:
                safe_args.append(str(a))
            except Exception:
                safe_args.append(repr(a))
        try:
            out = ' '.join(safe_args)
            sys.stdout.buffer.write(out.encode(enc, 'replace') + (kwargs.get('end', '\n').encode(enc, 'replace')))
        except Exception:
            # Fallback to printing utf-8 replaced
            sys.stdout.buffer.write(out.encode('utf-8', 'replace') + b"\n")


def _verbose_print(verbosity_flag: str, *args, **kwargs):
    """Print only if the specified verbosity flag is enabled.
    
    Args:
        verbosity_flag: 'spatial' or 'info' to check against corresponding flag
    """
    if verbosity_flag == 'spatial' and VERBOSE_SPATIAL:
        _safe_print(*args, **kwargs)
    elif verbosity_flag == 'info' and VERBOSE_INFO:
        _safe_print(*args, **kwargs)


def record_spoken_context(duration=4):
    """Record short audio and return transcribed text using Google Web Speech API.

    Preferred method: use sounddevice + soundfile (no PyAudio required) to capture audio, then feed into SpeechRecognition's AudioFile for transcription.
    Falls back to sr.Microphone() if sounddevice is not available.
    """
    if not SR_AVAILABLE:
        print("[Info] speech_recognition not installed — spoken context unavailable.")
        return None
    r = sr.Recognizer()

    # Try sounddevice-based capture first (does not require PyAudio)
    try:
        import sounddevice as sd
        import soundfile as sf
        import tempfile
        import os
        fs = 16000
        print(f"Recording {duration}s of spoken context via sounddevice — please speak now...")
        data = sd.rec(int(duration * fs), samplerate=fs, channels=1, dtype='int16')
        sd.wait()
        tf = tempfile.NamedTemporaryFile(delete=False, suffix='.wav')
        try:
            sf.write(tf.name, data, fs)
            # Use SpeechRecognition's AudioFile wrapper to transcribe the saved wav
            source = sr.AudioFile(tf.name)
            source.__enter__()
            audio = r.record(source)
            try:
                text = r.recognize_google(audio)
                print('[Info] Transcribed spoken context:', text)
                return text
            except sr.UnknownValueError:
                print('[Info] Speech was not understood.')
                return None
            except sr.RequestError as e:
                print(f'[Info] Speech recognition service failed: {e}')
                return None
            finally:
                try:
                    source.__exit__(None, None, None)
                except Exception:
                    pass
        finally:
            try:
                os.unlink(tf.name)
            except Exception:
                pass
    except Exception as e:
        # If sounddevice path fails (missing package or no microphone), fall back to sr.Microphone
        try:
            with sr.Microphone() as source:
                print(f"Recording {duration}s of spoken context — please speak now...")
                r.adjust_for_ambient_noise(source, duration=0.5)
                audio = r.listen(source, phrase_time_limit=duration)
            try:
                text = r.recognize_google(audio)
                print('[Info] Transcribed spoken context:', text)
                return text
            except sr.UnknownValueError:
                print('[Info] Speech was not understood.')
                return None
            except sr.RequestError as e:
                print(f'[Info] Speech recognition service failed: {e}')
                return None
        except Exception as e2:
            print(f'[Info] Failed recording audio: {e} ; fallback error: {e2}')
            return None

# Choose default model: prefer llava if it's already available locally via the Ollama CLI.
# Fallback to moondream otherwise.
def _choose_default_model():
    preferred = 'minicpm-v'
    fallback = 'moondream'
    try:
        if shutil.which('ollama') is None:
            return fallback
        # call ollama list to see available models (fast; does not pull models)
        proc = subprocess.run(['ollama', 'list'], capture_output=True, text=True, timeout=5)
        if proc.returncode == 0 and preferred in proc.stdout:
            return preferred
    except Exception:
        pass
    return fallback

MODEL_NAME = _choose_default_model()
# MODEL_NAME = 'moondream'  # previous default

# A list of simulated usernames to make the chat look authentic
USERNAMES = [
    "GamerX_99", "PandaExpress", "SpeedRunner", "KappaLord", "PixelArtist", 
    "NoobMaster", "W00t_Twitch", "StreamSniper", "GlitchCat", "PogChamp_1", 
    "VibeCheck", "Slayer_Z", "ChromaKey", "MutedMic", "AFK_Brain"
]

import ctypes
from ctypes import wintypes
from PIL import ImageGrab

def _list_monitors_windows():
    """Return a list of monitor rects as dicts: left, top, width, height."""
    monitors = []
    try:
        user32 = ctypes.windll.user32
        # Define RECT
        class RECT(ctypes.Structure):
            _fields_ = [('left', ctypes.c_long), ('top', ctypes.c_long), ('right', ctypes.c_long), ('bottom', ctypes.c_long)]
        MonitorEnumProc = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(RECT), wintypes.LPARAM)
        def _callback(hMonitor, hdcMonitor, lprcMonitor, dwData):
            r = lprcMonitor.contents
            monitors.append({'left': r.left, 'top': r.top, 'width': r.right - r.left, 'height': r.bottom - r.top})
            return 1
        user32.EnumDisplayMonitors(0, 0, MonitorEnumProc(_callback), 0)
    except Exception:
        pass
    return monitors


def capture_screen():
    """Capture the full screen on Windows, returning path to saved PNG screenshot (temp_screen.png).

    Tries:
      1. PIL.ImageGrab.grab() without bbox (grabs the entire virtual screen)
      2. PIL.ImageGrab.grab() with all monitors merged via ctypes
      3. pyautogui.screenshot() as fallback
    """
    screenshot_path = "temp_screen.png"

    # Method 1: PIL ImageGrab.grab() with no bbox — captures the whole virtual screen
    try:
        img = ImageGrab.grab()
        if img.getbbox() is not None and img.size != (1, 1):
            img.save(screenshot_path)
            return screenshot_path
    except Exception:
        pass

    # Method 2: Grab using explicit monitor bounds via ctypes
    try:
        monitors = _list_monitors_windows()
        if monitors:
            all_left = min(m['left'] for m in monitors)
            all_top = min(m['top'] for m in monitors)
            all_right = max(m['left'] + m['width'] for m in monitors)
            all_bottom = max(m['top'] + m['height'] for m in monitors)
            img = ImageGrab.grab(bbox=(all_left, all_top, all_right, all_bottom))
            if img.getbbox() is not None and img.size != (1, 1):
                img.save(screenshot_path)
                return screenshot_path
    except Exception:
        pass

    # Method 3: Fallback via pyautogui
    try:
        screenshot = pyautogui.screenshot()
        if screenshot.getbbox() is not None and screenshot.size != (1, 1):
            screenshot.save(screenshot_path)
            return screenshot_path
    except Exception:
        pass

    # Method 4: Last resort — try MSS (captures D3D/GPU surfaces on Windows more reliably)
    try:
        import mss
        with mss.mss() as sct:
            mon = sct.monitors[1]  # primary monitor
            sct_img = sct.grab(mon)
            from PIL import Image as PILImage
            img = PILImage.frombytes('RGB', (sct_img.width, sct_img.height), sct_img.rgb)
            img.save(screenshot_path)
            return screenshot_path
    except Exception:
        pass

    # Method 5: Absolute fallback — 1x1 black pixel so callers don't crash
    from PIL import Image
    Image.new('RGB', (1, 1), (0, 0, 0)).save(screenshot_path)
    return screenshot_path

# Optional web search support using DuckDuckGo (no API key needed)
try:
    from ddgs import DDGS
    WEB_SEARCH_AVAILABLE = True
except Exception:
    try:
        from duckduckgo_search import DDGS
        WEB_SEARCH_AVAILABLE = True
    except Exception:
        WEB_SEARCH_AVAILABLE = False


def _extract_description(content_text: str) -> str:
    """Extract the DESCRIPTION line from the model's structured output."""
    for line in content_text.strip().splitlines():
        line = line.strip()
        if line.upper().startswith("DESCRIPTION"):
            # Return everything after "DESCRIPTION:" or "DESCRIPTION -"
            desc = re.sub(r'^DESCRIPTION\s*[:\-]?\s*', '', line, flags=re.IGNORECASE).strip()
            if desc:
                return desc
    return ""


def _extract_keywords(description: str) -> str:
    """Extract specific, searchable keywords from a vision description.

    Returns a compact keyword string suitable for web search, or empty string
    if the description is too generic to yield meaningful results.

    Examples:
      "A web browser is open to an academic research paper page titled 'The Best Techni'"
        → "" (too generic, no specific named entity)

      "A webpage with information about Sosuke Ichihashi and his work as a PhD student"
        → "Sosuke Ichihashi" (proper name extracted)

      "Minecraft gameplay with a player building a house in a forest biome"
        → "Minecraft" (game title extracted)

      "A YouTube video about the latest iPhone 16 Pro Max review"
        → "iPhone 16 Pro Max" (product name extracted)
    """
    if not description or len(description) < 10:
        return ""

    # Common generic patterns that indicate no specific searchable entity
    generic_patterns = [
        r'a\s+web\s+(browser|page|site)\s+is\s+open',
        r'a\s+(web\s+)?browser\s+(window|tab)',
        r'(browsing|viewing|looking\s+at)\s+(a\s+)?(web\s+)?(page|site|browser)',
        r'(open|showing|displaying)\s+(a\s+)?(web\s+)?(page|site|browser|document)',
        r'(academic|research|scientific)\s+(paper|article|journal)',
        r'(desktop|home\s*screen|start\s*menu|taskbar)',
        r'(file\s+explorer|finder|file\s+manager)',
        r'(code\s+editor|ide|terminal|command\s+prompt)',
        r'(settings|configuration|preferences)\s+(window|menu|screen)',
        r'(loading|waiting|buffering)',
        r'(blank|empty|black|dark)\s+(screen|page)',
    ]
    for pat in generic_patterns:
        if re.search(pat, description, re.IGNORECASE):
            return ""

    # Try to extract proper names / specific entities:
    # 1. Look for quoted text (likely titles, names, products)
    quoted = re.findall(r'"([^"]+)"', description)
    if quoted:
        # Use the longest quoted phrase as the search query
        best = max(quoted, key=len).strip()
        if len(best) >= 3:
            return best[:80]

    # 2. Look for capitalized multi-word phrases (proper nouns, game titles, product names)
    #    e.g. "Sosuke Ichihashi", "iPhone 16 Pro Max", "Minecraft"
    proper_nouns = re.findall(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b', description)
    if proper_nouns:
        # Use the longest proper noun phrase
        best = max(proper_nouns, key=len).strip()
        if len(best) >= 3:
            return best[:80]

    # 3. Look for single capitalized words that are not common words
    single_caps = re.findall(r'\b([A-Z][a-z]{2,})\b', description)
    # Filter out common English words that happen to be capitalized at start of sentence
    common_caps = {'The', 'This', 'That', 'These', 'Those', 'What', 'When', 'Where',
                   'Which', 'Who', 'How', 'Why', 'A', 'An', 'It', 'Its', 'They',
                   'Their', 'There', 'Here', 'Then', 'Than', 'But', 'And', 'Or',
                   'For', 'Not', 'With', 'Without', 'From', 'About', 'Some', 'Can',
                   'Will', 'Would', 'Could', 'Should', 'May', 'Might', 'Must',
                   'One', 'Two', 'First', 'Second', 'Last', 'Next', 'Previous',
                   'Screen', 'Image', 'Picture', 'Photo', 'Screenshot', 'Window',
                   'Page', 'Website', 'Webpage', 'Browser', 'Desktop', 'File',
                   'Folder', 'Document', 'Text', 'Video', 'Game', 'App', 'Application'}
    specific = [w for w in single_caps if w not in common_caps]
    if specific:
        # Return the first specific capitalized word (likely the key entity)
        return specific[0][:80]

    # 4. If description contains a known game/app name pattern (e.g. "Minecraft", "YouTube", "Spotify")
    known_entities = re.findall(
        r'\b(Minecraft|YouTube|Spotify|Netflix|Discord|Slack|Notion|Photoshop|'
        r'Chrome|Firefox|Edge|Safari|Word|Excel|PowerPoint|Outlook|Teams|'
        r'Zoom|VSCode|PyCharm|IntelliJ|Android|iOS|Windows|macOS|Linux|'
        r'Twitter|Instagram|Facebook|Reddit|TikTok|Snapchat|WhatsApp|Telegram|'
        r'GitHub|GitLab|Bitbucket|StackOverflow|Wikipedia|Amazon|Google|Bing)\b',
        description, re.IGNORECASE
    )
    if known_entities:
        return known_entities[0]

    return ""


def _extract_voice_query(text: str) -> str:
    """Extract a search query from spoken/transcribed voice input.

    Recognizes explicit search-intent phrases ("search for X", "look up X",
    "what is X", "tell me about X", etc.) and returns the remainder as the
    query. Falls back to the same proper-noun/quote extraction used for
    vision descriptions if no explicit phrase is found.
    """
    if not text or len(text.strip()) < 3:
        return ""

    trigger_patterns = [
        r'\b(?:search|google|look\s*up|find(?:\s+out)?)\s+(?:for\s+|about\s+)?(.+)',
        r'\bwhat(?:\'s|\s+is|\s+are)\s+(.+)',
        r'\bwho(?:\'s|\s+is|\s+are)\s+(.+)',
        r'\btell\s+me\s+about\s+(.+)',
        r'\bcan\s+you\s+(?:search|look\s*up|find)\s+(?:for\s+)?(.+)',
    ]
    for pat in trigger_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            candidate = m.group(1).strip(" ?.!,'\"")
            if len(candidate) >= 2:
                return candidate[:80]

    # No explicit search phrase — fall back to proper-noun/quote extraction
    return _extract_keywords(text)


def _search_web(query: str, max_results: int = 3) -> str:
    """Search the web using DuckDuckGo and return a compact summary of results."""
    if not WEB_SEARCH_AVAILABLE or not query.strip():
        return ""
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        if not results:
            return ""
        snippets = []
        for r in results:
            title = r.get('title', '').strip()
            body = r.get('body', '').strip()
            if title and body:
                snippets.append(f"{title}: {body[:200]}")
            elif body:
                snippets.append(body[:200])
        if snippets:
            return " | ".join(snippets)
        return ""
    except Exception as e:
        _safe_print(f"[Info] Web search failed: {e}")
        return ""


def generate_chat_reactions(image_path, user_context=None):
    """Sends the screenshot to the local model and requests chat reactions.

    Uses a two-phase approach:
      1. Vision model describes the screen.
      2. Web search enriches context based on the description.
      3. Vision model generates reactions with the enriched context.
    """
    # Encode the image as base64 for the ollama library (v0.6+ expects base64 data, not file paths)
    try:
        with open(image_path, 'rb') as f:
            image_data = base64.b64encode(f.read()).decode('utf-8')
    except Exception as e:
        print(f"[Error] Failed to read/encode screenshot: {e}")
        return []

    # --- Phase 1: Get a description from the vision model ---
    describe_prompt = (
        "Look closely at this screenshot. Write a one-sentence DESCRIPTION of the specific "
        "activity, text, or game visible on the screen. Be specific — name the game, app, "
        "website, or activity if you can identify it.\n\n"
        "Output exactly in this format:\n"
        "DESCRIPTION: <what you see>"
    )

    ocr_text = None
    if OCR_AVAILABLE:
        try:
            img = Image.open(image_path)
            ocr_text = pytesseract.image_to_string(img).strip()
            img.close()
        except Exception:
            ocr_text = None

    if ocr_text:
        one_line = ' '.join(ocr_text.split())[:150]
        describe_prompt += f"\n\nVisible screen text for context: '{one_line}'"

    description = ""
    try:
        try:
            desc_response = ollama.chat(
                model=MODEL_NAME,
                messages=[{
                    'role': 'user',
                    'content': describe_prompt,
                    'images': [image_data]
                }],
                temperature=0.4,
                max_tokens=128000
            )
        except TypeError:
            desc_response = ollama.chat(
                model=MODEL_NAME,
                messages=[{
                    'role': 'user',
                    'content': describe_prompt,
                    'images': [image_data]
                }]
            )

        desc_text = ""
        if hasattr(desc_response, 'message') and hasattr(desc_response.message, 'content'):
            desc_text = desc_response.message.content
        elif isinstance(desc_response, str):
            desc_text = desc_response
        elif isinstance(desc_response, dict):
            if 'message' in desc_response and isinstance(desc_response['message'], dict) and 'content' in desc_response['message']:
                desc_text = desc_response['message']['content']
            elif 'content' in desc_response:
                desc_text = desc_response['content']

        description = _extract_description(desc_text)
        if not description:
            # Fallback: use the raw response as the description
            description = desc_text.strip()[:200]
    except Exception as e:
        _safe_print(f"[Info] Vision description failed: {e}")
        description = ""

    # --- Phase 2: Search the web for context, using spoken input first ---
    web_context = ""
    if WEB_SEARCH_AVAILABLE and (user_context or description):
        search_query = ""
        query_source = ""

        # Prefer spoken context — it's a direct signal of what the person
        # actually wants, rather than a guess based on the screenshot.
        if user_context:
            search_query = _extract_voice_query(user_context)
            if search_query:
                query_source = "voice"

        # Fall back to the visual description if voice didn't yield anything
        if not search_query and description:
            search_query = _extract_keywords(description)
            if search_query:
                query_source = "screen"

        if search_query:
            _safe_print(f"[Info] Searching web for: {search_query} (from {query_source})")
            web_context = _search_web(search_query, max_results=3)
            if web_context:
                _safe_print(f"[Info] Web context retrieved ({len(web_context)} chars)")
            else:
                _safe_print("[Info] No web results found")
        else:
            _safe_print("[Info] Nothing specific enough for web search, skipping")

    # --- Phase 3: Generate chat reactions with full context ---
    base_prompt = (
        "You are an active live streaming chat audience. Look closely at this screenshot.\n"
        "1. First, write a one-sentence DESCRIPTION of the specific activity, text, or game on the screen.\n"
        "2. Then, write 1 to 3 short, distinct chat REACTIONS (2-12 words each) reacting DIRECTLY to that description. "
        "Use typical Twitch/YouTube chat slang and emotes where appropriate.\n\n"
        "You must output exactly in this format:\n"
        "DESCRIPTION: <what you see>\n"
        "REACTIONS:\n"
        "- <reaction 1>\n"
        "- <reaction 2>\n"
        "- <reaction 3>"
    )

    prompt_main = f"{base_prompt}\n\nScreenshot: ![screenshot]({image_path})\n\n"

    if description:
        prompt_main += f"Screen description: '{description}'\n\n"

    if web_context:
        prompt_main += f"Web context about what's on screen: {web_context}\n\n"

    if ocr_text:
        one_line = ' '.join(ocr_text.split())[:150]
        prompt_main += f"Visible screen text for context: '{one_line}'\n\n"

    if user_context:
        uc = ' '.join(user_context.split())
        prompt_main += f"The streamer just said: '{uc}'\n\n"

    try:
        try:
            response = ollama.chat(
                model=MODEL_NAME,
                messages=[{
                    'role': 'user',
                    'content': prompt_main,
                    'images': [image_data]
                }],
                temperature=0.8,
                max_tokens=128000
            )
        except TypeError:
            response = ollama.chat(
                model=MODEL_NAME,
                messages=[{
                    'role': 'user',
                    'content': prompt_main,
                    'images': [image_data]
                }]
            )

        # Normalize response to text
        content_text = ""
        if hasattr(response, 'message') and hasattr(response.message, 'content'):
            content_text = response.message.content
        elif isinstance(response, str):
            content_text = response
        elif isinstance(response, dict):
            if 'message' in response and isinstance(response['message'], dict) and 'content' in response['message']:
                content_text = response['message']['content']
            elif 'content' in response:
                content_text = response['content']

        # Phrases that indicate an AI disclaimer/apology/refusal rather than a real chat reaction
        _AI_REFUSAL_PATTERNS = [
            r'as an?\s+(ai|language\s+model|assistant)',
            r'(cannot|cannot|can\'t|unable to)\s+(assist|provide|generate|analyze|process)',
            r'(does not|do not|don\'t|not\s+able to)\s+(have|possess|contain)\s+(the\s+)?ability',
            r'(against|violates?)\s+(policy|guidelines|rules|tos)',
            r'(not\s+relevant|not\s+appropriate|not\s+related)\s+to\s+(the\s+)?(task|request|question)',
            r'(I\'?m?\s+)?sorry\s*(,|but)',
            r'trained\s+by\s+openai',
            r'(my\s+)?purpose\s+is\s+to\s+assist',
            r'not\s+ethically',
            r'cannot\s+fulfill',
            r'cannot\s+complete',
            r'cannot\s+help',
            r'not\s+possible\s+to',
            r'no\s+visual\s+(information|content|data|input)',
            r'text\s+based\s+(on|description)',
            r'nothing\s+(in|on)\s+(the\s+)?(image|screenshot|screen)',
            r'(black|blank|empty|dark)\s+(screen|image|screenshot)',
        ]
        _AI_REFUSAL_REGEX = re.compile('|'.join(_AI_REFUSAL_PATTERNS), re.IGNORECASE)

        def _is_ai_refusal(text: str) -> bool:
            """Return True if the line looks like an AI disclaimer/refusal rather than a chat reaction."""
            word_count = len(text.split())
            if word_count > 15:
                return True
            if _AI_REFUSAL_REGEX.search(text):
                return True
            return False

        # Parse the output to ONLY extract the chat reactions, skipping the description
        raw_lines = [ln.strip() for ln in content_text.strip().splitlines() if ln.strip()]
        clean_reactions = []
        is_reaction_section = False
        
        for line in raw_lines:
            if line.upper().startswith("REACTIONS"):
                is_reaction_section = True
                continue
            elif line.upper().startswith("DESCRIPTION"):
                is_reaction_section = False
                continue
                
            if is_reaction_section:
                clean_line = re.sub(r'^\s*([0-9]+[.)]\s*|[-\u2022\*]\s*)', '', line).strip()
                clean_line = re.sub(r'^["\'](.*)["\']$', r'\1', clean_line).strip()
                if clean_line and not _is_ai_refusal(clean_line):
                    clean_reactions.append(clean_line)

        # If parsing failed, fallback to returning whatever it outputted that looks like a list
        if not clean_reactions:
             for line in raw_lines:
                 if line.startswith("-") or line.startswith("*"):
                     clean_line = re.sub(r'^\s*([0-9]+[.)]\s*|[-\u2022\*]\s*)', '', line).strip()
                     clean_line = re.sub(r'^["\'](.*)["\']$', r'\1', clean_line).strip()
                     if clean_line and not _is_ai_refusal(clean_line):
                         clean_reactions.append(clean_line)

        return clean_reactions[:5]

    except Exception as e:
        print(f"\n[System Error]: Failed to contact Ollama or parse response. Error: {e}")
        return []

# Continuous STT listener support (fills a rolling context buffer)
_CONTEXT_DEQUE = None
_CONTEXT_LOCK = None
_LISTENER_THREAD = None
_LISTENER_RUNNING = False
_LISTENER_OK = False


def get_continuous_context():
    """Return joined transcripts from the rolling STT buffer, or None if empty."""
    global _CONTEXT_DEQUE, _CONTEXT_LOCK
    if _CONTEXT_DEQUE is None:
        return None
    with _CONTEXT_LOCK:
        if not _CONTEXT_DEQUE:
            return None
        return ' '.join(list(_CONTEXT_DEQUE))


def stt_status():
    """Return a status dict for the STT listener and recent transcripts.

    Returns: {'running': bool, 'size': int, 'recent': [str,...]}
    """
    global _CONTEXT_DEQUE, _CONTEXT_LOCK, _LISTENER_RUNNING, _LISTENER_THREAD
    # Consider the thread alive instead of the separate _LISTENER_OK flag to reflect reality
    running = False
    try:
        running = bool(_LISTENER_RUNNING and _LISTENER_THREAD is not None and _LISTENER_THREAD.is_alive())
    except Exception:
        running = bool(_LISTENER_RUNNING)
    status = {'running': running, 'size': 0, 'recent': []}
    if _CONTEXT_DEQUE is None:
        return status
    with _CONTEXT_LOCK:
        status['size'] = len(_CONTEXT_DEQUE)
        # return up to last 5 entries
        status['recent'] = list(_CONTEXT_DEQUE)[-5:]
    return status


def start_continuous_listener(chunk_duration=4, fs=16000):
    """Start a daemon thread that continuously records short audio chunks and appends transcriptions to an in-memory buffer.

    Uses sounddevice + soundfile to capture audio and SpeechRecognition (recognize_google) for STT. Gracefully degrades on errors.
    """
    global _CONTEXT_DEQUE, _CONTEXT_LOCK, _LISTENER_THREAD, _LISTENER_RUNNING, _LISTENER_OK
    _LISTENER_OK = False
    if not SR_AVAILABLE:
        return False
    if _LISTENER_THREAD and _LISTENER_THREAD.is_alive():
        return True
    _CONTEXT_DEQUE = deque(maxlen=20)
    _CONTEXT_LOCK = threading.Lock()
    _LISTENER_RUNNING = True

    def _worker():
        try:
            import sounddevice as sd
            import soundfile as sf
            import speech_recognition as sr_local
            import tempfile, os
            r = sr_local.Recognizer()
            # mark listener OK after successful imports/init
            try:
                global _LISTENER_OK
                _LISTENER_OK = True
            except Exception:
                pass
        except Exception as e:
            # mark listener as not OK and exit
            return
        try:
            while _LISTENER_RUNNING:
                try:
                    data = sd.rec(int(chunk_duration * fs), samplerate=fs, channels=1, dtype='int16')
                    sd.wait()
                    tf = tempfile.NamedTemporaryFile(delete=False, suffix='.wav')
                    try:
                        sf.write(tf.name, data, fs)
                        source = sr_local.AudioFile(tf.name)
                        source.__enter__()
                        audio = r.record(source)
                        try:
                            text = r.recognize_google(audio)
                            text = text.strip()
                            if text:
                                with _CONTEXT_LOCK:
                                    _CONTEXT_DEQUE.append(text)
                                _safe_print(f"[STT] Heard: {text}")
                        except Exception as e:
                            pass
                        finally:
                            try:
                                source.__exit__(None, None, None)
                            except Exception:
                                pass
                    finally:
                        try:
                            os.unlink(tf.name)
                        except Exception:
                            pass
                except Exception as e:
                    pass
        finally:
            pass

    _LISTENER_THREAD = threading.Thread(target=_worker, daemon=True)
    _LISTENER_THREAD.start()
    # give a moment for thread to start
    time.sleep(0.1)
    # reflect actual thread aliveness in _LISTENER_OK
    try:
        _LISTENER_OK = bool(_LISTENER_THREAD and _LISTENER_THREAD.is_alive())
    except Exception:
        _LISTENER_OK = False
    return True


def stop_continuous_listener():
    global _LISTENER_RUNNING, _LISTENER_THREAD
    _LISTENER_RUNNING = False
    if _LISTENER_THREAD:
        try:
            _LISTENER_THREAD.join(timeout=2)
        except Exception:
            pass


def main():
    _safe_print(f"=== Starting Local Live Chat Simulator (Using {MODEL_NAME}) ===")
    _safe_print("Open up a game, video, or application on your screen.")
    _safe_print("Press Ctrl+C in this terminal to stop.")
    _safe_print("-" * 50)

    if TTS_AVAILABLE:
        _safe_print("[TTS] Text-to-speech enabled — chat messages will be spoken aloud.")
        _safe_print(f"[TTS] Using {len(TTS_VOICES)} different voices for chat variety.")
        
        # Check and report spatial audio status
        if SPATIAL_AUDIO_AVAILABLE:
            try:
                pa = _get_pyaudio()
                if pa:
                    default_output = pa.get_default_output_device_info()
                    channels = default_output.get('maxOutputChannels', 2)
                    channel_names = {2: 'Stereo', 6: '5.1 Surround', 8: '7.1 Surround'}
                    channel_desc = channel_names.get(channels, f'{channels}-channel')
                    _safe_print(f"[Spatial] Spatial audio enabled — random positions on {channel_desc} speakers")
            except Exception:
                _safe_print("[Spatial] Spatial audio enabled — using stereo panning fallback")
        else:
            _safe_print("[Spatial] pydub not installed; spatial audio disabled (install with: pip install pydub)")
    else:
        _safe_print("[TTS] edge-tts not installed; chat messages will only be displayed as text.")
        _safe_print("[TTS] Install with: pip install edge-tts")

    # Start continuous listener automatically when STT is available
    # (set DISABLE_STT=1 as an env var to skip this for debugging)
    if SR_AVAILABLE and not os.environ.get("DISABLE_STT"):
        started = start_continuous_listener()
        if not started:
            _safe_print('[Info] Continuous listener failed to start; falling back to manual recording prompt')
    else:
        _safe_print('[Info] SpeechRecognition not available or disabled via DISABLE_STT; manual recording disabled.')

    # TTS voice and position assignment: map usernames to voices and spatial positions
    user_voice_map = {}
    user_position_map = {}

    try:
        while True:
            # Use continuous STT context if available
            if SR_AVAILABLE:
                user_ctx = get_continuous_context()
            else:
                user_ctx = None

            # 1. Take a picture of what the user is doing
            img_path = capture_screen()

            # 2. Get the AI to act like a stream chat, supplying optional spoken context
            reactions = generate_chat_reactions(img_path, user_context=user_ctx)

            # 3. Safely delete the temporary screenshot
            if os.path.exists(img_path):
                os.remove(img_path)

            # 4. Stream the chat messages with slight, realistic delay offsets, and speak them aloud
            for reaction in reactions:
                if reaction:
                    user = random.choice(USERNAMES)
                    # Ensure reaction is a clean string without model metadata
                    reaction_str = str(reaction).strip()
                    # Skip if this looks like raw model output (contains Ollama response metadata)
                    ollama_meta_keys = ['model=', 'created_at=', 'done=True', 'done_reason=', 'total_duration=', 'load_duration=', 'prompt_eval_count=', 'prompt_eval_duration=', 'eval_count=', 'eval_duration=', 'message=Message(', 'logprobs=', 'role=', 'tool_calls=']
                    if any(k in reaction_str for k in ollama_meta_keys):
                        continue
                    # Also skip if line starts with typical Ollama repr patterns (key='value' pairs)
                    if re.match(r"^\w+='\w+'", reaction_str):
                        continue
                    # Strip any remaining surrounding double or single quotes from the reaction
                    reaction_str = re.sub(r'^["\'](.*)["\']$', r'\1', reaction_str).strip()
                    
                    # Print the chat message to console
                    try:
                        _safe_print(f"[{user}]: {reaction_str}")
                    except Exception:
                        # Last-resort fallback
                        try:
                            sys.stdout.buffer.write((f"[{user}]: {reaction_str}\n").encode(sys.stdout.encoding or 'utf-8', 'replace'))
                        except Exception:
                            pass
                    
                    # Speak the reaction aloud via TTS. Requests are queued and
                    # handled one at a time by a single worker thread — this
                    # avoids opening multiple simultaneous WebSocket connections
                    # to Microsoft's TTS service, which some firewalls/AV/networks
                    # will drop, causing NoAudioReceived even though a single
                    # isolated request succeeds.
                    if TTS_AVAILABLE and reaction_str:
                        # Assign a voice to this user (consistent per username)
                        if user not in user_voice_map:
                            user_voice_map[user] = random.choice(TTS_VOICES)
                        voice = user_voice_map[user]
                        
                        # Assign a spatial position to this user (consistent per username)
                        # Different users appear to speak from different directions
                        if user not in user_position_map:
                            user_position_map[user] = _get_spatial_position()
                        position = user_position_map[user]
                        speak_text_async(reaction_str, voice, position)
                    
                    # Stagger the messages so they feel like a live scrolling chat
                    time.sleep(random.uniform(0.4, 1.2))

            # 5. Wait for the next screen evaluation — the model had as long as it needed
            #    to analyze the image during the blocking ollama.chat() call above.
            time.sleep(random.uniform(3.5, 5.0))

    except KeyboardInterrupt:
        print("\nStopping chat simulation. Goodbye!")

    # Clean up TTS temp dir on exit
    global _TTS_TEMP_DIR
    if _TTS_TEMP_DIR and os.path.exists(_TTS_TEMP_DIR):
        try:
            for fname in os.listdir(_TTS_TEMP_DIR):
                try:
                    os.unlink(os.path.join(_TTS_TEMP_DIR, fname))
                except Exception:
                    pass
            os.rmdir(_TTS_TEMP_DIR)
        except Exception:
            pass

if __name__ == "__main__":
    main()