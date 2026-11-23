"""Run durable asset jobs with the same explicitly injected configuration as HTTP."""

import argparse
import asyncio
import json
import signal

from .app import RagApplication
from .config import load_settings
from .domain import RagError
from .environment import apply_environment, read_environment


async def run(tenant: str, interval: float, once: bool = False):
    settings = apply_environment(load_settings(), read_environment())
    application = RagApplication(settings)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stopping.set)
        except (NotImplementedError, RuntimeError):
            pass
    while not stopping.is_set():
        try:
            result = await application.assets.process_pending(tenant, 10)
            print(json.dumps({"worker": "assets", "result": result}, ensure_ascii=False), flush=True)
        except Exception as exc:
            print(json.dumps({"worker": "assets", "error": exc.code if isinstance(exc, RagError) else "internal_error"}), flush=True)
        if once:
            return
        try:
            await asyncio.wait_for(stopping.wait(), timeout=interval)
        except TimeoutError:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default="demo")
    parser.add_argument("--interval", type=float, default=10)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.interval <= 3600:
        parser.error("interval must be between 1 and 3600 seconds")
    asyncio.run(run(args.tenant, args.interval, args.once))


if __name__ == "__main__":
    main()
