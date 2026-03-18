import asyncio
from pathlib import Path

from ociapp_runtime import Runtime


async def main() -> None:
    artifact_path = Path("dist/echo-app-0.1.0.ociapp")
    async with Runtime(idle_timeout=5.0) as runtime:
        first = await runtime.execute(artifact_path, {"value": "hello"})
        second = await runtime.execute(artifact_path, {"value": "world"})
        print(first)
        print(second)


if __name__ == "__main__":
    asyncio.run(main())
