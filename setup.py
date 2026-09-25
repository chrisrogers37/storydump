from setuptools import setup, find_packages

from src import __version__

setup(
    name="storydump",
    version=__version__,
    description="Instagram Story Automation System with Telegram Integration",
    author="Chris Rogers",
    packages=find_packages(),
    # Floors, not pins: `requirements.txt` holds the pins for a deploy, this
    # list holds the names a `pip install -e .` must resolve. Only the NAMES
    # have to agree, and `tests/test_dependency_declarations.py` holds them
    # equal in both directions.
    install_requires=[
        "asyncpg>=0.29.0",
        "click>=8.1.7",
        "cloudinary>=1.36.0",
        "cryptography>=41.0.0",
        "fastapi>=0.109.0",
        "httpx>=0.25.2",
        "psycopg2-binary>=2.9.9",
        "pydantic>=2.5.0",
        "pydantic-settings>=2.1.0",
        "python-dotenv>=1.0.0",
        "python-multipart>=0.0.9",
        "rich>=13.7.0",
        "sqlalchemy[asyncio]>=2.0.23",
        "uvicorn>=0.27.0",
    ],
    # The v2 CLI's own dependencies (decision F2: `keyring` never ships to
    # the API or the worker). `pip install 'storydump[cli]'` for the terminal.
    extras_require={
        "cli": [
            "click>=8.1.7",
            "httpx>=0.25.2",
            "rich>=13.7.0",
            "keyring>=25,<26",
        ],
    },
    entry_points={
        "console_scripts": [
            "storydump=storydump_cli.main:main",
        ],
    },
    python_requires=">=3.10",
)
