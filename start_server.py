# /// script
# requires-python = ">=3.9"
# dependencies = ["fastapi", "uvicorn", "pydantic"]
# ///
import uvicorn
import bot
uvicorn.run(bot.app, host="0.0.0.0", port=8082)
