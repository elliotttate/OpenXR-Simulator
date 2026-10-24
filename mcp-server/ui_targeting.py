"""Screenshot coordinates -> XR aim poses, plus correlated, asynchronous file IPC.

Coordinates address pixel centres in the image returned to the MCP client. No
desktop coordinates or guessed UI distances are used here.
"""
import asyncio
import hashlib
import json
import math
import time
import uuid
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw


def rotate(q, v):
    x, y, z, w = q
    tx, ty, tz = (2 * (y*v[2]-z*v[1]), 2 * (z*v[0]-x*v[2]), 2 * (x*v[1]-y*v[0]))
    return [v[0]+w*tx+y*tz-z*ty, v[1]+w*ty+z*tx-x*tz, v[2]+w*tz+x*ty-y*tx]


def normalized(v):
    length = math.sqrt(sum(x*x for x in v))
    if not math.isfinite(length) or length < 1e-9:
        raise ValueError("Degenerate ray or surface")
    return [x/length for x in v]


def aim_orientation(direction):
    """Shortest rotation taking the OpenXR aim axis (0,0,-1) to direction."""
    x, y, z = normalized(direction)
    if z > 1-1e-8:
        return [0, 1, 0, 0]
    return normalized([y, -x, 0, 1-z])


def world_ray(eye, raw_x, raw_y):
    ex, ey, ew, eh = eye["viewport"]
    u, v = (raw_x-ex)/ew, (raw_y-ey)/eh
    u0, v0, u1, v1 = eye.get("uv", [0, 0, 1, 1])
    u, v = u0+u*(u1-u0), v0+v*(v1-v0)
    if not (0 <= u <= 1 and 0 <= v <= 1):
        raise ValueError("Pixel lies outside the submitted eye imageRect")
    left, right, up, down = map(math.tan, eye["fov"])
    direction = normalized(rotate(eye["pose"]["orientation"],
                                 [left+u*(right-left), up+v*(down-up), -1]))
    return eye["pose"]["position"], direction


def intersect_surface(origin, direction, surface):
    pose = surface["pose"]
    q = pose["orientation"]
    inverse = [-q[0], -q[1], -q[2], q[3]]
    local_origin = rotate(inverse, [a-b for a, b in zip(origin, pose["position"])])
    local_direction = rotate(inverse, direction)
    if abs(local_direction[2]) < 1e-8:
        return None
    distance = -local_origin[2]/local_direction[2]
    if distance <= 0:
        return None
    local = [a+distance*b for a, b in zip(local_origin, local_direction)]
    width, height = surface["size"]
    if abs(local[0]) > width/2 or abs(local[1]) > height/2:
        return None
    return [a+distance*b for a, b in zip(origin, direction)]


def resolve_target(capture, x, y, hand="right", origin_mode="hand", surface_id=None, surface=None, ray_offset=None):
    width, height = capture["image_size"]
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (x, y)):
        raise ValueError("x and y must be finite image coordinates")
    if not (0 <= x < width and 0 <= y < height):
        raise ValueError(f"Pixel outside returned image ({width}x{height})")
    if hand not in ("left", "right") or origin_mode not in ("hand", "eye"):
        raise ValueError("Invalid hand or origin_mode")
    g = capture["geometry"]
    if not g.get("supported"):
        raise ValueError("This capture layout cannot be targeted; use a single eye or stereo layout")
    if time.time()-capture["created_at"] > 30:
        raise ValueError("Capture expired; take a fresh screenshot")
    cx, cy, cw, ch = capture["crop"]
    raw_x, raw_y = cx+(x+0.5)*cw/width, cy+(y+0.5)*ch/height
    eyes = [e for e in g["eyes"] if e["viewport"][0] <= raw_x < e["viewport"][0]+e["viewport"][2]
            and e["viewport"][1] <= raw_y < e["viewport"][1]+e["viewport"][3]]
    if len(eyes) != 1:
        raise ValueError("Pixel is outside an unambiguous eye viewport (possibly a letterbox)")
    eye = eyes[0]
    origin, direction = world_ray(eye, raw_x, raw_y)
    target = None
    chosen = None
    if surface is not None:
        validate_surface(surface)
        # Scene surfaces use the app-submitted camera, rather than quad compositor camera.
        eye = next((e for e in g["projection_eyes"] if e["eye"] == eye["eye"]), eye)
        origin, direction = world_ray(eye, raw_x, raw_y)
        target = intersect_surface(origin, direction, surface)
        if target is None:
            raise ValueError("Pixel does not intersect the provided UI surface")
    else:
        # Composition order, not nearest distance: a later quad overlays earlier quads.
        for candidate in reversed(g["surfaces"]):
            if surface_id is not None and candidate["id"] != surface_id:
                continue
            if candidate["visibility"] not in (0, 1 if eye["eye"] == "left" else 2):
                continue
            hit = intersect_surface(origin, direction, candidate)
            if hit is not None:
                target, chosen = hit, candidate["id"]
                break
        if surface_id is not None and target is None:
            raise ValueError("Pixel does not intersect the selected quad")
    if target is None:
        if origin_mode == "hand":
            raise ValueError("No known UI surface at this pixel. Supply a surface or explicitly use origin_mode='eye'.")
        eye = next((e for e in g["projection_eyes"] if e["eye"] == eye["eye"]), eye)
        origin, direction = world_ray(eye, raw_x, raw_y)
    # Some applications modify the XR aim ray before hit-testing (e.g. FlatOut).
    # The supplied translation and rotation are independently applied in XR world
    # axes: app_origin = pose.position + offset.position;
    # app_direction = rotate(offset.orientation, rotate(pose.orientation, -Z)).
    offset_p, offset_q = [0, 0, 0], [0, 0, 0, 1]
    if ray_offset is not None:
        try:
            offset_p, offset_q = ray_offset["position"], ray_offset["orientation"]
            valid = (len(offset_p) == 3 and len(offset_q) == 4 and
                     all(math.isfinite(v) for v in [*offset_p, *offset_q]) and
                     abs(sum(v*v for v in offset_q)-1) < .001)
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise ValueError("ray_offset requires finite position[3] and unit orientation[4]")
    aim_origin = ([a-b for a, b in zip(origin, offset_p)] if origin_mode == "eye"
                  else g["hands"][0 if hand == "left" else 1])
    app_origin = [a+b for a, b in zip(aim_origin, offset_p)]
    app_direction = direction if target is None else normalized([a-b for a, b in zip(target, app_origin)])
    aim_direction = rotate([-offset_q[0], -offset_q[1], -offset_q[2], offset_q[3]], app_direction)
    return {"eye": eye["eye"], "raw_pixel": [raw_x, raw_y], "ray_origin": origin,
            "ray_direction": direction, "target_point": target, "surface_id": chosen,
            "aim_pose": {"position": aim_origin, "orientation": aim_orientation(aim_direction)},
            "origin_mode": origin_mode, "ray_offset": ray_offset,
            "application_ray": {"position": app_origin, "direction": app_direction}}


