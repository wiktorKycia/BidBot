import json
from pathlib import Path

import aiofiles


async def save_json(filepath: Path, data: dict):
    async with aiofiles.open(filepath, mode="w", encoding="utf-8") as f:
        json_data = json.dumps(data, ensure_ascii=False, indent=2)
        await f.write(json_data)


async def save_to_file(filepath: Path, data: str):
    async with aiofiles.open(filepath, mode="w", encoding="utf-8") as f:
        await f.write(data)
