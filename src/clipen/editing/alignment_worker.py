"""Offline subprocess for the separately installed MLX forced-aligner."""

import json
import os
import sys
from pathlib import Path

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"


def main():
    from mlx_audio.stt import load

    request = json.loads(Path(sys.argv[1]).read_text())
    model = load(request["model"])
    language = {"zh": "Chinese", "en": "English", "ja": "Japanese"}[request["language"]]
    result = model.generate(audio=request["audio"], text=request["text"], language=language)
    words = [{"text": i.text, "start": float(i.start_time), "end": float(i.end_time)} for i in result]
    Path(request["output"]).write_text(json.dumps(words, ensure_ascii=False))


if __name__ == "__main__":
    main()
