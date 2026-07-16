import logging

from fastapi import FastAPI

from app.llm_websocket import router as llm_websocket_router
from app.routers import calls, events, patients
from app.transcript_feed import router as transcript_feed_router

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="AI Calling Agent")

app.include_router(calls.router)
app.include_router(events.router)
app.include_router(patients.router)
app.include_router(llm_websocket_router)
app.include_router(transcript_feed_router)