"""
test_ui.py —— 测试窗口界面的后台（ui_server.py）和 AI 群聊（groupchat.py）

    不花钱：群聊里的 AI 换成假的；设置、密钥、群聊记录都用临时文件，不动你真实的。
    1. 网页服务：没有口令、从别的地址来的请求一律拒绝；有口令才能用
    2. 界面要的数据：状态、对话记录、群聊
    3. 群聊：@ 谁就只让谁回答；不 @ 所有人都回；能说话的才回；“接着聊一轮”；移出群就不回
    4. 密钥：界面上只看得到开头结尾几位
    加 --real 会真的让本地 Gemma、Claude 和 GPT（你的账号）在群里各回一句（用一点会员额度）

运行方式（在项目文件夹 D:\\voice-assistant 里）：
    .venv\\Scripts\\python.exe test_ui.py
"""

import http.client
import json
import os
import shutil
import sys
import time

import brains
import groupchat
import memory
import settings
import status
import takeover
import ui_server
from login_status import LoginResult

TMP = os.path.join(memory.PROJECT_DIR, "tests", "_tmp_ui")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP)
takeover.available = lambda: False
takeover.brain_claude_code.cached_login = lambda: LoginResult(True, False)
settings.PATH = os.path.join(TMP, "settings.json")
brains.KEYS_FILE = os.path.join(TMP, "api_keys.json")
groupchat.DATA_DIR = os.path.join(TMP, "groupchat")
groupchat.MEMBERS_FILE = os.path.join(groupchat.DATA_DIR, "members.json")
groupchat.HISTORY_FILE = os.path.join(groupchat.DATA_DIR, "history.jsonl")
ui_server.STATE_DIR = os.path.join(TMP, ".ui")
ui_server.SERVER_FILE = os.path.join(ui_server.STATE_DIR, "server.json")
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'[通过]' if ok else '[失败]'} {name}  {detail}")


address = ui_server.start()
port = int(address.split(":")[2].split("/")[0])
token = address.split("t=")[1]


