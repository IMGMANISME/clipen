"""Internal blocking entry point used by the first-run bootstrap."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time

from clipen.asr_release import NOTICE_VERSION

from .setup import ASRSetupError, ASRSetupManager, ASRSetupState


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--requirements", required=True)
    try:
        options = parser.parse_args(arguments)
        manager = ASRSetupManager(
            Path(options.data_root),
            Path(options.requirements),
        )
    except (OSError, ValueError, ASRSetupError) as error:
        print(f"無法準備本機語音辨識：{error}", file=sys.stderr, flush=True)
        return 1

    try:
        state = manager.status().state
        if state is ASRSetupState.READY:
            print("✓ 本機語音辨識已準備完成", flush=True)
            return 0
        if state is ASRSetupState.FAILED:
            removed = manager.remove()
            if removed.state is not ASRSetupState.SETUP_REQUIRED:
                print("無法安全清除不完整的語音辨識安裝", file=sys.stderr, flush=True)
                return 1
        manager.start_install(NOTICE_VERSION)
        last_phase = None
        while True:
            status = manager.status()
            if status.phase != last_phase:
                print(status.message, flush=True)
                last_phase = status.phase
            if status.state is ASRSetupState.READY:
                print("✓ 本機語音辨識與固定模型安裝完成", flush=True)
                return 0
            if status.state is not ASRSetupState.INSTALLING:
                print(
                    f"本機語音辨識設定失敗：{status.error_code or status.phase}",
                    file=sys.stderr,
                    flush=True,
                )
                return 1
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("正在取消本機語音辨識設定…", flush=True)
        return 130
    except ASRSetupError as error:
        print(f"本機語音辨識設定失敗：{error.code}", file=sys.stderr, flush=True)
        return 1
    finally:
        manager.close(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
