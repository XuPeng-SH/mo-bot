import argparse
import logging
from pathlib import Path
from .config import Config
from .service import Service


def main():
    parser = argparse.ArgumentParser(description="Durable reusable Codex PR reviewer")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    config_path = args.config.expanduser().resolve()
    Service(Config.load(config_path), config_path).run()


if __name__ == "__main__":
    main()
