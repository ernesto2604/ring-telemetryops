import os
import json
import boto3
from dotenv import load_dotenv

load_dotenv()

client = boto3.client(
    service_name="bedrock-runtime",
    region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1").strip(),
    aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", "").strip(),
    aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", "").strip()
)

model_id = os.getenv("BEDROCK_MODEL_ID").strip()

payload = {
    "anthropic_version": "bedrock-2023-05-31",
    "max_tokens": 50,
    "messages": [
        {"role": "user", "content": "Hello Claude, confirm connection by replying only: CONNECTION SUCCESSFUL"}
    ]
}

try:
    print(f"Testing inference with model: {model_id}...")
    response = client.invoke_model(
        modelId=model_id,
        body=json.dumps(payload),
        contentType="application/json",
        accept="application/json"
    )
    result = json.loads(response["body"].read())
    print("\nBedrock Response:")
    print(result["content"][0]["text"])
except Exception as e:
    print("\nError invoking Bedrock:")
    print(e)