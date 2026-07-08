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

try:
    import pytesseract
    from PIL import Image
except Exception:
    pass

try:
    import speech_recognition as sr
except Exception:
    pass

import edge_tts

print("All imports done. Checking for proxy env vars...")
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    if k in os.environ:
        print(f"  {k} = {os.environ[k]}")
print("(none printed above means no proxy env vars are set)")

print("Now attempting TTS...")
try:
    asyncio.run(edge_tts.Communicate("hello after imports", "en-US-JennyNeural").save("test4.mp3"))
    print("done - success")
except Exception as e:
    print(f"FAILED: {e}")
