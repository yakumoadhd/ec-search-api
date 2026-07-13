import os, subprocess, httpx
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

SECRET = os.environ.get("AGENT_SECRET", "pr_agent_2026_secret")
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_DB = "08af5a60-e8dc-4753-b0e1-a68373606798"
ME = "https://pr-agent-api-826846133648.asia-northeast1.run.app"

def auth(t):
    if t != SECRET:
        raise HTTPException(401, "Unauthorized")

async def nlog(title, cat, st, detail, cb="", ca=""):
    if not NOTION_TOKEN:
        return
    h = {"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"}
    p = {"title": {"title": [{"text": {"content": title}}]}, "kind": {"select": {"name": cat}}, "state": {"select": {"name": st}}, "detail": {"rich_text": [{"text": {"content": detail[:2000]}}]}, "url": {"url": ME}}
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post("https://api.notion.com/v1/pages", headers=h, json={"parent": {"database_id": NOTION_DB}, "properties": p})
        print(f"NOTION RESPONSE: {r.status_code} {r.text[:500]}")

class Exec(BaseModel):
    cmd: str
    timeout: int = 30
    log: bool = True

@app.get("/health")
async def healthz():
    return {"status": "ok", "version": "v1.2"}

@app.get("/status")
async def selfcheck(x_agent_token: str = Header(...)):
    auth(x_agent_token)
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://pr-agent-api-826846133648.asia-northeast1.run.app/health")
        st = "OK" if r.status_code == 200 else "ERROR"
        detail = f"HTTP {r.status_code}"
    except Exception as e:
        st, detail = "ERROR", str(e)
    from datetime import datetime
    await nlog(f"HealthCheck {datetime.now().strftime('%Y-%m-%d %H:%M')}", "ヘルスチェック", st, detail)
    return {"status": st, "detail": detail}

@app.post("/exec")
async def exec_cmd(req: Exec, x_agent_token: str = Header(...)):
    auth(x_agent_token)
    r = subprocess.run(req.cmd, shell=True, capture_output=True, text=True, timeout=req.timeout)
    return {"returncode": r.returncode, "output": r.stdout + r.stderr}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
