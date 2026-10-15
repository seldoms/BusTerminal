# 用法: python cdp_shot.py <url> <out.png> [ready_js]
import json
import subprocess
import sys
import time
import urllib.request
import base64

import websocket

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
DEBUG_PORT = 9341

url, out = sys.argv[1], sys.argv[2]
ready_js = sys.argv[3] if len(sys.argv) > 3 else "document.querySelectorAll('.movie-card-title,.genre-link,.pill-tab').length > 3"

proc = subprocess.Popen([
    EDGE, "--headless=new", "--disable-gpu", "--no-first-run",
    f"--remote-debugging-port={DEBUG_PORT}", "--remote-allow-origins=*",
    "--window-size=1440,900",
    "--user-data-dir=" + __import__("tempfile").mkdtemp(prefix="edge-shot-"),
    "about:blank",
], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

try:
    ws_url = None
    for _ in range(40):
        try:
            tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json", timeout=2))
            page = next(t for t in tabs if t.get("type") == "page")
            ws_url = page["webSocketDebuggerUrl"]
            break
        except Exception:
            time.sleep(0.5)
    ws = websocket.create_connection(ws_url, timeout=30)
    mid = 0

    def send(method, params=None):
        global mid
        mid += 1
        ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == mid:
                return msg.get("result", {})

    def js(expr):
        return send("Runtime.evaluate", {"expression": expr, "returnByValue": True}).get("result", {}).get("value")

    send("Page.enable")
    send("Runtime.enable")
    send("Page.navigate", {"url": url})

    ok = False
    for _ in range(80):
        time.sleep(0.5)
        try:
            if js(ready_js):
                ok = True
                break
        except Exception:
            pass
    time.sleep(2)
    shot = send("Page.captureScreenshot", {"format": "png"})
    open(out, "wb").write(base64.b64decode(shot["data"]))
    print("ready:", ok, "saved:", out)
finally:
    proc.terminate()
