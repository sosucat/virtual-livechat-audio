import base64
import asyncio
import ollama
import edge_tts
from PIL import ImageGrab

print("Capturing screenshot...")
img = ImageGrab.grab()
img.save("temp_screen_test.png")

print("Encoding...")
with open("temp_screen_test.png", "rb") as f:
    image_data = base64.b64encode(f.read()).decode("utf-8")

print("Calling ollama.chat() with minicpm-v...")
try:
    response = ollama.chat(
        model="minicpm-v",
        messages=[{
            "role": "user",
            "content": "Describe this screenshot in one sentence.",
            "images": [image_data]
        }]
    )
    print("Ollama response received:", str(response)[:150])
except Exception as e:
    print(f"Ollama call failed: {e}")

print("Now attempting TTS...")
try:
    asyncio.run(edge_tts.Communicate("hello after ollama call", "en-US-JennyNeural").save("test6.mp3"))
    print("done - success")
except Exception as e:
    print(f"FAILED: {e}")
