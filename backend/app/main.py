from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import config
from .db import Base, engine
from .routes import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Dev convenience; production uses `alembic upgrade head` (see README).
    Base.metadata.create_all(engine)
    yield


app = FastAPI(title="APAAR Sentinel API", version="1.0.0", lifespan=lifespan,
              description="Trust layer for APAAR-based student data: claim -> lock -> record. Synthetic data; external systems are mocked.")
app.add_middleware(CORSMiddleware, allow_origins=config.CORS_ORIGINS, allow_credentials=False,
                   allow_methods=["*"], allow_headers=["*"])
app.include_router(router)


@app.get("/health")
def health():
    return {"status": "ok"}
