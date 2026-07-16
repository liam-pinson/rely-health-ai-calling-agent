import os

from dotenv import load_dotenv

# Same pattern as alembic/env.py: load backend/.env if present, then require
# these to be set in the real environment -- no hardcoded fallbacks.
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

DATABASE_URL = os.environ["DATABASE_URL"]
RETELL_API_KEY = os.environ["RETELL_API_KEY"]
RETELL_FROM_NUMBER = os.environ["RETELL_FROM_NUMBER"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

# Which CallProvider implementation to use -- see app/providers/factory.py.
PROVIDER = os.environ.get("PROVIDER", "retell")