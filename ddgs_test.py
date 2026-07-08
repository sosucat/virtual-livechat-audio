import asyncio
import edge_tts

try:
    from ddgs import DDGS
except Exception:
    from duckduckgo_search import DDGS

print("Doing a DDGS web search first...")
try:
    with DDGS() as ddgs:
        results = list(ddgs.text("Minecraft", max_results=3))
    print(f"Search returned {len(results)} results")
except Exception as e:
    print(f"Search failed: {e}")

print("Now attempting TTS...")
try:
    asyncio.run(edge_tts.Communicate("hello after a web search", "en-US-JennyNeural").save("test5.mp3"))
    print("done - success")
except Exception as e:
    print(f"FAILED: {e}")
