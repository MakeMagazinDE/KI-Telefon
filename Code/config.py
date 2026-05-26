import os

# Motordriver configuration: "L298N" (classic setup, default) or "DRV8871" (optimized setup)
MOTORDRIVER = os.environ.get("MOTORDRIVER", "L298N")

# Try to fetch from environment variables first (production standard), fallback to the string for local maker-tests
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

if not OPENAI_API_KEY:
    OPENAI_API_KEY = "HIER OPENAI API KEY EINTRAGEN"
