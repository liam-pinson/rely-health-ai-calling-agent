import logging

from fastapi import FastAPI

from app.routers import calls, events, patients

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="AI Calling Agent")

app.include_router(calls.router)
app.include_router(events.router)
app.include_router(patients.router)