"""Spatial review contracts: recommendations never masquerade as visibility."""
import asyncio
import copy
from io import BytesIO
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mcp-server'))
import ui_comfort as comfort
from ui_targeting import rotate


def capture():
    eyes = []
    for name, x, left, right in (('left', -.032, -60, 40), ('right', .032, -40, 60)):
        eyes.append({'eye': name, 'viewport': [0 if name == 'left' else 640, 0, 640, 640],
                     'pose': {'position': [x, 1.7, 0], 'orientation': [0, 0, 0, 1]},
                     'fov': list(map(math.radians, [left, right, 50, -60])), 'uv': [0, 0, 1, 1]})
    return {'capture_id': 'fixture', 'created_at': 100., 'image_size': [1280, 640], 'crop': [0, 0, 1280, 640],
            'geometry': {'supported': True, 'session_id': 'fixture-session', 'frame_id': 123,
                         'head': {'position': [0, 1.7, 0], 'orientation': [0, 0, 0, 1]},
                         'eyes': eyes, 'projection_eyes': copy.deepcopy(eyes), 'surfaces': []}}


def target(yaw=0., elevation=-15., width=.6, height=.25, distance=2., **extra):
    y = 1.7 + distance * math.tan(math.radians(elevation))
    x = distance * math.tan(math.radians(yaw))
    return {'name': 'button', 'surface': {'pose': {'position': [x, y, -distance], 'orientation': [0, 0, 0, 1]},
                                         'size': [width, height]}, **extra}


