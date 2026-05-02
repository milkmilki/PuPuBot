import requests

url = "https://xinghuapi.com/v1/messages"

headers = {
    "Content-Type": "application/json",
    "x-api-key": "你的API_KEY",
    "anthropic-version": "sk-jacLOQDhdRufr1RokATNunxp7u93J6UwvdP686rkJcLoLwCU"
}

data = {
    "model": "claude-opus-4-6",
    "max_tokens": 50,
    "messages": [
        {"role": "user", "content": "ping"}
    ]
}

response = requests.post(url, headers=headers, json=data)

print(response.status_code)
print(response.text)