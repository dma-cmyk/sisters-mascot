"""Sisters Mascot package constants and shared paths."""

from pathlib import Path

APP_NAME = "Sisters Mascot"
APP_VERSION = "0.1.0"
PROJECT_ROOT = Path("/home/dma/プロジェクト/sisters-mascot")
ASSETS_DIR = PROJECT_ROOT / "assets"
SPRITES_DIR = ASSETS_DIR / "sprites"
CONFIG_DIR = Path.home() / ".config" / "sisters-mascot"
USER_SPRITES_DIR = CONFIG_DIR / "sprites"
WIKI_AGENTS_DIR = Path("/home/dma/sisters-wiki/docs/agents")

__all__ = [
    "APP_NAME",
    "APP_VERSION",
    "PROJECT_ROOT",
    "ASSETS_DIR",
    "SPRITES_DIR",
    "CONFIG_DIR",
    "USER_SPRITES_DIR",
    "WIKI_AGENTS_DIR",
]
