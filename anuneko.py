import json
import os
import httpx
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message
from nonebot.params import CommandArg

# ============================================================
# API 地址
# ============================================================

CHAT_API_URL = "https://anuneko.com/api/v1/chat"
STREAM_API_URL = "https://anuneko.com/api/v1/msg/{uuid}/stream"
SELECT_CHOICE_URL = "https://anuneko.com/api/v1/msg/select-choice"
SELECT_MODEL_URL = "https://anuneko.com/api/v1/user/select_model"

DEFAULT_TOKEN = (
    "CAEaJGMwOWE3YjFjLTVmOWEtNGE0Zi1iOWI1LWQyNDAyZjQzNzlmYSDF7r7JBii13YShBzCdgsCg-rvT9Qs6BG5la28.RbcvaQAAAAAAAg.MEUCIQCx9bAl-l6HjYbAC8UfyZXeerPcu5j6-B85lfDM72S0CwIgIum0RAvL8_d-5BiDQp1lDM-6Xcgjs02vG1npCD70UeE"
)

# 每个 QQ 用户一个独立会话
user_sessions = {}
# 每个 QQ 用户的当前模型
user_models = {}  # user_id: "Orange Cat" or "Exotic Shorthair"

WATERMARK = ""


# ============================================================
#             异步 httpx 请求封装
# ============================================================

def build_headers():
    token = os.environ.get("ANUNEKO_TOKEN", DEFAULT_TOKEN)
    cookie = os.environ.get("ANUNEKO_COOKIE")

    headers = {
        "accept": "*/*",
        "content-type": "application/json",
        "origin": "https://anuneko.com",
        "referer": "https://anuneko.com/",
        "user-agent": "Mozilla/5.0",
        "x-app_id": "com.anuttacon.neko",
        "x-client_type": "4",
        "x-device_id": "7b75a432-6b24-48ad-b9d3-3dc57648e3e3",
        "x-token": token,
    }

    if cookie:
        headers["Cookie"] = cookie

    return headers


# ============================================================
#          创建新会话（使用 httpx 异步重写）
# ============================================================

async def create_new_session(user_id: str):
    headers = build_headers()
    model = user_models.get(user_id, "Orange Cat")
    data = json.dumps({"model": model})

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(CHAT_API_URL, headers=headers, content=data)
            resp_json = resp.json()

        chat_id = resp_json.get("chat_id") or resp_json.get("id")
        if chat_id:
            user_sessions[user_id] = chat_id
            # 切换模型以确保一致性
            await switch_model(user_id, chat_id, model)
            return chat_id

    except Exception:
        return None

    return None


# ============================================================
#      切换模型（async）
# ============================================================

async def switch_model(user_id: str, chat_id: str, model_name: str):
    headers = build_headers()
    data = json.dumps({"chat_id": chat_id, "model": model_name})

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(SELECT_MODEL_URL, headers=headers, content=data)
            if resp.status_code == 200:
                user_models[user_id] = model_name
                return True
    except:
        pass
    return False


# ============================================================
#      自动选分支（async）
# ============================================================

async def send_choice(msg_id: str):
    headers = build_headers()

    data = json.dumps({"msg_id": msg_id, "choice_idx": 0})

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(SELECT_CHOICE_URL, headers=headers, content=data)
    except:
        pass


# ============================================================
#      核心：异步流式回复（超级稳定）
# ============================================================

