"""A backend-only Satori client; the credential represents delegated execution."""

import argparse
import asyncio
import json
import mimetypes
import uuid
from html import escape
from pathlib import Path

import httpx
import websockets


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--image", type=Path, action="append", default=[], help="send as an image (repeatable)")
    parser.add_argument("--file", type=Path, action="append", default=[], help="send as a file (repeatable)")
    parser.add_argument("message")
    args = parser.parse_args()
    token = args.token_file.read_text().strip()
    base = f"{args.url.rstrip('/')}/api/satori/agents/{args.agent}/v1"
    headers = {"Authorization": "Bearer " + token, "Satori-Platform": "astrabox", "Satori-User-ID": args.agent}
    async with httpx.AsyncClient(headers=headers, timeout=30) as client:
        async def call(method: str, body: dict, *, key: str | None = None):
            response = await client.post(base + "/" + method, json=body,
                                         headers={"Idempotency-Key": key} if key else {})
            response.raise_for_status()
            return response.json()
        prepared = await call("internal/astrabox.preparation.get", {})
        if not prepared["ready"]:
            print(json.dumps(prepared, ensure_ascii=False))
            return
        channel = await call("channel.create", {"guild_id": args.agent, "data": {"type": 0}}, key=uuid.uuid4().hex)
        url = base.replace("https://", "wss://").replace("http://", "ws://") + "/events"
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({"op": 3, "body": {"token": token}}))
            ready = json.loads(await ws.recv())
            assert ready["op"] == 4, ready
            content = escape(args.message)
            sent = [("img", path) for path in args.image] + [("file", path) for path in args.file]
            if sent:
                # One upload for every file; each part name maps to its resource link.
                parts = [(f"part{index}", (path.name, path.read_bytes(),
                                            mimetypes.guess_type(path.name)[0] or "application/octet-stream"))
                         for index, (_tag, path) in enumerate(sent)]
                response = await client.post(base + "/upload.create", files=parts)
                response.raise_for_status()
                links = response.json()
                content += "".join(
                    f'<{tag} src="{escape(links[f"part{index}"])}" title="{escape(path.name)}"/>'
                    for index, (tag, path) in enumerate(sent)
                )
            await call("message.create", {"channel_id": channel["id"], "content": content}, key=uuid.uuid4().hex)
            async def heartbeat():
                while True:
                    await asyncio.sleep(8)
                    await ws.send(json.dumps({"op": 1}))
            beat = asyncio.create_task(heartbeat())
            try:
                async for raw in ws:
                    event = json.loads(raw)
                    if event.get("op") != 0 or event["body"].get("channel", {}).get("id") != channel["id"]:
                        continue
                    print(json.dumps(event["body"], ensure_ascii=False))
                    if event["body"].get("_type") == "astrabox.state":
                        response = event["body"]["_data"].get("response") or {}
                        if response.get("status") in {"ended", "waiting"}:
                            break
            finally:
                beat.cancel()
                await asyncio.gather(beat, return_exceptions=True)
        # Read the conversation back, and download every file it names.
        history = await call("message.list", {"channel_id": channel["id"]})
        for item in history["data"]:
            for link in _resource_links(item["content"]):
                download = await client.get(base + "/proxy/" + link)
                download.raise_for_status()
                print(json.dumps({"message": item["id"], "link": link, "bytes": len(download.content),
                                  "type": download.headers.get("content-type")}))


def _resource_links(content: str) -> list[str]:
    from satori.element import File, Image
    from satori.model import MessageObject

    return [element.src for element in MessageObject(id="", content=content).message
            if isinstance(element, (Image, File))]


if __name__ == "__main__":
    asyncio.run(main())
