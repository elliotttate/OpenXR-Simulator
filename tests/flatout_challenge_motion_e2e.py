"""Observe native FlatOut Mode page animation during a screenshot-derived ray press.

Start a fresh instrumented Fox build in INSANITY, page zero, with motion enabled
and an isolated simulator profile. Never starts an event or writes game memory.
The HUD samples qualify temporal opacity, not GPU frame time or headset comfort.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import uuid

import numpy as np
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from flatout_frontend_e2e import Frontend, ROOT
from flatout_ray_offset import native_pointer_offset
sys.path.insert(0,str(ROOT/'tools'))
from flatout_ui_atlas import NativeGame


def require(condition, message, detail=None):
    if not condition:
        raise AssertionError((message, detail) if detail is not None else message)


def hud_sample(hud, timestamp):
    """Keep only the actual rendered nodes needed for the temporal oracle."""
    nodes = hud["nodes"]
    roots = [n for n in nodes if n["path"].endswith("/ChallengeReader")]
    photos = sorted((n for n in nodes if n["path"].endswith("/ChallengeTrackArt")),
                    key=lambda n: n["rect"][0])
    cards = sorted((n for n in nodes if n["path"].endswith("/ChallengeCard")),
                   key=lambda n: n["rect"][0])
    require(len(roots) == 1 and len(photos) == 3 and len(cards) == 3,
            "Expected one native reader, three photos and three stable cards",
            [len(roots), len(photos), len(cards)])
    return {"time": timestamp, "root_alpha": roots[0]["color"]["a"],
            "photo_alphas": [n["color"]["a"] for n in photos],
            "photo_rects": [n["rect"] for n in photos],
            "card_rects": [n["rect"] for n in cards],
            "root_rect": roots[0]["rect"],
            "titles": [n.get("text", "") for n in sorted(
                (n for n in nodes if n["path"].endswith("/ChallengeEventTitle")),
                key=lambda n: n["rect"][0])]}


def target_sample(native):
    targets = native["targets"]
    return {"clicks": native["clicks"],
            "cards": sorted((t["rect"] for t in targets
                             if t["name"].startswith("challenge.event.")), key=lambda r: r[0]),
            "actions": {t["name"]: t["rect"] for t in targets
                        if t["name"].startswith(("challenge.page.", "helper."))}}


def capture_carrier(capture, pid):
    """Quad-only menus have no native vr_frame; inspect submitted XR geometry."""
    geometry = capture["geometry"]
    # The actual runtime publishes PID-tick-serial inside geometry; capture_id
    # is an opaque MCP identifier, not a process claim.
    owner = re.fullmatch(r"(\d+)-\d+-\d+", geometry.get("session_id", ""))
    require(owner is not None and int(owner.group(1)) == pid,
            "MCP capture belongs to a different game process", geometry.get("session_id"))
    require(geometry.get("supported") is True, "Capture geometry unsupported")
    planes = [s for s in geometry["surfaces"] if s["visibility"] == 0]
    require(planes, "Native challenge requires a submitted BOTH-eye UI carrier")
    # A promoted hover face may coexist with the fixed full-menu quad. Rays
    # and controller images are decorations, never candidate menu carriers.
    plane = max(planes, key=lambda s: s["size"][0] * s["size"][1])
    return {"capture_id": capture["capture_id"], "session_id": geometry["session_id"],
            "frame_id": geometry["frame_id"], "sync_id": geometry["sync_id"],
            "carrier": {k: plane[k] for k in ("pose", "size")}}


def check_geometry(before, after):
    require(before["cards"] == after["cards"], "Ray card bounds drifted during animation")
    # Prev may appear on page one; preview may become unavailable for locked
    # cards. Every action that remains available must retain its exact hit box.
    for name in before["actions"].keys() & after["actions"].keys():
        require(before["actions"][name] == after["actions"][name],
                "Ray action bounds drifted during animation", name)
    for prefix in ("helper.Cancel.", "helper.Secondary."):
        names = [n for n in before["actions"] if n.startswith(prefix)]
        require(len(names) == 1 and names[0] in after["actions"],
                "Back/Leaderboard availability flickered", prefix)


def check_carrier(before, after):
    require(before["session_id"] == after["session_id"], "Runtime session changed during regression")
    require(after["frame_id"] >= before["frame_id"], "Carrier evidence came from an older frame")
    a, b = before["carrier"], after["carrier"]
    require(np.allclose(a["size"], b["size"], rtol=0, atol=1e-5), "Carrier size changed")
    require(np.allclose(a["pose"]["position"], b["pose"]["position"], rtol=0, atol=.001),
            "Carrier distance/height changed")
    # q and -q describe the same rotation.
    qa, qb = np.array(a["pose"]["orientation"]), np.array(b["pose"]["orientation"])
    require(min(np.max(np.abs(qa-qb)), np.max(np.abs(qa+qb))) <= 1e-5,
            "Carrier orientation changed")


def qualify_fade(samples):
    tolerance = 1.1 / 255
    start = next((i for i, s in enumerate(samples) if min(s["photo_alphas"]) < .995), None)
    require(start is not None, "Fade was not temporally observed; native sampling cannot qualify it")
    ramp = samples[start:]
    require(len(ramp) >= 3, "Too few native samples to qualify the fade")
    previous = .60
    distinct = set()
    for sample in ramp:
        values = sample["photo_alphas"]
        require(min(values) >= .60 and max(values) <= 1 + tolerance,
                "Page ink became unreadable or exceeded full opacity", values)
        require(max(values)-min(values) <= tolerance, "Card fades lost synchronization", values)
        alpha = sum(values)/len(values)
        require(alpha + tolerance >= previous, "Page fade reversed or restarted", [previous, alpha])
        previous = alpha
        distinct.add(round(alpha * 255))
    require(len(distinct) >= 3 and previous >= .995,
            "Need at least three distinct fade levels ending fully visible", sorted(distinct))
    return {"sample_count": len(ramp), "distinct_alpha_levels": sorted(distinct),
            "first_observed_alpha": sum(ramp[0]["photo_alphas"])/3,
            "observed_seconds": ramp[-1]["time"]-ramp[0]["time"],
            "note": "Read-only native HUD opacity samples; no GPU timing or pixel-video claim"}


class NativeMotionSampler:
    """Own read-only pipe client; never depends on asyncio/image serialization."""
    def __init__(self,pid,depth,native=None):
        self.operations=[]
        self.native=native if native is not None else NativeGame(pid,self.operations,timeout=3)
        self.depth=depth
        self.samples=[]
        self.geometry_samples=[]
        self.lock=threading.Lock()
        self.ready=threading.Event()
        self.stop=threading.Event()
        self.error=None
        self.thread=threading.Thread(target=self.collect,name='native-ui-motion-sampler',daemon=True)

    def read(self,method,params=None):
        require(method in ('hud.rects','vr.targets'),'Motion sampler only accepts read-only observations',method)
        return self.native.call(method,params)

    def collect(self):
        deadline=time.monotonic()+8
        fade_levels=set()
        fade_seen=False
        try:
            while not self.stop.is_set() and time.monotonic()<deadline:
                sample=hud_sample(self.read('hud.rects',{'root':'menu','max_depth':self.depth}),time.monotonic())
                with self.lock:
                    self.samples.append(sample)
                    count=len(self.samples)
                self.ready.set()
                alpha=sum(sample['photo_alphas'])/3
                if alpha<.995:
                    fade_seen=True
                    fade_levels.add(round(alpha*255))
                # Keep the first three ink levels clear of other native queries.
                # Every HUD sample includes the stable reader/card/photo boxes.
                acquired=len(fade_levels)>=3 or (fade_seen and alpha>=.995)
                if acquired and (not self.geometry_samples or count%4==0):
                    geometry=target_sample(self.read('vr.targets'))
                    with self.lock:self.geometry_samples.append(geometry)
                self.stop.wait(.002)
        except Exception as error:
            self.error=error
        finally:
            self.ready.set()

    def snapshot(self):
        with self.lock:return list(self.samples),list(self.geometry_samples)


class ChallengeMotion(Frontend):
    def __init__(self, args, session):
        super().__init__(args, session)
        self.samples, self.geometry_samples = [], []
        self.carrier_samples = []
        self.cleanup_errors = []
        self.monitor = None
        self.hud_depth = 48

    async def tool(self, name, args, label):
        value = await super().tool(name, args, label)
        capture = value if "geometry" in value else value.get("capture")
        if capture:
            observation = {"label": label, **capture_carrier(capture, self.args.pid)}
            if self.carrier_samples:
                check_carrier(self.carrier_samples[0], observation)
            self.carrier_samples.append(observation)
            self.last_capture = capture
        return value

    async def release(self, hand):
        if self.last_capture:
            await self.tool("release_ui_pointer", {"capture_id": self.last_capture["capture_id"],
                            "hand": hand, "timeout": 15}, "release-"+hand)

    async def both_eyes(self, label):
        self.last_capture = await self.tool("capture_screenshot", {
            "eye": "both", "max_width": 2048, "timeout": 30}, label)
        return self.last_capture

    async def sample(self):
        hud = await self.query("hud.rects", {"root": "menu", "max_depth": self.hud_depth})
        sample = hud_sample(hud, time.monotonic())
        # Discover the real hierarchy once, then omit deep Flash art children
        # from temporal reads. This keeps diagnostics out of the 140ms budget.
        suffixes = ("/ChallengeReader", "/ChallengeCard", "/ChallengeTrackArt", "/ChallengeEventTitle")
        self.hud_depth = max(n["depth"] for n in hud["nodes"] if n["path"].endswith(suffixes))
        return sample

    async def stop_monitor(self):
        if self.monitor:
            self.monitor.stop.set()
            await asyncio.to_thread(self.monitor.thread.join,8)
            self.samples,self.geometry_samples=self.monitor.snapshot()
            require(not self.monitor.thread.is_alive(),'Background native sampler did not stop within8seconds')
            if self.monitor.error:raise RuntimeError('Native sampler failed: '+repr(self.monitor.error)) from self.monitor.error

    async def run(self):
        errors = await self.query("game.errors")
        require(errors["total"] == 0, "Start with a clean native build; do not qualify known preload errors", errors)
        require(not (await self.query("ui.get_message_box"))["open"], "Close the modal before this test")
        menu = await self.query("menu.get_state")
        require(menu["current_menu"] == "INSANITY" and not menu["is_3d"] and menu["selection_index"]//3 == 0,
                "Start in native FlatOut Mode page zero", menu)
        self.initial_selection = menu["selection_index"]
        await self.both_eyes("motion-before-both-eyes")
        for hand in ("left", "right"):
            await self.release(hand)
        vr = await self.query("vr.get_state")
        require(all(c["trigger"] < .01 for c in vr["controllers"]), "Both injected triggers must start released")
        baseline = target_sample(await self.query("vr.targets"))
        baseline_hud = await self.sample()
        require(min(baseline_hud["photo_alphas"]) >= .995 and baseline_hud["root_alpha"] >= .995,
                "Wait for initial menu presentation to settle", baseline_hud)
        _, aim = await self.point_args("challenge.page.next")
        aim.update(dwell_frames=1, command_id=uuid.uuid4().hex, timeout=30)
        # Frontend.tool normally reads calibration immediately before sending
        # input. That serial native query can consume most of a 140ms fade
        # while the concurrent sampler waits on query_lock. Resolve and cache
        # the identical authoritative emitter offset before measurement begins.
        calibration = native_pointer_offset(await self.query("vr.get_state"), self.args.hand)
        require(calibration is not None, "Need a valid native pointer pose before measuring motion")
        aim["ray_offset"] = calibration
        self.calibrated_commands[aim["command_id"]] = dict(aim)
        self.monitor=NativeMotionSampler(self.args.pid,self.hud_depth)
        self.monitor.thread.start()
        require(await asyncio.to_thread(self.monitor.ready.wait,4),'Background native sampler did not become ready')
        if self.monitor.error:raise RuntimeError('Native sampler failed before input: '+repr(self.monitor.error)) from self.monitor.error
        held = await self.tool("hold_ui_at", aim, "motion-next-held-press")
        require(held["status"] == "input_delivered", "XR press was not delivered", held)
        self.last_capture = held.get("capture", self.last_capture)
        await asyncio.sleep(.25)
        await self.stop_monitor()
        require(self.samples, "Native monitor collected no samples")
        fade = qualify_fade(self.samples)
        self.cases.append({"case": "native-page-ink-monotonic-fade", "passed": True, **fade})
        for sample in self.samples:
            require(sample["card_rects"] == baseline_hud["card_rects"] and
                    sample["photo_rects"] == baseline_hud["photo_rects"] and
                    sample["root_rect"] == baseline_hud["root_rect"], "Presentation moved its native HUD rectangles")
            require(sample["root_alpha"] >= .995, "Paging replayed the reader entrance fade")
        after = target_sample(await self.query("vr.targets"))
        require(self.geometry_samples, "No native target snapshot during the held interaction")
        for sample in [*self.geometry_samples, after]:
            check_geometry(baseline, sample)
        self.cases.append({"case": "stable-ray-bounds-and-reader-through-fade", "passed": True,
                           "geometry_samples": len(self.geometry_samples)+1, "before": baseline, "after": after})
        require(after["clicks"] == baseline["clicks"]+1, "Held press activated more than once")
        require((await self.query("menu.get_state"))["selection_index"]//3 == 1,
                "Held press skipped a page or failed to advance")
        vr = await self.query("vr.get_state")
        own = 0 if self.args.hand == "left" else 1
        require(vr["controllers"][own]["trigger"] > .8 and vr["controllers"][1-own]["trigger"] < .01,
                "Held hand or spare-hand exclusion incorrect", vr["controllers"])
        retry = await self.tool("hold_ui_at", aim, "motion-next-same-id-retry")
        for key in ("sync_id", "applied_frame"):
            require(retry["delivery"][key] == held["delivery"][key], "Retry redelivered its physical press", key)
        require((await self.query("vr.targets"))["clicks"] == after["clicks"] and
                (await self.query("menu.get_state"))["selection_index"]//3 == 1,
                "Same-ID retry skipped another page")
        self.cases.append({"case": "held-input-and-idempotent-retry-one-page-only", "passed": True})
        await self.release(self.args.hand)
        await self.both_eyes("motion-after-both-eyes")
        # Return to the initial page through the same public screenshot/ray path.
        await self.click("challenge.page.prev")
        await self.hover("challenge.event."+str(self.initial_selection))
        for hand in ("left", "right"):
            await self.release(hand)
        require(all(c["trigger"] < .01 for c in (await self.query("vr.get_state"))["controllers"]),
                "Pointer cleanup left a trigger held")
        require((await self.query("menu.get_state"))["selection_index"] == self.initial_selection,
                "Restoration did not return the initial event")
        require((await self.query("game.errors"))["total"] == 0, "Native test produced a game error")
        self.cases.append({"case": "release-both-hands-and-restore-initial-page", "passed": True})
        require(len(self.carrier_samples) >= 5, "Too few submitted carrier observations")
        self.cases.append({"case": "submitted-both-eye-carrier-stays-fixed", "passed": True,
                           "capture_count": len(self.carrier_samples),
                           "labels": [s["label"] for s in self.carrier_samples],
                           "note": "Every real MCP screenshot/interaction receipt, including held press and retry"})

    async def cleanup(self):
        if self.monitor:
            try:
                await self.stop_monitor()
            except Exception as error:
                self.cleanup_errors.append("monitor: "+repr(error))
        for hand in ("left", "right"):
            try:
                await self.release(hand)
            except Exception as error:
                self.cleanup_errors.append(hand+": "+repr(error))


async def main(args):
    args.evidence.mkdir(parents=True, exist_ok=False)
    params = StdioServerParameters(command=sys.executable,
        args=[str(ROOT / "mcp-server/openxr_simulator_mcp.py")],
        env={**os.environ, "SIMXR_DATA_DIR": str(args.data_dir.resolve())})
    result = {"passed": False, "pid": args.pid, "hand": args.hand,
              "scope": "Native page-opacity animation, stable geometry, actual held ray press and retry",
              "not_qualified": ["menu entrance", "focus interpolation", "reduced-motion native path",
                                "headset comfort", "GPU performance", "frame-by-frame stereo pixel animation"]}
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            test = ChallengeMotion(args, session)
            try:
                await test.run()
                result["passed"] = True
            except Exception as error:
                result["error"] = repr(error)
                raise
            finally:
                await test.cleanup()
                result.update(cases=test.cases, samples=test.samples,
                              carrier_samples=test.carrier_samples,
                              native_sampler_operations=test.monitor.operations if test.monitor else [],
                              cleanup_errors=test.cleanup_errors)
                if test.cleanup_errors:
                    result["passed"] = False
                (args.evidence / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
            require(result["passed"], "Regression cleanup failed", test.cleanup_errors)
    print("Native challenge page animation and ray press regression PASS", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--hand", choices=("left", "right"), default="right")
    asyncio.run(main(parser.parse_args()))
