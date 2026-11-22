# Calculator

Minimal FastAPI calculator accepting arithmetic expressions as strings.

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 127.0.0.1 --port 8000
```

## Test

```bash
curl -X POST http://127.0.0.1:8000/calculate -H 'Content-Type: application/json' -d '{"expression":"12+4*3"}'
```

Expected response: `{"result":24}`.
