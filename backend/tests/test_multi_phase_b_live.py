"""Phase B 真实联调：解析 Phase A 输出 → POST /chat/multi/confirm → 事件摘要。"""
import json
import re
import sys

import requests

BASE = "http://127.0.0.1:8123/api/v1"
TOKEN = requests.post(f"{BASE}/auth/login",
                      json={"username": "admin", "password": "admin123"}).json()["access_token"]
H = {"Authorization": f"Bearer {TOKEN}"}

# 1. 解析 Phase A 输出（按 SSE 块解析）
with open("/tmp/multi_phaseA.jsonl") as f:
    raw = f.read()

conv_id = None
spec = None
evt = None
data = []
for line in raw.splitlines():
    if not line.strip():
        if evt == "conv_id" and data:
            conv_id = int(json.loads("".join(data).lstrip("data: "))["conversation_id"])
        if evt == "multi_spec" and data:
            spec = json.loads("".join(data).lstrip("data: "))
        evt, data = None, []
        continue
    if line.startswith("event:"):
        evt = line[6:].strip()
    elif line.startswith("data:"):
        data.append(line[5:].strip())

assert conv_id, "未找到 conversation_id"
assert spec, "未找到 multi_spec"
task_id = spec["task_id"]
subs = spec["sub_queries"]
print(f"[Phase B] conv_id={conv_id} task_id={task_id} subs={len(subs)}")

# 2. 调用 confirm（SSE 流）
payload = {
    "conversation_id": conv_id,
    "question": spec["original_question"],
    "datasource_id": 2,
    "workspace_id": 1,
    "task_id": task_id,
    "sub_queries": subs,
}
summary = {}
with requests.post(f"{BASE}/chat/multi/confirm", headers=H, json=payload,
                   stream=True, timeout=180) as resp:
    print("HTTP", resp.status_code)
    evt, data = None, []
    for line in resp.iter_lines(decode_unicode=True):
        if not line:
            if evt:
                try:
                    obj = json.loads("".join(data))
                except Exception:
                    obj = {"raw": "".join(data)[:200]}
                summary.setdefault(evt, []).append(obj)
            evt, data = None, []
            continue
        if line.startswith("event:"):
            evt = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].strip())

# 3. 摘要输出
for evt, items in summary.items():
    print(f"--- {evt} x{len(items)} ---")
    for it in items[:4]:
        s = json.dumps(it, ensure_ascii=False)
        print("   ", s[:300])

# 4. 断言
assert "multi_task" in summary, "缺 multi_task 事件"
assert "sub_result" in summary, "缺 sub_result（子查询未产出结果）"
assert "dashboard" in summary, "缺 dashboard 事件"
dash = summary["dashboard"][-1]
cards = dash.get("cards", [])
print(f"\n[dashboard] message_id={dash.get('message_id')} cards={len(cards)} layout={dash.get('layout')}")
for c in cards:
    data = c.get("data") or {}
    print(f"  - {c.get('sub_id')}: chart={c.get('chart_type')} rows={len(data.get('rows') or [])} "
          f"title={c.get('title','')[:20]} status={c.get('status')}")
print("\n=== Phase B 真实联调通过 ===")
