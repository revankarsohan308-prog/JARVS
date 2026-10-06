"""ALYO entry point.

Run with:  python main.py   (or: uvicorn api.app:app --host 127.0.0.1 --port 8760)
"""

from __future__ import annotations

import uvicorn

from config.settings import settings

if __name__ == "__main__":
    uvicorn.run(
        "api.app:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.environment == "development",
    )
