import asyncio
from pathlib import Path

from ociapp_runtime import Runtime


async def main() -> None:
    artifact_path = Path("dist/echo-app-0.1.0.ociapp")
    async with Runtime(idle_timeout=5) as runtime:
        first = await runtime.execute(artifact_path, {"value": "hello"})
        print(first)
        second = await runtime.execute(artifact_path, {"value": "world"})
        print(second)
        third = await runtime.execute(artifact_path, {"value": "!"})
        print(third)
        fourth = await runtime.execute(artifact_path, {"value": "!!"})
        print(fourth)
        await asyncio.sleep(6)
        fifth = await runtime.execute(artifact_path, {"value": "hello again!"})
        print(fifth)


if __name__ == "__main__":
    asyncio.run(main())
