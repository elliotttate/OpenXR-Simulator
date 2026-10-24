import copy
import math
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"mcp-server"))
from ui_targeting import aim_orientation, intersect_surface, resolve_target, rotate, world_ray


def capture():
    eye = {"eye": "left", "viewport": [100, 50, 1000, 800], "uv": [0, 0, 1, 1],
           "pose": {"position": [0, 0, 0], "orientation": [0, 0, 0, 1]},
           "fov": [-math.pi/4, math.pi/4, math.pi/4, -math.pi/4]}
    return {"created_at": time.time(), "image_size": [500, 400], "crop": [100, 50, 1000, 800],
            "geometry": {"supported": True, "eyes": [eye], "projection_eyes": [copy.deepcopy(eye)],
                         "hands": [[-.2, -.3, -.4], [.2, -.3, -.4]], "surfaces": [
                             {"id": 0, "visibility": 0, "size": [4, 4],
                              "pose": {"position": [0, 0, -2], "orientation": [0, 0, 0, 1]}}]}}


class TargetingTests(unittest.TestCase):
    def test_crop_resize_and_pixel_centres(self):
        target = resolve_target(capture(), 249.5, 199.5)
        self.assertEqual(target["raw_pixel"], [600, 450])
        self.assertEqual(target["target_point"], [0, 0, -2])
        direction = rotate(target["aim_pose"]["orientation"], [0, 0, -1])
        hit = intersect_surface(target["aim_pose"]["position"], direction, capture()["geometry"]["surfaces"][0])
        for actual, expected in zip(hit, [0, 0, -2]):
            self.assertAlmostEqual(actual, expected, places=6)

    def test_asymmetric_fov(self):
        e = capture()["geometry"]["eyes"][0]
        e["fov"] = [-.9, .5, .7, -.6]
        _, ray = world_ray(e, 600, 450)
        self.assertAlmostEqual(ray[0]/-ray[2], (math.tan(-.9)+math.tan(.5))/2)
        self.assertAlmostEqual(ray[1]/-ray[2], (math.tan(.7)+math.tan(-.6))/2)

    def test_rotated_surface_and_camera(self):
        c = capture()
        angle = .3
        q = [0, math.sin(angle/2), 0, math.cos(angle/2)]
        c["geometry"]["eyes"][0]["pose"]["orientation"] = q
        surface = c["geometry"]["surfaces"][0]
        surface["pose"]["position"] = rotate(q, [0, 0, -2])
        surface["pose"]["orientation"] = q
        result = resolve_target(c, 249.5, 199.5)
        for a, b in zip(result["target_point"], surface["pose"]["position"]):
            self.assertAlmostEqual(a, b)

    def test_both_layouts_and_hands(self):
        for viewport in ([500, 0, 500, 400], [0, 400, 500, 400]):
            c = capture()
            c["crop"] = [0, 0, 1000, 800]
            c["image_size"] = [1000, 800]
            c["geometry"]["eyes"][0].update(eye="right", viewport=viewport)
            result = resolve_target(c, viewport[0]+249.5, viewport[1]+199.5, hand="left")
            self.assertEqual(result["eye"], "right")
            self.assertEqual(result["aim_pose"]["position"], [-.2, -.3, -.4])

    def test_layer_order_and_visibility(self):
        c = capture()
        second = copy.deepcopy(c["geometry"]["surfaces"][0])
        second["id"] = 1
        second["pose"]["position"][2] = -3
        c["geometry"]["surfaces"].append(second)
        self.assertEqual(resolve_target(c, 249.5, 199.5)["surface_id"], 1)
        second["visibility"] = 2
        self.assertEqual(resolve_target(c, 249.5, 199.5)["surface_id"], 0)

    def test_no_guessed_distance(self):
        c = capture()
        c["geometry"]["surfaces"] = []
        with self.assertRaisesRegex(ValueError, "No known UI surface"):
            resolve_target(c, 249.5, 199.5)
        target = resolve_target(c, 249.5, 199.5, origin_mode="eye")
        self.assertIsNone(target["target_point"])
        self.assertEqual(target["aim_pose"]["position"], [0, 0, 0])

    def test_explicit_scene_surface(self):
        c = capture()
        s = c["geometry"]["surfaces"][0]
        c["geometry"]["surfaces"] = []
        self.assertEqual(resolve_target(c, 249.5, 199.5, surface=s)["target_point"], [0, 0, -2])

    def test_invalid_stale_and_letterbox(self):
        for x, y in ((-1, 0), (500, 2), (2, 400), (math.nan, 2), (2, math.inf)):
            with self.assertRaises(ValueError):
                resolve_target(capture(), x, y)
        c = capture()
        c["created_at"] -= 31
        with self.assertRaisesRegex(ValueError, "expired"):
            resolve_target(c, 1, 1)
        c = capture()
        c["crop"] = [0, 0, 1000, 800]
        with self.assertRaisesRegex(ValueError, "viewport"):
            resolve_target(c, 0, 0)

    def test_aim_singularity(self):
        for direction in ([0, 0, 1], [0, 0, -1], [0, 1, 0]):
            self.assertEqual([round(x, 6) for x in rotate(aim_orientation(direction), [0, 0, -1])], direction)

    def test_application_ray_offset_hits_quad_from_hand(self):
        offset = {"position": [.03, 0, 0], "orientation": [0, math.sin(-.07), 0, math.cos(-.07)]}
        c = capture()
        result = resolve_target(c, 300, 210, ray_offset=offset)
        pose = result["aim_pose"]
        direction = rotate(offset["orientation"], rotate(pose["orientation"], [0, 0, -1]))
        origin = [a+b for a, b in zip(pose["position"], offset["position"])]
        hit = intersect_surface(origin, direction, c["geometry"]["surfaces"][0])
        self.assertEqual(pose["position"], c["geometry"]["hands"][1])
        for actual, expected in zip(hit, result["target_point"]):
            self.assertAlmostEqual(actual, expected, places=6)

    def test_application_ray_offset_preserves_world_view_ray(self):
        c = capture()
        c["geometry"]["surfaces"] = []
        offset = {"position": [.03, .02, -.01], "orientation": [math.sin(.1), 0, 0, math.cos(.1)]}
        result = resolve_target(c, 305, 115, origin_mode="eye", ray_offset=offset)
        pose = result["aim_pose"]
        direction = rotate(offset["orientation"], rotate(pose["orientation"], [0, 0, -1]))
        for actual, expected in zip(direction, result["ray_direction"]):
            self.assertAlmostEqual(actual, expected, places=6)
        for a, b, expected in zip(pose["position"], offset["position"], result["ray_origin"]):
            self.assertAlmostEqual(a+b, expected, places=6)

    def test_invalid_application_ray_offsets(self):
        for offset in ({}, {"position": [0, 0], "orientation": [0, 0, 0, 1]},
                       {"position": [0, math.inf, 0], "orientation": [0, 0, 0, 1]},
                       {"position": [0, 0, 0], "orientation": [0, 0, 0, 2]}):
            with self.assertRaisesRegex(ValueError, "ray_offset"):
                resolve_target(capture(), 250, 200, ray_offset=offset)


if __name__ == "__main__":
    unittest.main()
