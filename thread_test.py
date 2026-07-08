import asyncio
import threading
import edge_tts

def worker():
    try:
        asyncio.run(edge_tts.Communicate("hello from a thread", "en-US-JennyNeural").save("test3.mp3"))
        print("done - success")
    except Exception as e:
        print(f"FAILED: {e}")

t = threading.Thread(target=worker)
t.start()
t.join()