async def stream_reply(session_uuid: str, text: str) -> str:
    headers = {
        "x-token": os.environ.get("ANUNEKO_TOKEN", DEFAULT_TOKEN),
        "Content-Type": "text/plain",
    }

    cookie = os.environ.get("ANUNEKO_COOKIE")
    if cookie:
        headers["Cookie"] = cookie

    url = STREAM_API_URL.format(uuid=session_uuid)
    data = json.dumps({"contents": [text]}, ensure_ascii=False)

    result = ""
    current_msg_id = None

    try:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST", url, headers=headers, content=data
            ) as resp:
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    
                    # 处理错误响应
                    if not line.startswith("data: "):
                        try:
                            error_json = json.loads(line)
                            if error_json.get("code") == "chat_choice_shown":
                                return "⚠️ 检测到对话分支未选择，请重试或新建会话。"
                        except:
                            pass
                        continue

                    # 处理 data: {}
                    try:
                        raw_json = line[6:]
                        if not raw_json.strip():
                            continue
                            
                        j = json.loads(raw_json)

                        # 只要出现 msg_id 就更新，流最后一条通常是 assistmsg，也就是我们要的 ID
                        if "msg_id" in j:
                            current_msg_id = j["msg_id"]

                        # 如果有 'c' 字段，说明是多分支内容
                        # 格式如: {"c":[{"v":"..."},{"v":"...","c":1}]}
                        if "c" in j and isinstance(j["c"], list):
                            for choice in j["c"]:
                                # 默认选项 idx=0，可能显式 c=0 或隐式(无 c 字段)
                                idx = choice.get("c", 0)
                                if idx == 0:
                                    if "v" in choice:
                                        result += choice["v"]
                        
                        # 常规内容 (兼容旧格式或无分支情况)
                        elif "v" in j and isinstance(j["v"], str):
                            result += j["v"]

                    except:
                        continue
        
        # 流结束后，如果有 msg_id，自动确认选择第一项，确保下次对话正常
        if current_msg_id:
            await send_choice(current_msg_id)

    except Exception:
        return "请求失败，请稍后再试。"

    return result


# ============================================================
#                NoneBot 指令
# ============================================================

chat_cmd = on_command("chat", aliases={"对话"})
new_cmd = on_command("new", aliases={"新会话"})
switch_cmd = on_command("switch", aliases={"切换"})


# ---------------------------
#   /switch 切换模型
# ---------------------------

@switch_cmd.handle()
async def _(event, args: Message = CommandArg()):
    user_id = str(event.user_id)
    arg = args.extract_plain_text().strip()

    if "橘猫" in arg or "orange" in arg.lower():
        target_model = "Orange Cat"
        target_name = "橘猫"
    elif "黑猫" in arg or "exotic" in arg.lower():
        target_model = "Exotic Shorthair"
        target_name = "黑猫"
    else:
        await switch_cmd.finish("请指定要切换的模型：橘猫 / 黑猫")
        return

    # 获取当前会话ID，如果没有则新建
    if user_id not in user_sessions:
        chat_id = await create_new_session(user_id)
        if not chat_id:
             await switch_cmd.finish("❌ 切换失败：无法创建会话")
             return
    else:
        chat_id = user_sessions[user_id]

    success = await switch_model(user_id, chat_id, target_model)
    
    if success:
        await switch_cmd.finish(f"✨ 已切换为：{target_name}")
    else:
        await switch_cmd.finish(f"❌ 切换为 {target_name} 失败")


# ---------------------------
#   /new 创建新会话
# ---------------------------

@new_cmd.handle()
async def _(event):
    user_id = str(event.user_id)

    new_id = await create_new_session(user_id)

    if new_id:
        model_name = "橘猫" if user_models.get(user_id) == "Orange Cat" else "黑猫"
        await new_cmd.finish(f"✨ 已创建新的会话（当前模型：{model_name}）！")
    else:
        await new_cmd.finish("❌ 创建会话失败，请稍后再试。")


# ---------------------------
#   /chat 进行对话
# ---------------------------

@chat_cmd.handle()
async def _(event, args: Message = CommandArg()):
    user_id = str(event.user_id)
    text = args.extract_plain_text().strip()

    if not text:
        await chat_cmd.finish("❗ 请输入内容，例如：/chat 你好")

    # 自动创建会话
    if user_id not in user_sessions:
        cid = await create_new_session(user_id)
        if not cid:
            await chat_cmd.finish("❌ 创建会话失败，请稍后再试。")

    session_id = user_sessions[user_id]
    reply = await stream_reply(session_id, text)

    reply += WATERMARK

    await chat_cmd.finish(reply)
