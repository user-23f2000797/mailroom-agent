"""
Quick standalone check that your OPENAI_API_KEY / OPENAI_MODEL actually
work, before wiring up the full agent. Run:

    export OPENAI_API_KEY=your_key_here
    python3 test_openai_connection.py

If this succeeds, `app/ai.py` will work as-is. If it fails, the error
message will tell you whether it's an auth problem (wrong/expired key,
no billing set up) or a model problem (wrong model name / no access to it
on your account).
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from app.ai import classify_dossier  # noqa: E402


async def main():
    if not os.environ.get("OPENAI_API_KEY"):
        print("ERROR: set OPENAI_API_KEY first, e.g.:\n  export OPENAI_API_KEY=your_key_here")
        sys.exit(1)

    test_dossier = {
        "dossierId": "test-1",
        "subject": "Quick question about my order",
        "body": "Hi, I ordered a widget last week and it hasn't arrived yet. Can you check on it?",
        "sender": "customer@example.com",
    }

    print(f"Calling api.openai.com with model {os.environ.get('OPENAI_MODEL', 'gpt-4o-mini')} ...")

    result = await classify_dossier(test_dossier)
    print("\nResult:")
    print(result)

    if result["action"] == "request_confirmation" and "model_error" in str(result.get("payload", {}).get("reason", "")):
        print("\n^^ That's the safe-fallback path, meaning the real API call FAILED. "
              "Check the reason string above and your OPENAI_API_KEY / OPENAI_MODEL.")
        sys.exit(1)
    else:
        print("\nSUCCESS: OpenAI API connection is working.")


if __name__ == "__main__":
    asyncio.run(main())
