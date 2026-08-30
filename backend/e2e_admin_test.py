"""管理员后台 + 文字入库 + RAG 检索 端到端自测（不依赖任何真实 API Key）。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")

from fastapi.testclient import TestClient  # noqa: E402

from app.core import runtime_config  # noqa: E402

# 清空运行时配置，从默认口令开始
runtime_config._CONFIG_PATH.unlink(missing_ok=True) if runtime_config._CONFIG_PATH.exists() else None
Path("data/app.db").unlink(missing_ok=True)  # SQLite 默认持久化：测试前清库保证幂等
runtime_config._cache = None
runtime_config._cache_mtime = -1.0

from app.main import app  # noqa: E402

client = TestClient(app)
passed, failed = [], []


def check(name: str, cond: bool, detail: str = ""):
    (passed if cond else failed).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f" —— {detail}" if detail else ""))


# ---------------------------------------------------------------- 认证 ------
r = client.post("/api/v1/admin/login", json={"password": "wrong"})
check("错误密码被拒绝(401)", r.status_code == 401)

r = client.post("/api/v1/admin/login", json={"password": "admin123"})
token = r.json().get("token", "")
check("默认口令登录成功", r.status_code == 200 and bool(token))

H = {"X-Admin-Token": token}

r = client.get("/api/v1/admin/config")
check("未带 Token 读取配置被拒(401)", r.status_code == 401)

r = client.get("/api/v1/admin/config", headers=H)
view = r.json()
masked_ok = all(
    (not view[k]["api_key"]) or view[k]["api_key"].startswith("******")
    for k in ("llm", "embedding", "asr", "vlm")
)
check("读取配置（Key 已打码）", r.status_code == 200 and masked_ok, json.dumps(view["llm"], ensure_ascii=False)[:120])

# ------------------------------------------------------------ 配置热更新 ----
payload = {"asr": {"base_url": "", "api_key": "", "model": ""}, 
           "llm": {"provider": "openai-compatible", "base_url": "https://api.openai.com/v1",
                   "api_key": "sk-test-1234567890abcdef", "model": "test-llm"}}
r = client.put("/api/v1/admin/config", headers=H, json=payload)
new_view = r.json()
check("保存 LLM 配置生效", new_view["llm"]["model"] == "test-llm" and new_view["llm"]["configured"] is True,
      json.dumps(new_view["llm"], ensure_ascii=False))

from app.services.llm.provider import get_provider, _build_from_settings, invalidate_provider_cache  # noqa: E402
invalidate_provider_cache()
providers = _build_from_settings(get_settings_stub := __import__("app.core.config", fromlist=["get_settings"]).get_settings())
check("主模型 Provider 热注册(main)", "main" in providers and providers["main"].name == "openai-main")
p = get_provider()
check("get_provider 默认走面板主模型", p.name == "openai-main")

# 掩码提交 = 保持不变；空串 = 清空回落
patch_keep = {"llm": {"api_key": "******cdef"}}
r = client.put("/api/v1/admin/config", headers=H, json=patch_keep)
kept = r.json()["llm"]
patch_clear = {"llm": {"base_url": "", "api_key": "", "model": ""}}
r = client.put("/api/v1/admin/config", headers=H, json=patch_clear)
cleared = r.json()["llm"]
env_fallback = cleared["api_key"] != "" or cleared["model"] not in ("test-llm",)
check("掩码保持 / 清空回落环境变量", kept["api_key"].startswith("******") and env_fallback,
      f"kept={kept['api_key'][:10]} cleared={json.dumps(cleared, ensure_ascii=False)[:80]}")

invalidate_provider_cache()

# ------------------------------------------------------------ 测试连通性 ----
for kind in ("llm", "embedding", "asr"):
    r = client.post(f"/api/v1/admin/config/test?kind={kind}", headers=H)
    d = r.json()
    check(f"连通性测试接口 {kind} 返回结构", r.status_code == 200 and isinstance(d.get("ok"), bool) and d.get("message"),
          d.get("message", "")[:60])

# ------------------------------------------------------------ 文字入库 ------
r = client.post(
    "/api/v1/ingest",
    data={
        "session_id": "e2e-admin",
        "media_type": "text",
        "text_content": (
            "今天讲二重积分的换元法。首先回顾直角坐标下的累次积分计算，关键是画域定限。\n\n"
            "极坐标变换公式：x=r·cosθ，y=r·sinθ，面积微元 dxdy 变成 r·drdθ，千万别丢雅可比因子 r。\n\n"
            "例题：计算单位圆域上的 ∫∫(x²+y²)dxdy。用极坐标立刻化为 ∫θ ∫r 的 r³ 积分，答案是 π/2。\n\n"
            "常见错误：把积分上下限的圆域直接当成矩形域，忘记先对 θ 定范围再对 r 定范围。"
        ),
    },
)
d = r.json()
check("文字素材入库成功", r.status_code == 200 and d["chunks_added"] >= 3, f"chunks_added={d.get('chunks_added')}")
asset_id = d["asset"]["id"]

from app.services.rag.retriever import get_retriever  # noqa: E402
import asyncio  # noqa: E402

retriever = asyncio.get_event_loop().run_until_complete(get_retriever()) if False else None


async def score_probe():
    ret = await get_retriever()
    return await ret.retrieve_scored("极坐标变换后雅可比因子 r 别丢")

scored = asyncio.run(score_probe())
top = scored[0][1].text if scored else ""
hit_text = bool(scored) and scored[0][1].text.find("雅可比") != -1 or scored[0][0] >= 0.45 and ("极坐标" in top)
check("入库切片可被检索命中(且排第一)", hit_text, f"top_score={scored[0][0]:.2f} text={top[:30]}")

# 无关问题的相关性门槛
async def irrelevant_probe():
    ret = await get_retriever()
    return await ret.retrieve("今天晚饭吃什么好呢", min_score=0.42), await ret.retrieve("如何判断反常积分收敛", top_k=3)

irr, rel = asyncio.run(irrelevant_probe())
print(f"   [分数校准] 无关问题命中 {len(irr)} 条 · 相关问题命中 {len(rel)} 条")

# ------------------------------------------------------------ 流式提问 ------
with client.stream("POST", "/api/v1/chat/stream", json={"session_id": "e2e-admin", "text": "极坐标换元时雅可比因子是什么来着？"}) as resp:
    events = []
    for line in resp.iter_lines():
        if line.startswith("event:"):
            events.append(line.split(":", 1)[1].strip())
    check("general 提问流式返回完成", "done" in events, f"events={events}")

with client.stream("POST", "/api/v1/chat/stream", json={"session_id": "e2e-admin", "text": "你好呀"}) as resp:
    got_evidence = False
    buf = ""
    for line in resp.iter_lines():
        buf += line + "\n"
        if line.startswith("event:evidence") or line.startswith("event: evidence"):
            got_evidence = True
    print(f"   [问候语] evidence 注入={got_evidence}（阈值过严则应为 False）")

# ------------------------------------------------------------ 统计与改密 ----
r = client.get("/api/v1/admin/stats", headers=H)
st = r.json()
check("统计接口正常", r.status_code == 200 and st["assets"] >= 1 and st["uploaded_chunks"] > 0,
      json.dumps({k: st[k] for k in ("assets", "chunks", "uploaded_chunks")}, ensure_ascii=False))

r = client.post("/api/v1/admin/password", headers=H, json={"old_password": "admin123", "new_password": "class2026"})
check("修改管理员密码", r.status_code == 200)

r = client.post("/api/v1/admin/login", json={"password": "admin123"})
check("旧密码已失效", r.status_code == 401)
r = client.post("/api/v1/admin/login", json={"password": "class2026"})
check("新密码可登录", r.status_code == 200)

runtime_config._CONFIG_PATH.unlink(missing_ok=True)
runtime_config._cache = None
runtime_config._cache_mtime = -1.0

print("\n========== 结果汇总 ==========")
print(f"通过 {len(passed)} 项 · 失败 {len(failed)} 项")
if failed:
    print("失败项：", failed)
    sys.exit(1)
