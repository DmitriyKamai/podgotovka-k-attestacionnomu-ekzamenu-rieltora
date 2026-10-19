import httpx

from config import GO_API_KEY, GO_BASE_URL, MODEL, USER_AGENT


class LLMError(Exception):
    pass


async def chat(messages, session_id: str, max_tokens: int = 3000, temperature: float = 0.2) -> str:
    if not GO_API_KEY:
        raise LLMError("GO_API_KEY не задан в .env")
    headers = {
        "Authorization": f"Bearer {GO_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "x-opencode-session": session_id,
    }
    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    url = f"{GO_BASE_URL.rstrip('/')}/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(url, headers=headers, json=payload)
        if r.status_code != 200:
            raise LLMError(f"HTTP {r.status_code}: {r.text[:400]}")
        data = r.json()
        msg = data["choices"][0]["message"]
        content = (msg.get("content") or "").strip()
        if not content:
            # reasoning-модель могла израсходовать лимит на reasoning_content
            content = (msg.get("reasoning_content") or "").strip()
        return content
    except httpx.HTTPError as e:
        raise LLMError(f"Сетевая ошибка: {e}")
    except (KeyError, IndexError) as e:
        raise LLMError(f"Неожиданный ответ API: {e}")
