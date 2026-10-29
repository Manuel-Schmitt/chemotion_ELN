"""Launch the graphical chemotion inventory station."""

import argparse
import os
import sys
from dataclasses import replace
from pathlib import Path

from .api import ApiError, Client
from .workflow import Config, Service, WorkflowError


def make_client(config: Config) -> Client:
    token = os.environ.get("CHEMOTION_API_TOKEN", "")
    if not token or token != token.strip() or any(ord(char) < 33 for char in token):
        raise WorkflowError("Set CHEMOTION_API_TOKEN in the protected station environment.")
    return Client(config.base_url, token, timeout=config.timeout)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Graphical chemotion inventory station")
    result.add_argument("--config", type=Path, required=True)
    result.add_argument("--windowed", action="store_true", help="Disable initial full-screen mode")
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        config = Config.load(args.config)
        if args.windowed:
            config = replace(config, fullscreen=False)
        client = make_client(config)
        try:
            from .gui import run
        except ImportError as exc:
            raise WorkflowError("Tkinter is required. On Raspberry Pi OS install python3-tk.") from exc
        run(Service(client, config))
    except (ApiError, WorkflowError, OSError) as exc:
        print(f"Inventory station: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
