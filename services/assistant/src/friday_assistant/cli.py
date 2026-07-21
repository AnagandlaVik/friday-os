import uvicorn

from friday_assistant.config import Settings


def main() -> None:
    settings = Settings()

    uvicorn.run(
        "friday_assistant.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )
