"""Audit the complete FlatOut Mode event browser using screenshot-derived XR input.

Start in INSANITY on the current native Fox build with an isolated profile.
Diagnostics stay read-only. The optional preview round trip never starts a race.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from flatout_frontend_e2e import Frontend, ROOT


class Challenge(Frontend):
    async def layout(self):
        menu = await self.query("menu.get_state")
        assert menu["current_menu"] == "INSANITY" and not menu["is_3d"], menu
        native = await self.query("vr.targets")
        await self.capture("challenge-page-layout")
        cards = sorted((t for t in native["targets"] if t["name"].startswith("challenge.event.")),
                       key=lambda t: int(t["name"].split(".")[-1]))
        indices = [int(t["name"].split(".")[-1]) for t in cards]
        valid = len(cards) == min(6, menu["item_count"] - (indices[0] if indices else 0))
        self.cases.append({"case": "six-explicit-event-card-targets", "passed": valid,
                           "indices": indices, "targets": native["targets"]})
        assert valid, "FlatOut Mode needs six explicit ray cards per page"
        assert all(i["title"].strip() for i in menu["items"]), "Every event needs a localized readable name"
        page = indices[0] // 6
        assert indices == list(range(page * 6, min((page + 1) * 6, menu["item_count"]))), indices
        names = {t["name"] for t in native["targets"]}
        assert ("challenge.page.prev" in names) == (page > 0), names
        assert ("challenge.page.next" in names) == ((page + 1) * 6 < menu["item_count"]), names
        assert not any(n.startswith(("helper.LB.", "helper.RB.")) for n in names), "Paging must stay in the header"
        assert any(n.startswith("helper.Cancel.") for n in names), "Keep a visible Back action"
        for target in cards:
            l, t, r, b = target["rect"]
            assert 80 <= l < r <= 1840 and 240 <= t < b <= 920, target
            assert r - l >= 520 and b - t >= 290, target
        for i, target in enumerate(cards):
            l, t, r, b = target["rect"]
            for other in cards[i + 1:]:
                l2, t2, r2, b2 = other["rect"]
                if min(b, b2) > max(t, t2):
                    assert max(l, l2) - min(r, r2) >= 20, (target, other)
                if min(r, r2) > max(l, l2):
                    assert max(t, t2) - min(b, b2) >= 20, (target, other)
        for target in native["targets"]:
            l, t, r, b = target["rect"]
            if target["name"].startswith("challenge.page."):
                assert r - l >= 88 and b - t >= 88 and b <= 224, target
            elif target["name"].startswith("helper."):
                assert r - l >= 200 and b - t >= 80 and t >= 960, target
        await self.audit("challenge-card-header-and-dock-spacing")
        self.cases.append({"case": "challenge-page-readable-bounds-and-gaps", "passed": True,
                           "page": page, "names": [menu["items"][i]["title"] for i in indices]})
        return menu, cards, page

    async def run(self):
        menu, cards, initial_page = await self.layout()
        await self.reading_comfort("flatout-mode")
        if self.args.layout_only:
            return
        while (await self.layout())[2] > 0:
            await self.click("challenge.page.prev")
        visited, locked_checked = [], 0
        page_count = (menu["item_count"] + 5) // 6
        for page in range(page_count):
            menu, cards, actual_page = await self.layout()
            assert actual_page == page, (page, actual_page)
            for target in cards:
                index = int(target["name"].split(".")[-1])
                await self.hover(target["name"])
                current = await self.query("menu.get_state")
                assert current["selection_index"] == index and current["current_menu"] == "INSANITY", current
                assert not (await self.query("ui.get_message_box"))["open"]
                visited.append(index)
            locked = next((t for t in cards if menu["items"][int(t["name"].split(".")[-1])]["locked"]), None)
            if locked:
                before, aim = await self.point_args(locked["name"])
                assert (await self.tool("hover_ui_at", aim, "challenge-locked-hover"))["status"] == "input_delivered"
                args = {**aim, "hold_frames": 45, "timeout": 30, "command_id": uuid.uuid4().hex}
                assert (await self.tool("click_ui_at", args, "challenge-locked-click"))["status"] == "input_delivered"
                after = await self.query("vr.targets")
                assert after["clicks"] == before["clicks"], "A locked event dispatched an activation"
                assert (await self.query("menu.get_state"))["current_menu"] == "INSANITY"
                assert not (await self.query("game.get_state"))["is_loading"]
                assert not (await self.query("ui.get_message_box"))["open"]
                await self.tool("click_ui_at", args, "challenge-locked-retry")
                assert (await self.query("vr.targets"))["clicks"] == before["clicks"]
                locked_checked += 1
            print("FlatOut Mode page", page + 1, "of", page_count, "PASS", flush=True)
            if page + 1 < page_count:
                await self.click("challenge.page.next")
                assert (await self.layout())[2] == page + 1, "A held click skipped a page"
        assert visited == list(range(menu["item_count"])) and locked_checked, (visited, locked_checked)
        self.cases.append({"case": "all-event-pages-hovers-and-locked-clicks", "passed": True,
                           "visited_indices": visited, "locked_pages_checked": locked_checked})
        while (await self.layout())[2] > initial_page:
            await self.click("challenge.page.prev")
        if self.args.preview_round_trip:
            menu, cards, _ = await self.layout()
            event = next(t for t in cards if not menu["items"][int(t["name"].split(".")[-1])]["locked"])
            index = int(event["name"].split(".")[-1])
            await self.click(event["name"])
            current = await self.query("menu.get_state")
            assert current["current_menu"] == "CARS" and current["item_count"] == 1, current
            await self.audit("challenge-fixed-vehicle-preview-actions")
            back = next(t["name"] for t in (await self.query("vr.targets"))["targets"] if t["name"].startswith("helper.Cancel."))
            await self.click(back)
            current = await self.query("menu.get_state")
            assert current["current_menu"] == "INSANITY" and current["selection_index"] == index, current
            assert (await self.layout())[2] == initial_page
            self.cases.append({"case": "event-preview-and-back-preserve-page-without-starting-race", "passed": True,
                               "title": menu["items"][index]["title"]})
        assert (await self.query("game.errors"))["total"] == self.errors_before


async def main(args):
    args.evidence.mkdir(parents=True, exist_ok=False)
    env = {**os.environ, "SIMXR_DATA_DIR": str(args.data_dir.resolve())}
    params = StdioServerParameters(command=sys.executable,
                                   args=[str(ROOT / "mcp-server/openxr_simulator_mcp.py")], env=env)
    result = {"passed": False, "pid": args.pid, "hand": args.hand,
              "scope": "Complete FlatOut Mode browser layout, 42-event paging, actual XR hover/click and safe vehicle preview"}
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            test = Challenge(args, session)
            test.errors_before = (await test.query("game.errors"))["total"]
            try:
                await test.run()
                result["passed"] = True
            except Exception as error:
                result["error"] = repr(error)
                raise
            finally:
                result["cases"] = test.cases
                (args.evidence / "result.json").write_text(json.dumps(result, indent=2))
                if test.last_capture:
                    for hand in ("left", "right"):
                        await session.call_tool("release_ui_pointer", {"capture_id": test.last_capture["capture_id"], "hand": hand})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--hand", choices=("left", "right"), default="right")
    parser.add_argument("--layout-only", action="store_true")
    parser.add_argument("--preview-round-trip", action="store_true")
    asyncio.run(main(parser.parse_args()))
