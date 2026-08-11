from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import searxng_client
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="EC Price Search API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class SearchRequest(BaseModel):
    keyword: str

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/search")
async def search_get(q: str):
    """GET /search?q=キーワード"""
    results = await searxng_client.search_products(q)
    return {"results": results, "count": len(results)}

@app.post("/api/search")
async def search_post(request: SearchRequest):
    """POST /api/search {keyword: ...} - フロントエンド互換エンドポイント"""
    results = await searxng_client.search_products(request.keyword)
    return {"results": results, "count": len(results)}
