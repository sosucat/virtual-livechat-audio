import virtual_livechat, time, os, sys, random

enc = sys.stdout.encoding or 'utf-8'
print('Starting headless loop with continuous STT')

if virtual_livechat.SR_AVAILABLE:
    virtual_livechat.start_continuous_listener(chunk_duration=3)
    print('[Info] Listener started, waiting 6s to collect speech')
    time.sleep(6)
else:
    print('[Info] SpeechRecognition not available; listener not started')

end = time.time() + 30
while time.time() < end:
    img = virtual_livechat.capture_screen()
    try:
        ctx = virtual_livechat.get_continuous_context() if virtual_livechat.SR_AVAILABLE else None
    except Exception:
        ctx = None
    reacts = virtual_livechat.generate_chat_reactions(img, user_context=ctx)
    if os.path.exists(img):
        try:
            os.remove(img)
        except Exception:
            pass

    # Safe-print reactions
    try:
        print('Reactions:', reacts)
    except Exception:
        try:
            print('Reactions:', str(reacts).encode(enc, 'replace').decode(enc))
        except Exception:
            print('Reactions: <encoding error>')

    # Print STT debug info (most recent transcripts)
    try:
        status = virtual_livechat.stt_status()
        print(f"[Debug] STT listener running: {status['running']}; buffer_size: {status['size']}")
        if status['size'] > 0:
            print('[Debug] STT buffer (most recent):')
            for s in status['recent']:
                try:
                    print(' -', s)
                except Exception:
                    print(' -', s.encode(enc, 'replace').decode(enc))
        else:
            print('[Debug] STT buffer empty')
    except Exception as e:
        print('[Debug] STT status error:', e)

    time.sleep(2)

virtual_livechat.stop_continuous_listener()
print('Headless run complete')
