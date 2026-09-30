# Copyright © 2026 Joshua Seaton. All rights reserved. Evaluation only; see LICENSE.
"""Where the agent keeps City downloads, its SQLite store and its reports.

DATA_DIR defaults to ./data and OUTPUT_DIR to ./output, relative to the directory you run
the agent from. Relocate them with ZONING_DATA_DIR and ZONING_OUTPUT_DIR. A .env file in
the working directory is read for these and for ANTHROPIC_API_KEY.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path.cwd() / ".env")

DATA_DIR = Path(os.environ.get("ZONING_DATA_DIR") or Path.cwd() / "data").expanduser()
OUTPUT_DIR = Path(os.environ.get("ZONING_OUTPUT_DIR") or Path.cwd() / "output").expanduser()
DB_PATH = DATA_DIR / "zoning.db"
