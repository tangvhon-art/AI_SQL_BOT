"""端到端验证脚本：多意图场景断言（直连后端 SSE，结构化输出）"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8000/api/v1"


def get(path, token):
    req = urllib.request.Request(BASE + path, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def chat_stream(body, token):
    req = urllib.request.Request(BASE + "/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=90) as r:
        data = r.read().decode()
    events = []
    for block in data.split("\n\n"):
        event, payload = "message", {}
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                try:
                    payload = json.loads(line[5:].strip())
                except Exception:
                    payload = {"raw": line[5:].strip()}
        if payload:
            events.append((event, payload))
    return events


def run(token, ds_id, question, conv_id=None):
    print(f"\n{'=' * 60}\nQ: {question}" + (f"  (conv={conv_id})" if conv_id else ""))
    events = chat_stream({"question": question, "datasource_id": ds_id,
                          "conversation_id": conv_id}, token)
    got = {}
    for ev, pl in events:
        got[ev] = pl
        if ev == "spec":
            spec = pl
            print(f"  spec: intent={spec['intent']} metrics={[(m['name'], m['agg']) for m in spec['metrics']]} "
                  f"dims={[d['name'] for d in spec['dimensions']]} time={spec['time'].get('expr')}")
        elif ev == "table":
            print(f"  table: cols={pl['columns']} rows={len(pl['rows'])}")
        elif ev == "summary":
            print(f"  summary: sections={json.dumps(pl.get('sections', {}), ensure_ascii=False)}")
            if pl.get("anomalies"):
                print(f"  anomalies: {json.dumps(pl['anomalies'], ensure_ascii=False)}")
            if pl.get("candidates"):
                print(f"  clarify candidates: {len(pl['candidates'])} kind={pl.get('kind')}")
        elif ev == "trace":
            print(f"  trace: template_sql={pl.get('template_sql')} tables={pl.get('tables')}")
        elif ev == "error":
            print(f"  ERROR: {pl}")
    return got


if __name__ == "__main__":
    token = get("/auth/login", None) if False else \
        urllib.request.urlopen(urllib.request.Request(
            BASE + "/auth/login", data=json.dumps({"username": "admin", "password": "admin123"}).encode(),
            headers={"Content-Type": "application/json"}), timeout=10).read()
    token = json.loads(token)["access_token"]
    ds_list = get("/datasources", token)
    ds_id = ds_list[0]["id"]
    print(f"LOGIN OK, datasource={ds_list[0]['name']} id={ds_id}")
    scenarios = sys.argv[1:] or [
        "重试次数总和是多少",
        "按状态统计重试次数占比",
        "按类型统计重试次数前五",
        "近30天重试次数趋势",
        "本月销售额是多少",
    ]
    for q in scenarios:
        run(token, ds_id, q)