def validate_surface(surface):
    try:
        p, q, size = surface["pose"]["position"], surface["pose"]["orientation"], surface["size"]
        valid = (len(p) == 3 and len(q) == 4 and len(size) == 2 and
                 all(math.isfinite(v) for v in [*p, *q, *size]) and min(size) > 0 and
                 abs(sum(v*v for v in q)-1) < 0.001)
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("surface requires position[3], unit orientation[4], and positive size[2]")


def atomic_json(path, payload):
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    tmp.write_text(json.dumps(payload, allow_nan=False), encoding="utf-8")
    tmp.replace(path)


class UiAutomation:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.lock = asyncio.Lock()
        self.captures = {}
        self.images = {}

    async def wait_json(self, path, predicate, timeout):
        until = time.monotonic()+timeout
        while time.monotonic() < until:
            try:
                result = json.loads(path.read_text(encoding="utf-8"))
                if predicate(result):
                    return result
            except (OSError, ValueError):
                pass
            await asyncio.sleep(0.02)
        raise TimeoutError(f"Runtime did not complete {path.name} within {timeout}s")

    async def capture(self, eye="left", timeout=5.0, after_sync=0, max_width=1280):
        if eye not in ("left", "right", "both") or not 0 < timeout <= 30:
            raise ValueError("Invalid eye or timeout (0..30 seconds)")
        if not isinstance(max_width, int) or not 128 <= max_width <= 4096:
            raise ValueError("max_width must be an integer in 128..4096")
        self.directory.mkdir(parents=True, exist_ok=True)
        capture_id = uuid.uuid4().hex
        atomic_json(self.directory/"screenshot_request.json", {
            "capture_id": capture_id, "eye": eye, "layer": "all", "after_sync": after_sync})
        status = await self.wait_json(self.directory/"screenshot_status.json",
                                      lambda s: s.get("capture_id") == capture_id, timeout)
        raw_path = self.directory/f"screenshot.{capture_id}.bmp"
        with Image.open(raw_path) as source:
            image = source.convert("RGB")
        raw_path.unlink(missing_ok=True)
        if list(image.size) != [status["width"], status["height"]]:
            raise ValueError("Capture metadata does not match its pixels")
        g = status["geometry"]
        crop = [0, 0, image.width, image.height]
        if eye != "both":
            e = next((e for e in g["eyes"] if e["eye"] == eye), None)
            if e is None:
                raise ValueError(f"The captured preview does not contain the {eye} eye")
            x, y, w, h = e["viewport"]
            x0, y0 = max(0, math.floor(x)), max(0, math.floor(y))
            x1, y1 = min(image.width, math.ceil(x+w)), min(image.height, math.ceil(y+h))
            if x1 <= x0 or y1 <= y0:
                raise ValueError("Eye is outside the captured viewport")
            crop = [x0, y0, x1-x0, y1-y0]
            image = image.crop((x0, y0, x1, y1))
        if image.width > max_width:
            image = image.resize((max_width, max(1, round(image.height*max_width/image.width))), Image.Resampling.LANCZOS)
        info = {"capture_id": capture_id, "created_at": time.time(), "eye": eye,
                "image_size": list(image.size), "raw_size": [status["width"], status["height"]],
                "crop": crop, "max_width": max_width, "coordinate_origin": "top_left", "geometry": g}
        self.captures[capture_id] = info
        while len(self.captures) > 32:
            oldest = next(iter(self.captures))
            self.captures.pop(oldest)
            self.images.pop(oldest, None)
        out = BytesIO()
        image.save(out, "JPEG", quality=90)
        self.images[capture_id] = out.getvalue()
        return info, out.getvalue()

    async def command(self, payload, timeout=5):
        command_id = payload.setdefault("command_id", uuid.uuid4().hex)
        if not isinstance(command_id, str) or not command_id.isalnum() or len(command_id) > 64:
            raise ValueError("command_id must be 1..64 alphanumeric characters")
        semantic = {k: v for k, v in payload.items() if k not in ("command_id", "request_digest", "dwell_frames", "hold_frames", "lease_ms")}
        digest = hashlib.sha256(json.dumps(semantic, sort_keys=True, allow_nan=False).encode()).hexdigest()
        payload["request_digest"] = digest
        path = self.directory/f"ui_pointer_result.{command_id}.json"
        # Runtime retains results for idempotent retries. Do not send a second click.
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing.get("session_id") == payload["session_id"]:
                if existing.get("request_digest") != digest:
                    raise ValueError("command_id was already used for a different interaction")
                return existing
            raise ValueError("command_id belongs to a previous runtime session; use a new key")
        atomic_json(self.directory/"ui_pointer_command.json", payload)
        return await self.wait_json(path, lambda r: r.get("command_id") == command_id and
                                    r.get("session_id") == payload["session_id"], timeout)

    async def interact(self, action, capture_id, x=0, y=0, hand="right", button="trigger",
                       origin_mode="hand", surface_id=None, surface=None, dwell_frames=2,
                       hold_frames=2, timeout=5.0, command_id=None, ray_offset=None):
        if action not in ("hover", "click", "release"):
            raise ValueError("Unknown UI interaction")
        if hand not in ("left", "right") or button not in ("trigger", "primary", "menu", "select"):
            raise ValueError("Invalid hand or button")
        if not all(isinstance(n, int) and 1 <= n <= 90 for n in (dwell_frames, hold_frames)):
            raise ValueError("dwell_frames and hold_frames must be integers in 1..90")
        if not 0 < timeout <= 30:
            raise ValueError("timeout must be in 0..30 seconds")
        capture = self.captures.get(capture_id)
        if capture is None:
            raise ValueError("Unknown capture_id; take a screenshot in this MCP session")
        target = {} if action == "release" else resolve_target(capture, x, y, hand, origin_mode, surface_id, surface, ray_offset)
        payload = {"command_id": command_id or uuid.uuid4().hex, "action": action,
                   "session_id": capture["geometry"]["session_id"], "capture_id": capture_id,
                   "hand": 0 if hand == "left" else 1, "button": button,
                   "dwell_frames": dwell_frames, "hold_frames": hold_frames,
                   "surface_id": target.get("surface_id", -1), "lease_ms": 10000}
        if target:
            p = target["aim_pose"]
            payload.update(dict(zip(("px", "py", "pz"), p["position"])))
            payload.update(dict(zip(("qx", "qy", "qz", "qw"), p["orientation"])))
        try:
            result = await self.command(payload, timeout)
            if not result["success"]:
                raise ValueError(result["error"])
            after, pixels = await self.capture(capture["eye"], timeout, result["sync_id"], capture["max_width"])
        except (TimeoutError, asyncio.CancelledError, ValueError):
            # The runtime also expires its lease if the MCP disconnects or the app stalls.
            if action != "release":
                try:
                    await self.command({"action": "release", "session_id": payload["session_id"],
                                        "hand": payload["hand"]}, min(timeout, 2))
                except (TimeoutError, ValueError, OSError):
                    pass
            raise
        return {"status": "input_delivered", "interaction": action, "target": target,
                "delivery": result, "capture": after,
                "confirmation": "Inspect application feedback; delivery alone does not confirm a UI activation."}, pixels


def annotate_target(jpeg, x, y):
    image = Image.open(BytesIO(jpeg)).convert("RGB")
    draw = ImageDraw.Draw(image)
    draw.ellipse((x-7, y-7, x+7, y+7), outline="red", width=2)
    draw.line((x-11, y, x+11, y), fill="red", width=1)
    draw.line((x, y-11, x, y+11), fill="red", width=1)
    out = BytesIO()
    image.save(out, "JPEG", quality=90)
    return out.getvalue()