class GeometryTests(unittest.TestCase):
    def test_preferred_reading_target_and_actual_both_eye_measurement(self):
        r = comfort.analyze_visibility(capture(), [target()])
        t = r['targets'][0]
        self.assertTrue(t['fully_in_both_submitted_eye_frusta'])
        self.assertTrue(t['preferred_gaze_zone'])
        self.assertEqual(t['placement_advisories'], [])
        self.assertAlmostEqual(t['center_angle_deg']['elevation'], -15)
        self.assertAlmostEqual(t['center_distance_m'], 2/math.cos(math.radians(15)))
        self.assertTrue(r['read_only'])

    def test_visible_footer_can_still_be_too_low_for_preferred_region(self):
        t = comfort.analyze_visibility(capture(), [target(elevation=-39)])['targets'][0]
        self.assertTrue(t['fully_in_both_submitted_eye_frusta'])
        self.assertFalse(t['preferred_gaze_zone'])
        self.assertIn('below_preferred_zone', t['placement_advisories'])
        self.assertNotIn('outside_outer_review_envelope', t['placement_advisories'])

    def test_peripheral_target_clips_in_one_asymmetric_eye(self):
        t = comfort.analyze_visibility(capture(), [target(yaw=43, width=.6)])['targets'][0]
        self.assertFalse(t['fully_in_both_submitted_eye_frusta'])
        self.assertEqual(t['render_visibility']['left']['status'], 'partial')
        self.assertEqual(t['render_visibility']['right']['status'], 'full')

    def test_target_outside_both_eye_frusta(self):
        t = comfort.analyze_visibility(capture(), [target(yaw=75, width=.1)])['targets'][0]
        self.assertEqual({v['status'] for v in t['render_visibility'].values()}, {'outside'})

    def test_enclosing_quad_with_all_corners_outside_is_partial_not_missing(self):
        t = comfort.analyze_visibility(capture(), [target(elevation=0, width=100, height=100, role='panel')])['targets'][0]
        self.assertEqual({v['status'] for v in t['render_visibility'].values()}, {'partial'})
        self.assertTrue(all(len(v['clipped_polygon_image']) >= 3 for v in t['render_visibility'].values()))

    def test_behind_head_is_not_a_comfortable_forward_target(self):
        value = target(); value['surface']['pose']['position'][2] = 2
        t = comfort.analyze_visibility(capture(), [value])['targets'][0]
        self.assertIsNone(t['angular_bounds_deg'])
        self.assertFalse(t['preferred_gaze_zone'])
        self.assertFalse(t['fully_in_both_submitted_eye_frusta'])

    def test_near_plane_crossing_does_not_create_nan(self):
        value = target(width=2, height=1)
        value['surface']['pose'] = {'position': [0, 1.7, -.2], 'orientation': [0, math.sin(math.pi/8), 0, math.cos(math.pi/8)]}
        report = comfort.analyze_visibility(capture(), [value])
        json.dumps(report, allow_nan=False)
        self.assertTrue(any(v['status'] == 'partial' for v in report['targets'][0]['render_visibility'].values()))

    def test_missing_projection_eyes_never_falls_back_for_scene_surface(self):
        source = capture(); source['geometry']['projection_eyes'] = []
        with self.assertRaisesRegex(ValueError, 'Both projection_eyes'):
            comfort.analyze_visibility(source, [target()])

    def test_composition_quad_uses_compositor_camera_and_eye_mask(self):
        source = capture()
        surface = target()['surface']; surface.update(id=7, visibility=1)
        source['geometry']['surfaces'] = [surface]
        source['geometry']['projection_eyes'] = []
        t = comfort.analyze_visibility(source)['targets'][0]
        self.assertEqual(t['camera_kind'], 'eyes')
        self.assertEqual(t['role'], 'panel')
        self.assertEqual(t['render_visibility']['right']['status'], 'not_submitted')
        self.assertFalse(t['fully_in_both_submitted_eye_frusta'])

    def test_single_eye_image_retains_honest_both_frustum_geometry(self):
        source = capture(); source.update(image_size=[640, 640], crop=[0, 0, 640, 640])
        t = comfort.analyze_visibility(source, [target()])['targets'][0]
        self.assertTrue(t['fully_in_both_submitted_eye_frusta'])
        self.assertTrue(t['render_visibility']['left']['eye_in_returned_image'])
        self.assertFalse(t['render_visibility']['right']['eye_in_returned_image'])

    def test_odd_stereo_viewports_are_never_split_by_half_image_width(self):
        source = capture(); source.update(image_size=[1279, 641], crop=[0, 0, 1279, 641])
        for kind in ('eyes', 'projection_eyes'):
            source['geometry'][kind][0]['viewport'] = [0, 0, 639, 641]
            source['geometry'][kind][1]['viewport'] = [639, 0, 640, 641]
        t = comfort.analyze_visibility(source, [target()])['targets'][0]
        self.assertEqual(t['render_visibility']['left']['viewport_image_rect'], [0, 0, 639, 641])
        self.assertEqual(t['render_visibility']['right']['viewport_image_rect'], [639, 0, 1279, 641])
        self.assertTrue(all(x >= 639 for x, y in t['render_visibility']['right']['clipped_polygon_image']))

    def test_submitted_uv_subrect_affects_frustum_and_projection(self):
        source = capture()
        for kind in ('eyes', 'projection_eyes'):
            for eye in source['geometry'][kind]: eye['uv'] = [.25, .25, .75, .75]
        t = comfort.analyze_visibility(source, [target(width=.05, height=.05)])['targets'][0]
        polygon = t['render_visibility']['left']['clipped_polygon_image']
        l, r = map(math.tan, source['geometry']['projection_eyes'][0]['fov'][:2])
        expected = ((.032/2-l)/(r-l)-.25)/.5*640-.5
        self.assertAlmostEqual(sum(p[0] for p in polygon)/len(polygon), expected)

    def test_crossing_a_reference_half_space_never_wraps_into_false_comfort(self):
        value = target(width=5)
        value['surface']['pose']['orientation'] = [0, math.sin(math.pi/4), 0, math.cos(math.pi/4)]
        t = comfort.analyze_visibility(capture(), [value])['targets'][0]
        self.assertIsNone(t['angular_bounds_deg'])
        self.assertFalse(t['preferred_gaze_zone'])
        self.assertIn('crosses_or_behind_reference_forward_plane', t['placement_advisories'])

    def test_reference_removes_head_pitch_but_does_not_change_actual_frusta(self):
        source = capture(); pitch = math.radians(-30)
        q = [math.sin(pitch/2), 0, 0, math.cos(pitch/2)]
        source['geometry']['head']['orientation'] = q
        for kind in ('eyes', 'projection_eyes'):
            for eye in source['geometry'][kind]: eye['pose']['orientation'] = q
        value = target(elevation=-30)
        level = comfort.analyze_visibility(source, [value])['targets'][0]
        current = comfort.analyze_visibility(source, [value], reference_mode='captured_head')['targets'][0]
        self.assertAlmostEqual(level['center_angle_deg']['elevation'], -30)
        self.assertAlmostEqual(current['center_angle_deg']['elevation'], 0)
        self.assertEqual(level['render_visibility'], current['render_visibility'])

    def test_saved_neutral_pose_detects_turning_even_when_current_view_is_centered(self):
        source = capture(); yaw = math.radians(40)
        q = [0, math.sin(yaw/2), 0, math.cos(yaw/2)]
        source['geometry']['head']['orientation'] = q
        for kind in ('eyes', 'projection_eyes'):
            for eye in source['geometry'][kind]: eye['pose']['orientation'] = q
        value = target()
        position = value['surface']['pose']['position']
        offset = rotate(q, [position[0], position[1]-1.7, position[2]])
        value['surface']['pose'] = {'position': [offset[0], offset[1]+1.7, offset[2]], 'orientation': q}
        current = comfort.analyze_visibility(source, [value])['targets'][0]
        saved = comfort.analyze_visibility(source, [value], reference_pose={'position': [0, 1.7, 0], 'orientation': [0, 0, 0, 1]})['targets'][0]
        self.assertAlmostEqual(current['center_angle_deg']['yaw'], 0, places=5)
        self.assertAlmostEqual(saved['center_angle_deg']['yaw'], -40, places=5)
        self.assertTrue(current['preferred_gaze_zone'])
        self.assertFalse(saved['preferred_gaze_zone'])
        self.assertEqual(current['render_visibility'], saved['render_visibility'])

    def test_elevation_edge_extremum_is_not_underestimated_by_corners(self):
        value = target(elevation=0, width=8, height=.4)
        value['surface']['pose']['position'][1] = 2.7
        t = comfort.analyze_visibility(capture(), [value])['targets'][0]
        self.assertAlmostEqual(t['angular_bounds_deg']['elevation'][1], math.degrees(math.atan2(1.2, 2)), places=6)

    def test_saved_reference_translation_does_not_change_actual_viewing_distance_or_size(self):
        source = capture()
        actual = comfort.analyze_visibility(source, [target()])['targets'][0]
        saved = comfort.analyze_visibility(source, [target()], reference_pose={
            'position': [10, 1.7, 0], 'orientation': [0, 0, 0, 1]})['targets'][0]
        self.assertEqual(actual['center_distance_m'], saved['center_distance_m'])
        self.assertEqual(actual['angular_size_deg'], saved['angular_size_deg'])
        self.assertEqual(actual['current_head_center_angle_deg'], saved['current_head_center_angle_deg'])
        self.assertNotEqual(actual['center_angle_deg'], saved['center_angle_deg'])
        self.assertNotIn('farther_than_distance_guidance', saved['placement_advisories'])

    def test_rect_canvas_maps_only_the_control_region(self):
        value = target(width=4, height=2, canvas_size=[1920, 1080], rect=[960, 540, 1440, 810])
        t = comfort.analyze_visibility(capture(), [value])['targets'][0]
        self.assertEqual(t['rect_uv'], [.5, .5, .75, .75])
        self.assertAlmostEqual(t['world_corners'][0][0], 0)
        self.assertAlmostEqual(t['world_corners'][1][0], 1)

    def test_small_target_and_distance_are_advisory_not_visibility_failures(self):
        t = comfort.analyze_visibility(capture(), [target(width=.05, height=.05, distance=4)])['targets'][0]
        self.assertTrue(t['fully_in_both_submitted_eye_frusta'])
        self.assertIn('small_angular_control_target', t['placement_advisories'])
        self.assertIn('farther_than_distance_guidance', t['placement_advisories'])

    def test_configurable_profiles_and_inputs_are_not_mutated(self):
        source, values = capture(), [target(elevation=-39)]
        original = copy.deepcopy((source, values))
        profile = {'name': 'project-footer', 'preferred_elevation_deg': [-45, 0]}
        report = comfort.analyze_visibility(source, values, profile=profile)
        self.assertTrue(report['targets'][0]['preferred_gaze_zone'])
        self.assertEqual((source, values), original)
        self.assertEqual(profile, {'name': 'project-footer', 'preferred_elevation_deg': [-45, 0]})
        self.assertEqual(comfort.DEFAULT_PROFILE['preferred_elevation_deg'], [-35, 0])

    def test_no_surface_never_guesses_scene_depth(self):
        report = comfort.analyze_visibility(capture())
        self.assertEqual(report['targets'], [])
        self.assertIn('never guessed', report['note'])
        self.assertEqual(len(report['eye_frusta']['eyes']), 2)

    def test_invalid_input_is_rejected(self):
        cases = [({'targets': [target(surface_id=1)]}, 'exactly one'),
                 ({'targets': [target(rect=[1, 0, 0, 1])]}, 'rect'),
                 ({'profile': {'min_control_angle_deg': float('nan')}}, 'min_control'),
                 ({'profile': {'preferred_yaw_deg': [-50, 50]}}, 'Preferred'),
                 ({'targets': [target()]*257}, '256'),
                 ({'reference_mode': 'guess'}, 'reference_mode')]
        for kwargs, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                comfort.analyze_visibility(capture(), **kwargs)

    def test_overlay_is_separate_png_and_preserves_source(self):
        source = capture(); raw = BytesIO(); Image.new('RGB', (1280, 640), '#202020').save(raw, 'JPEG')
        pixels = raw.getvalue(); immutable = bytes(pixels)
        report = comfort.analyze_visibility(source, [target(elevation=-39)])
        png = comfort.annotate_visibility(pixels, source, report)
        self.assertEqual(pixels, immutable)
        image = Image.open(BytesIO(png))
        self.assertEqual(image.format, 'PNG'); self.assertEqual(image.size, (1280, 640))
        self.assertNotEqual(image.getpixel((0, 0)), (32, 32, 32))


class McpContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_registered_tools_and_cached_read_only_analysis(self):
        import openxr_simulator_mcp as server
        tools = {t.name: t for t in await server.list_tools()}
        self.assertIn('get_ui_comfort_guidelines', tools)
        self.assertIn('analyze_ui_visibility', tools)
        source = capture(); raw = BytesIO(); Image.new('RGB', (1280, 640), '#111111').save(raw, 'JPEG')
        with tempfile.TemporaryDirectory() as directory:
            from ui_targeting import UiAutomation
            automation = UiAutomation(Path(directory))
            automation.captures['fixture'] = source; automation.images['fixture'] = raw.getvalue()
            automation.capture = AsyncMock(side_effect=AssertionError('No capture request may be issued'))
            automation.interact = AsyncMock(side_effect=AssertionError('No input may be issued'))
            with patch.object(server, 'ui_automation', automation):
                result = await server.call_tool('analyze_ui_visibility', {'capture_id': 'fixture', 'targets': [target()]})
                self.assertEqual(json.loads(result[0].text)['status'], 'analyzed')
                self.assertEqual(result[1].mimeType, 'image/png')
                self.assertEqual(list(Path(directory).iterdir()), [])
                self.assertEqual(automation.images['fixture'], raw.getvalue())
                automation.capture.assert_not_called(); automation.interact.assert_not_called()

    async def test_unknown_capture_errors_without_runtime_access(self):
        import openxr_simulator_mcp as server
        result = await server.call_tool('analyze_ui_visibility', {'capture_id': 'does-not-exist'})
        self.assertEqual(json.loads(result[0].text)['status'], 'error')


if __name__ == '__main__':
    unittest.main()
