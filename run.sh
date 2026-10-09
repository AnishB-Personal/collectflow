#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"
exec ./venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
