from fastapi import FastAPI

from app.routers import admin, auth, ingest, operator, project_memory, retrieve

app = FastAPI(title="GuardRail RAG", version="0.1.0")

app.include_router(auth.router)
app.include_router(operator.router)
app.include_router(admin.router)
app.include_router(ingest.router)
app.include_router(project_memory.router)
app.include_router(retrieve.router)


@app.get("/health")
async def health():
    return {"status": "ok"}
