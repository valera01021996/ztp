"""
ZTP & Pipeline Server — FastAPI.

Запуск:
  pip install -r requirements.txt
  cp .env.example .env
  uvicorn main:app --host 0.0.0.0 --port 80
"""

from fastapi import FastAPI
from database import init_db
from templates import sync_templates
from routers import ztp, devices, ui

app = FastAPI(title="ZTP Server")

app.include_router(ztp.router)
app.include_router(devices.router)
app.include_router(ui.router)


@app.on_event("startup")
def on_startup():
    init_db()
    sync_templates()
