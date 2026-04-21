"""Shared AWS Bedrock Claude call."""

import os

import requests

DEFAULT_MODEL = "us.anthropic.claude-sonnet-4-20250514-v1:0"


def call_bedrock(system_prompt, user_prompt, max_tokens=2048, model=None):
    region = os.environ["AWS_REGION"]
    token = os.environ["AWS_BEARER_TOKEN_BEDROCK"]
    model_id = model or os.environ.get("BEDROCK_MODEL", DEFAULT_MODEL)

    url = f"https://bedrock-runtime.{region}.amazonaws.com/model/{model_id}/converse"
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={
            "system": [{"text": system_prompt}],
            "messages": [
                {"role": "user", "content": [{"text": user_prompt}]},
            ],
            "inferenceConfig": {"maxTokens": max_tokens},
        },
    )
    if not resp.ok:
        print(f"Bedrock API error {resp.status_code}: {resp.text}")
        resp.raise_for_status()
    return resp.json()["output"]["message"]["content"][0]["text"]
