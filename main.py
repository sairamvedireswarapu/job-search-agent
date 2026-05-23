"""FastAPI app entrypoint."""
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from api import router
from config import get_settings

settings = get_settings()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

app = FastAPI(
    title="Job Search Agent",
    version="0.1.0",
    docs_url="/docs" if settings.ENV != "production" else None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.ENV == "development" else [],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/health")
async def health():
    return {"status": "ok"}


# Serve the single-page UI at root
@app.get("/")
async def serve_ui():
    return FileResponse("index.html")

@app.get("/api-keys-guide.pdf")
async def serve_pdf():
    from fastapi.responses import FileResponse
    return FileResponse("api-keys-guide.pdf", media_type="application/pdf")
