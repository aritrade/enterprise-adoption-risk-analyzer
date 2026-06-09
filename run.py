"""Entry point for the Adoption & Escalation Risk Analyzer."""
from pathlib import Path

from dotenv import load_dotenv

# Load .env into os.environ before uvicorn imports the app, so non-pydantic
# vars like HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE are honoured by libraries
# that read os.environ directly (huggingface_hub, transformers, …).
load_dotenv(Path(__file__).resolve().parent / ".env")

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
