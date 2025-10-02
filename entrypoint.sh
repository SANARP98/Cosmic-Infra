#!/bin/sh
# Default entrypoint for Python project runners

# If user specifies FILE in env, use it, else fall back to main.py
TARGET_FILE=${TARGET_FILE:-main.py}

if [ -f "$TARGET_FILE" ]; then
    echo "Running $TARGET_FILE with uv..."
    uv run python "$TARGET_FILE"
else
    echo "No $TARGET_FILE found, idling..."
    tail -f /dev/null
fi
