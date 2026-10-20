"""Conservative ASR warning signals, not a claim that spoken repetition is wrong."""
import re


def speech_flags(text):
    return ['repeated_asr_token_needs_listening'] if re.search(r'\b([a-z]{3,})(?:[\s,.!?，。！？]+\1){2,}\b',text,re.I) else []
