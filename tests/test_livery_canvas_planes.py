"""Projection checks for the affine canvas mappings reported by native livery."""
import copy
import math
import unittest

import numpy as np

from flatout_livery_e2e import ui_frame_at, ui_surface, screenshot_pixel, quaternion_matrix


class CanvasPlanes(unittest.TestCase):
    def setUp(self):
        camera = dict(origin=[0, 0, 0], right=[1, 0, 0], up=[0, 1, 0], back=[0, 0, 1])
        center = dict(origin=[0, 0, -3], right=[1, 0, 0], up=[0, 1, 0], back=[0, 0, -1])
        frame = dict(camera=camera, panel=center, panel_width=4.6, panel_height=2.5875)
        self.legacy = dict(vr=dict(frame=copy.deepcopy(frame)))
        panels = []
        for side, x in enumerate((264, 1656)):
            angle = math.radians(30)*(1 if side else -1)
            right = np.array([math.cos(angle), 0, -math.sin(angle)])
            back = np.array([-math.sin(angle), 0, -math.cos(angle)])
            origin = np.array([2.6 if side else -2.6, 0, -3])-right*((x/1920-.5)*4.6)
            panels.append(dict(id=('left', 'right')[side], canvas_rect=[side*960, 0, (side+1)*960, 1080],
                               panel=dict(origin=origin.tolist(), right=right.tolist(), up=[0, 1, 0], back=back.tolist()),
                               panel_width=4.6, panel_height=2.5875))
        frame['panels'] = panels
        self.state = dict(vr=dict(frame=frame))

    def test_split_requires_point_and_rejects_unmapped_points(self):
        with self.assertRaises(ValueError):
            ui_surface(self.state)
        for point in ((-1, 200), (1920, 200), (400, 1080)):
            with self.subTest(point=point), self.assertRaises(ValueError):
                ui_frame_at(self.state, *point)
        self.assertEqual(ui_frame_at(self.state, 959.9, 200)['id'], 'left')
        self.assertEqual(ui_frame_at(self.state, 960, 200)['id'], 'right')

    def test_both_sides_project_from_native_plane(self):
        capture = dict(geometry=dict(projection_eyes=[dict(eye='left',
            pose=dict(position=[0, 0, 0], orientation=[0, 0, 0, 1]),
            fov=[-math.pi/4, math.pi/4, math.pi/4, -math.pi/4], viewport=[0, 0, 1000, 1000])]),
            crop=[0, 0, 1000, 1000], image_size=[1000, 1000])
        for x, world_x in ((264, -2.6), (1656, 2.6)):
            with self.subTest(x=x):
                surface = ui_surface(self.state, x, 540)
                basis = quaternion_matrix(surface['pose']['orientation'])
                point = np.array(surface['pose']['position'])+basis[:, 0]*((x/1920-.5)*4.6)
                np.testing.assert_allclose(point, [world_x, 0, -3], atol=1e-7)
                px, py = screenshot_pixel(self.state, capture, x, 540)
                self.assertAlmostEqual(px, (world_x/3+1)*500-.5)
                self.assertAlmostEqual(py, 499.5)
        self.assertNotEqual(ui_surface(self.state, 264, 540)['pose']['orientation'],
                            ui_surface(self.state, 1656, 540)['pose']['orientation'])

    def test_single_center_and_legacy_surfaces_remain_compatible(self):
        expected = ui_surface(self.legacy)
        frame = self.legacy['vr']['frame']
        frame['panels'] = [dict(id='center', canvas_rect=[0, 0, 1920, 1080],
                                panel=copy.deepcopy(frame['panel']), panel_width=4.6, panel_height=2.5875)]
        self.assertEqual(ui_surface(self.legacy), expected)
        self.assertEqual(ui_surface(self.legacy, 400, 200), expected)


if __name__ == '__main__':
    unittest.main()
