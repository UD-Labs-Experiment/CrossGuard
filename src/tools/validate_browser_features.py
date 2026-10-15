"""Observe the live bridge; caller starts a trial before running this check."""
import asyncio
import json
from pathlib import Path
import urllib.request
import websockets


def command(action,params=None):
    data=json.dumps({"action":action,"params":params or {}}).encode()
    request=urllib.request.Request("http://127.0.0.1:8000/api/command",data,
                                   {"Content-Type":"application/json"})
    with urllib.request.urlopen(request,timeout=120) as response:
        return json.load(response)


def status():
    with urllib.request.urlopen("http://127.0.0.1:8000/api/status",timeout=5) as response:
        return json.load(response)


async def main():
    destination=Path(__file__).resolve().parents[1]/"results"/"browser_feature_validation"
    destination.mkdir(parents=True,exist_ok=True)
    last=None
    async with websockets.connect("ws://127.0.0.1:8000/ws",max_size=12_000_000) as connection:
        if status()["state"] in ("finished","error","stopped"):
            await asyncio.to_thread(command,"reset")
        for _ in range(600):
            packet=json.loads(await asyncio.wait_for(connection.recv(),timeout=30))
            if packet.get("type")!="frame":
                continue
            last=packet
            if packet["sim_time_s"]>=8:
                break
    assert last is not None
    metrics=last["metrics"]
    assert metrics["receiver_mode"]=="retrofit",metrics["receiver_mode"]
    phone=metrics["communication"]["phone"]["events"]
    assert any(e["event"]=="reacted" for e in phone),phone
    assert any(v.get("phone_state")=="responding" for v in metrics["group_vehicles"].values()),metrics["group_vehicles"]
    command("pause")
    before=status()
    command("view",{"name":"driver_b"})
    after=status()
    assert before["sim_time_s"]==after["sim_time_s"],(before,after)
    assert after["state"]=="paused" and after["current_view"]=="driver_b"
    # Remove the large image string; the camera smoke stores image evidence.
    last.pop("image",None)
    summary={"status":"passed","kind":"Live retrofit reaction and paused-view protocol check",
             "packet":last,"pause_before":before,"pause_after":after}
    (destination/"summary.json").write_text(json.dumps(summary,indent=2))
    print(json.dumps({"status":"passed","summary":str(destination/"summary.json")}),flush=True)


if __name__=="__main__":
    asyncio.run(main())
