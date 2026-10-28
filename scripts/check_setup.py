"""Check your keys and list the Nemotron models your Token Factory account can use.

Usage: python -m scripts.check_setup
"""
import httpx

from app import config
from app.llm import chat_json, client

if __name__ == "__main__":
    print(f"Base URL: {config.NEBIUS_BASE_URL}")
    ids = sorted(m.id for m in client().models.list().data)
    nemotron = [i for i in ids if "nemotron" in i.lower()]
    print("Nemotron models available:\n  " + "\n  ".join(nemotron or ["(none found)"]))
    for name, model in [("MODEL_FAST", config.MODEL_FAST), ("MODEL_REASON", config.MODEL_REASON)]:
        status = "OK" if model in ids else "NOT FOUND, copy an ID from the list above"
        print(f"{name} = {model}: {status}")
        if model in ids:
            data, usage = chat_json(model, "Reply in JSON.", 'Return {"ok": true}', max_tokens=1000,
                                    thinking=name == "MODEL_REASON")
            print(f"   test call -> {data}  tokens={usage}")
    if config.TAVILY_API_KEY:
        r = httpx.post("https://api.tavily.com/search", timeout=30,
                       headers={"Authorization": f"Bearer {config.TAVILY_API_KEY}"},
                       json={"query": "statins dementia confounding", "max_results": 1})
        print(f"Tavily: HTTP {r.status_code}")
    else:
        print("Tavily: TAVILY_API_KEY not set")