def request(method, path, body=None, token_=token, host="127.0.0.1", ctype="application/json"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    headers = {"Host": f"{host}:{port}"}
    if token_:
        headers["X-Token"] = token_
    if body is not None:
        headers["Content-Type"] = ctype
        body = json.dumps(body)
    conn.request(method, path, body=body, headers=headers)
    r = conn.getresponse()
    data = r.read()
    conn.close()
    try:
        return r.status, json.loads(data)
    except ValueError:
        return r.status, data.decode("utf-8", "replace")


print("===== 1. 安全：只认本机地址 + 口令 =====")
check("只监听本机（127.0.0.1）", ui_server._server.server_address[0] == "127.0.0.1")
check("没有口令：拒绝", request("GET", "/api/state", token_=None)[0] == 403)
check("口令不对：拒绝", request("GET", "/api/state", token_="wrong")[0] == 403)
check("别的网址冒充（DNS 重绑定）：拒绝", request("GET", "/api/state", host="evil.example.com")[0] == 403)
check("不是 JSON 的提交（别的网页表单偷偷提交）：拒绝",
      request("POST", "/api/action/overlay", body={"value": "off"}, ctype="text/plain")[0] == 415)
code, page = request("GET", "/", token_=None)
check("界面网页本身可以打开（里面没有任何数据）", code == 200 and "<title>星期八</title>" in page)
check("不在名单里的文件拿不到（比如 api_keys.json）", request("GET", "/api_keys.json")[0] == 403)

print("\n===== 2. 界面要的数据 =====")
code, st = request("GET", "/api/state")
check("状态：有当前状态、大脑、声音、各项开关", code == 200 and {"status", "brains", "voices", "barge_in", "claude_models"} <= set(st),
      f"→ 状态“{st.get('status', {}).get('state')}”，大脑 {st.get('brain')}")
code, _ = request("POST", "/api/action/overlay", body={"value": "always"})
check("改设置：写进设置文件（这里是临时的）", code == 200 and settings.get("overlay_mode") == "always")
events = []
q = __import__("queue").Queue()
ui_server._clients.append(q)
status.set("思考中", "测试")
status.publish({"type": "turn", "you": "测试", "reply": "好的"})
got = [json.loads(q.get(timeout=2)) for _ in range(2)]
ui_server._clients.remove(q)
check("状态变化、对话内容会实时推给界面", got[0]["type"] == "status" and got[0]["state"] == "思考中"
      and got[1]["type"] == "turn")
code, chat = request("GET", "/api/chat")
check("对话记录能拿到", code == 200 and isinstance(chat, list), f"→ 最近 {len(chat)} 条")

print("\n===== 3. AI 群聊（假 AI）=====")
asked = []


def fake_ask(member, recent, hint=None, **kw):
    asked.append((member["name"], hint, [m["text"] for m in recent]))
    time.sleep(0.05)
    return f"{member['name']}：我是{member['name']}，收到。"


groupchat.ask = fake_ask
groupchat.readiness = lambda m, *, cached_login=False: (
    (True, "") if m.get("kind") != "openai" or groupchat._key(m.get("key")) else (False, "还没填密钥")
)
code, g = request("GET", "/api/group")
names = [m["name"] for m in g["members"]]
check("第一次打开：Claude、GPT（都用你登录的账号）、本地 Gemma 在群里，DeepSeek 先不在",
      names == ["Claude", "GPT", "本地 Gemma", "DeepSeek"] and [m["enabled"] for m in g["members"]] == [True, True, True, False]
      and [m["kind"] for m in g["members"]][:2] == ["claude", "codex"])
groupchat.send("大家好", wait=True)
hist = groupchat.history()
check("不 @：在群里的三个都回了", sorted(n for n, _, _ in asked) == ["Claude", "GPT", "本地 Gemma"]
      and [m["role"] for m in hist] == ["user", "ai", "ai", "ai"])
check("回答开头多出来的“名字：”被去掉", all(not m["text"].startswith(m.get("name", "") + "：") for m in hist if m["role"] == "ai"))
asked.clear()
groupchat.send("@Claude 你怎么看", wait=True)
check("@Claude：只有 Claude 回", [n for n, _, _ in asked] == ["Claude"])
check("Claude 看得到之前的群消息（包括 Gemma 说的）", any("我是本地 Gemma" in t for t in asked[0][2]))
asked.clear()
deepseek = next(m for m in groupchat.members() if m["name"] == "DeepSeek")
groupchat.update_member(deepseek["id"], {"enabled": True})
groupchat.send("@DeepSeek 在吗", wait=True)
check("DeepSeek 拉进群但没填密钥：不回答，群里提示缺密钥",
      not asked and "DeepSeek 没法回答：还没填密钥" in groupchat.history()[-1]["text"])
request("POST", "/api/action/set_key", body={"key": "deepseek", "value": "sk-test-1234567890abcdef"})
groupchat.send("@DeepSeek 在吗", wait=True)
check("填了密钥：DeepSeek 就能回答", [n for n, _, _ in asked] == ["DeepSeek"])
code, g = request("GET", "/api/group")
key = next(k for k in g["keys"] if k["name"] == "deepseek")
check("界面上只看得到密钥开头结尾几位", key["set"] and key["hint"] == "sk-t…cdef" and "1234567890" not in json.dumps(g))
asked.clear()
gemma = next(m for m in groupchat.members() if m["name"] == "本地 Gemma")
groupchat.update_member(gemma["id"], {"enabled": False})
groupchat.update_member(deepseek["id"], {"enabled": False})
groupchat.continue_round(wait=True)
check("“接着聊一轮”：在群里的都再说一句（Gemma 移出群了，不说）",
      sorted(n for n, _, _ in asked) == ["Claude", "GPT"] and all(h for _, h, _ in asked))
r = groupchat.add_member("claude")
check("再拉一个 Claude：自动叫“Claude 2”", r["member"]["name"] == "Claude 2")
check("@Claude 2 只匹配 Claude 2，不会也匹配到 Claude",
      [m["name"] for m in groupchat.mentioned("@Claude 2 你好", groupchat.members())] == ["Claude 2"])
check("@所有人 = 所有人回", groupchat.mentioned("@所有人 早", groupchat.members()) == [])
groupchat.remove_member(r["member"]["id"])
check("删除成员", all(m["name"] != "Claude 2" for m in groupchat.members()))
groupchat.clear()
check("清空聊天：旧记录存档，不真删", not groupchat.history()
      and any(f.startswith("history-") for f in os.listdir(groupchat.DATA_DIR)))
messages = groupchat._chat_messages({"id": "x", "name": "GPT"}, [
    {"role": "user", "text": "你好"}, {"role": "ai", "member_id": "y", "name": "Claude", "text": "你好呀"},
    {"role": "ai", "member_id": "x", "name": "GPT", "text": "嗨"}, {"role": "system", "text": "提示"}], None)
check("发给 AI 的对话：别人说的标名字、它自己说的算 assistant、最后一条是 user",
      [m["role"] for m in messages] == ["user", "assistant", "user"] and "【Claude】：你好呀" in messages[0]["content"])

if "--real" in sys.argv:
    print("\n===== 4. 真的让 AI 在群里说一句 =====")
    import importlib
    importlib.reload(groupchat)
    groupchat.DATA_DIR = os.path.join(TMP, "groupchat_real")
    groupchat.MEMBERS_FILE = os.path.join(groupchat.DATA_DIR, "members.json")
    groupchat.HISTORY_FILE = os.path.join(groupchat.DATA_DIR, "history.jsonl")
    for m in groupchat.members():
        if m["preset"] == "claude":
            groupchat.update_member(m["id"], {"model": "haiku"})  # 测试用最便宜的
    t = time.time()
    groupchat.send("大家好，用一句话介绍一下你自己", wait=True)
    for m in groupchat.history()[1:]:
        print(f"  {m.get('name', '系统')}：{m['text']}（{m.get('seconds', '-')} 秒）")
    replies = [m for m in groupchat.history() if m["role"] == "ai"]
    check("Claude、GPT（ChatGPT 账号）和本地 Gemma 都在群里回了一句", len(replies) == 3,
          f"→ 一共用了 {time.time() - t:.1f} 秒")

ui_server.stop()
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n结果：{sum(results)}/{len(results)} 项通过")
