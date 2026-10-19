"""Submit or observe a configured peer's A2A task without owning its execution."""

import argparse
import json
import urllib.request

from hermes_cli.config import load_config_readonly


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("submit", "watch"))
    parser.add_argument("--peer", required=True)
    parser.add_argument("--message-id")
    parser.add_argument("--message")
    parser.add_argument("--context-id")
    parser.add_argument("--task-id")
    args = parser.parse_args()
    if not args.peer.startswith("astrabox/"):
        parser.error("select a configured astrabox/ peer from a2a_list")
    if args.action == "submit" and (not args.message_id or not args.message):
        parser.error("submit requires --message-id and --message")
    if args.action == "watch" and not args.task_id:
        parser.error("watch requires --task-id")
    peer = load_config_readonly()["a2a_agents"][args.peer]
    headers = {"Authorization": "Bearer " + peer["auth"]["token"], "A2A-Version": "1.0"}
    timeout = peer.get("timeout", 120)

    def open_request(url: str, body: dict | None = None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **headers})
        return urllib.request.urlopen(req, timeout=timeout)

    def request(url: str, body: dict | None = None) -> dict:
        with open_request(url, body) as response:
            return json.load(response)

    card = request(peer["url"].rstrip("/") + "/.well-known/agent-card.json")
    interface = next(item for item in card["supportedInterfaces"] if item["protocolBinding"] == "JSONRPC")

    def rpc(method: str, params: dict) -> dict:
        response = request(interface["url"], {"jsonrpc": "2.0", "id": method, "method": method, "params": params})
        if "error" in response:
            raise RuntimeError(json.dumps(response["error"]))
        result = response["result"]
        return result["task"] if method == "SendMessage" else result

    if args.action == "submit":
        message = {"messageId": args.message_id, "role": "ROLE_USER", "parts": [{"text": args.message}]}
        if args.context_id:
            message["contextId"] = args.context_id
        task = rpc("SendMessage", {"message": message, "configuration": {"returnImmediately": True}})
    else:
        task = rpc("GetTask", {"id": args.task_id})
        active = {"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"}
        if task["status"]["state"] in active:
            headers["Accept"] = "text/event-stream"
            body = {"jsonrpc": "2.0", "id": "watch", "method": "SubscribeToTask", "params": {"id": args.task_id}}
            with open_request(interface["url"], body) as response:
                data: list[str] = []
                for raw in response:
                    line = raw.decode().rstrip("\r\n")
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip())
                    elif not line and data:
                        event = json.loads("\n".join(data))
                        data.clear()
                        if "error" in event:
                            raise RuntimeError(json.dumps(event["error"]))
                        result = event["result"]
                        if "task" in result:
                            task = result["task"]
                        elif "statusUpdate" in result:
                            task["status"] = result["statusUpdate"]["status"]
                        if task["status"]["state"] not in active:
                            break
            if task["status"]["state"] in active:
                raise RuntimeError("Observation ended while the task was active; read the same task before watching again")
    print(json.dumps({key: task[key] for key in ("id", "contextId", "status", "metadata")}))


if __name__ == "__main__":
    main()
