import os
import sys
from pathlib import Path

# Import the package under test without installing it, and give config.py a
# token so `import main` and CONFIG.validate() work in tests.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DISCORD_TOKEN", "test-token")

import pytest

from core.database import Database


@pytest.fixture
async def db(tmp_path):
    """A migrated, empty database on a temporary file."""
    database = Database(str(tmp_path / "test.db"))
    await database.connect()
    yield database
    await database.close()
