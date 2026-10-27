"""Metric checks for real submitted UI surfaces, shared by native ray tests.

Input is capture_screenshot/hover_ui_at geometry from the running XR compositor;
no model of the application's rendering substitutes for submitted surfaces.
"""
import numpy as np
from flatout_ui_motion_policy import HOVER_LIFT_METERS
from flatout_livery_e2e import quaternion_matrix


def ui_planes(capture):
    planes = [s for s in capture['geometry']['surfaces'] if s['visibility'] == 0]
    assert planes, 'No submitted both-eye UI plane'
    base = max(planes, key=lambda s: np.prod(s['size']))
    return base, [s for s in planes if s['id'] != base['id']]


def assert_hover_lift(capture, expected=HOVER_LIFT_METERS):
    base, tiles = ui_planes(capture)
    assert tiles, 'Hovered control remains flattened into its source UI plane'
    normal = quaternion_matrix(base['pose']['orientation'])[:, 2]
    lifts = [float(np.dot(np.array(s['pose']['position']) - base['pose']['position'], normal)) for s in tiles]
    assert any(abs(lift - expected) < .001 for lift in lifts), (expected, lifts)
    assert all(-.0001 <= lift <= expected + .001 for lift in lifts), lifts
    return {'lift_meters': lifts, 'base': base, 'tiles': tiles}


def assert_reading_distance(capture, reference_distance, reference_width):
    base, _ = ui_planes(capture)
    head = np.array(capture['geometry']['head']['position'])
    normal = quaternion_matrix(base['pose']['orientation'])[:, 2]
    distance = abs(float(np.dot(np.array(base['pose']['position']) - head, normal)))
    assert abs(distance - reference_distance) < .01, (distance, reference_distance)
    assert abs(base['size'][0] - reference_width) < .01, (base['size'][0], reference_width)
    return {'distance_meters': distance, 'width_meters': base['size'][0]}
