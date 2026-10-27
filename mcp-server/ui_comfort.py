"""Read-only spatial UI review from immutable screenshot geometry.

Render-frustum coverage is measured. Comfort zones are configurable design
guidance, not a medical assessment or a simulation of through-the-lens vision.
OpenXR coordinates are metres, +Y up, -Z forward; quaternions are [x,y,z,w].
"""
import copy
import math
import time
from io import BytesIO

from PIL import Image, ImageDraw
from ui_targeting import rotate, validate_surface


SOURCES = [
    {"id": "microsoft-comfort", "url": "https://learn.microsoft.com/en-us/windows/mixed-reality/design/comfort",
     "guidance": "Main content region 0–35 degrees below horizon; resting gaze 10–20 degrees below. Review angles above +10, below -60, or beyond 45 degrees horizontally. These are design recommendations, not individual eye or neck limits."},
    {"id": "meta-indirect-ui", "url": "https://developers.meta.com/vr/design/hands-ui-best-practices/",
     "guidance": "Indirect hand/ray UI: roughly 0.8–3 m; target colliders roughly 2.5–3 degrees. This hand-interaction guidance is an advisory reference for controller-ray review, not a controller-specific pass/fail standard."},
    {"id": "meta-render-vs-visible", "url": "https://developers.meta.com/vr/essentials/field-of-view/",
     "guidance": "Rendered extent differs from visible lens extent. Fit and gaze can reduce what a person sees. Use actual per-eye view data and validate critical UI on the device."},
]

DEFAULT_PROFILE = {
    "name": "ray_menu_review",
    "preferred_yaw_deg": [-30, 30],
    "preferred_elevation_deg": [-35, 0],
    "review_yaw_deg": [-45, 45],
    "review_elevation_deg": [-60, 10],
    "resting_elevation_deg": [-20, -10],
    "distance_m": [.8, 3],
    "min_control_angle_deg": 3,
}

LIMITATIONS = [
    "Frustum coverage measures submitted geometry only. It does not prove lens visibility, occlusion, text readability, eye-tracking accuracy, or human comfort.",
    "Comfort thresholds are advisory and configurable. Preferred horizontal +/-30 degrees is this project's review policy; it is not a universal published human limit.",
    "The default comfort reference removes captured head pitch/roll and retains heading in XR local +Y-up space. Supply the menu-opening reference_pose to assess a saved neutral facing direction.",
    "Overlay gaze guides are orientation-only angular lines, without depth parallax. Target visibility and angular measurements use each target's actual 3D position.",
    "Automatic quad targets cover the full submitted canvas, including any transparent padding. Supply actual control rectangles to assess populated UI rather than empty canvas extents.",
    "Scene occlusion, other UI, controller models, backface/material culling, lens masks, visual acuity and animation are not inferred. Test seated/standing users and the physical device.",
]


def guidelines():
    return {"profile": copy.deepcopy(DEFAULT_PROFILE), "sources": copy.deepcopy(SOURCES),
            "units": "degrees and metres; elevation positive up", "limitations": list(LIMITATIONS)}


def _numbers(value, count, label):
    if not isinstance(value, (list, tuple)) or len(value) != count or any(
            isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in value):
        raise ValueError(label + " requires " + str(count) + " finite numbers")
    return list(value)


def _pose(value):
    if not isinstance(value, dict):
        raise ValueError("pose must be an object")
    p = _numbers(value.get("position"), 3, "position")
    q = _numbers(value.get("orientation"), 4, "orientation")
    if abs(sum(x*x for x in q)-1) > .001:
        raise ValueError("orientation must be a unit quaternion [x,y,z,w]")
    return {"position": p, "orientation": q}


def _profile(overrides):
    profile = copy.deepcopy(DEFAULT_PROFILE)
    if overrides is None:
        return profile
    if not isinstance(overrides, dict) or set(overrides)-set(profile):
        raise ValueError("Unknown comfort profile option")
    profile.update(overrides)
    if not isinstance(profile['name'], str) or not 1 <= len(profile['name']) <= 80:
        raise ValueError("profile name must be 1–80 characters")
    for key in ('preferred_yaw_deg', 'review_yaw_deg'):
        a, b = _numbers(profile[key], 2, key)
        if not -89 < a < b < 89:
            raise ValueError(key + " must be an ordered interval within (-89,89)")
    for key in ('preferred_elevation_deg', 'review_elevation_deg', 'resting_elevation_deg'):
        a, b = _numbers(profile[key], 2, key)
        if not -89 < a < b < 89:
            raise ValueError(key + " must be an ordered interval within (-89,89)")
    a, b = _numbers(profile['distance_m'], 2, 'distance_m')
    if not 0 < a < b:
        raise ValueError("distance_m must be positive and ordered")
    size = profile['min_control_angle_deg']
    if isinstance(size, bool) or not isinstance(size, (int, float)) or not math.isfinite(size) or not 0 < size < 90:
        raise ValueError("min_control_angle_deg must be within (0,90)")
    for preferred, outer in (('preferred_yaw_deg', 'review_yaw_deg'), ('preferred_elevation_deg', 'review_elevation_deg')):
        if profile[preferred][0] < profile[outer][0] or profile[preferred][1] > profile[outer][1]:
            raise ValueError("Preferred intervals must fit inside the outer review envelope")
    return profile


def _inverse(q):
    return [-q[0], -q[1], -q[2], q[3]]


def _local(point, pose):
    return rotate(_inverse(pose['orientation']), [a-b for a, b in zip(point, pose['position'])])


def _world(point, pose):
    return [a+b for a, b in zip(rotate(pose['orientation'], point), pose['position'])]


def _reference(head, mode, explicit):
    if explicit is not None:
        return _pose(explicit), 'explicit saved neutral pose'
    if mode not in ('level_head', 'captured_head'):
        raise ValueError("reference_mode must be level_head or captured_head")
    if mode == 'captured_head':
        return copy.deepcopy(head), 'captured head direction; not an independently known ergonomic neutral'
    forward = rotate(head['orientation'], [0, 0, -1])
    if math.hypot(forward[0], forward[2]) < 1e-7:
        raise ValueError("Vertical head direction has no level heading; supply reference_pose")
    yaw = math.atan2(-forward[0], -forward[2])
    return {'position': list(head['position']), 'orientation': [0, math.sin(yaw/2), 0, math.cos(yaw/2)]}, 'level current heading in XR local +Y-up space'


def _eye(value):
    eye = copy.deepcopy(value)
    if eye.get('eye') not in ('left', 'right'):
        raise ValueError("An eye must be left or right")
    eye['pose'] = _pose(eye.get('pose'))
    l, r, u, d = _numbers(eye.get('fov'), 4, 'eye fov [left,right,up,down]')
    if not -math.pi/2 < l < r < math.pi/2 or not -math.pi/2 < d < u < math.pi/2:
        raise ValueError("Invalid per-eye FOV")
    x, y, w, h = _numbers(eye.get('viewport'), 4, 'eye viewport')
    if w <= 0 or h <= 0:
        raise ValueError("Eye viewport must have positive size")
    u0, v0, u1, v1 = _numbers(eye.get('uv', [0, 0, 1, 1]), 4, 'eye uv')
    if u0 == u1 or v0 == v1 or min(u0, v0, u1, v1) < 0 or max(u0, v0, u1, v1) > 1:
        raise ValueError("Eye UV must address a nonempty submitted imageRect")
    return eye


def _eyes(geometry, kind):
    eyes = [_eye(e) for e in geometry.get(kind, [])]
    if len(eyes) != 2 or {e['eye'] for e in eyes} != {'left', 'right'}:
        raise ValueError("Both " + kind + " are required; no guessed fallback camera is used")
    return eyes


def _clip(poly, planes):
    for fn in planes:
        out = []
        for a, b in zip(poly, poly[1:] + poly[:1]):
            fa, fb = fn(a), fn(b)
            if fa >= 0:
                out.append(a)
            if (fa >= 0) != (fb >= 0):
                t = fa/(fa-fb)
                out.append([x+t*(y-x) for x, y in zip(a, b)])
        poly = out
        if not poly:
            break
    return poly


def _eye_planes(eye):
    l, r, u, d = map(math.tan, eye['fov'])
    u0, v0, u1, v1 = eye.get('uv', [0, 0, 1, 1])
    left, right = l+min(u0, u1)*(r-l), l+max(u0, u1)*(r-l)
    bottom, top = u+max(v0, v1)*(d-u), u+min(v0, v1)*(d-u)
    return [lambda p: -p[2]-1e-6, lambda p: p[0]+left*p[2], lambda p: -p[0]-right*p[2],
            lambda p: p[1]+bottom*p[2], lambda p: -p[1]-top*p[2]]


def _pixel(local, eye, capture):
    l, r, u, d = map(math.tan, eye['fov'])
    u0, v0, u1, v1 = eye.get('uv', [0, 0, 1, 1])
    ex, ey, ew, eh = eye['viewport']
    x = ex+((local[0]/-local[2]-l)/(r-l)-u0)/(u1-u0)*ew
    y = ey+((local[1]/-local[2]-u)/(d-u)-v0)/(v1-v0)*eh
    cx, cy, cw, ch = capture['crop']
    w, h = capture['image_size']
    return [(x-cx)/cw*w-.5, (y-cy)/ch*h-.5]


def _viewport_image(eye, capture):
    cx, cy, cw, ch = capture['crop']
    w, h = capture['image_size']
    x, y, ew, eh = eye['viewport']
    return [max(0., (x-cx)/cw*w), max(0., (y-cy)/ch*h),
            min(float(w), (x+ew-cx)/cw*w), min(float(h), (y+eh-cy)/ch*h)]


def _coverage(corners, eye, capture, submitted):
    viewport = _viewport_image(eye, capture)
    result = {'status': 'not_submitted', 'viewport_image_rect': viewport, 'clipped_polygon_image': [],
              'eye_in_returned_image': viewport[2] > viewport[0] and viewport[3] > viewport[1]}
    if not submitted:
        return result
    points = [_local(p, eye['pose']) for p in corners]
    planes = _eye_planes(eye)
    clipped = _clip(points, planes)
    inside = all(all(fn(p) >= -1e-8 for fn in planes) for p in points)
    result['status'] = 'full' if inside else 'partial' if len(clipped) >= 3 else 'outside'
    result['clipped_polygon_image'] = [_pixel(p, eye, capture) for p in clipped]
    return result


def _angles(point):
    x, y, z = point
    return [math.degrees(math.atan2(x, -z)), math.degrees(math.atan2(y, math.hypot(x, z)))]


def _angular_bounds(points):
    # Perspective angular extrema occur at a vertex or an elevation stationary
    # point on an edge, for this convex rectangle entirely in the forward half-space.
    if any(p[2] >= -1e-7 for p in points):
        return None
    samples = list(points)
    for a, b in zip(points, points[1:] + points[:1]):
        dx, dy, dz = [y-x for x, y in zip(a, b)]
        aa, ab, dd = a[0]**2+a[2]**2, a[0]*dx+a[2]*dz, dx*dx+dz*dz
        numerator, denominator = a[1]*ab-dy*aa, dy*ab-a[1]*dd
        if abs(denominator) > 1e-12:
            t = numerator/denominator
            if 0 < t < 1:
                samples.append([a[0]+t*dx, a[1]+t*dy, a[2]+t*dz])
    angles = [_angles(p) for p in samples]
    return {'yaw': [min(a[0] for a in angles), max(a[0] for a in angles)],
            'elevation': [min(a[1] for a in angles), max(a[1] for a in angles)]}


def _angle_between(a, b):
    aa, bb = math.sqrt(sum(x*x for x in a)), math.sqrt(sum(x*x for x in b))
    if min(aa, bb) < 1e-9:
        raise ValueError("UI reaches the head origin")
    return math.degrees(math.acos(max(-1., min(1., sum(x*y for x, y in zip(a, b))/(aa*bb)))))


def _target(value, geometry):
    if not isinstance(value, dict) or set(value)-{'name', 'role', 'surface', 'surface_id', 'rect', 'canvas_size'}:
        raise ValueError("Unknown UI target property")
    name = value.get('name', 'UI')
    if not isinstance(name, str) or not 1 <= len(name) <= 128:
        raise ValueError("Target name must be 1–128 characters")
    role = value.get('role', 'control')
    if role not in ('panel', 'control'):
        raise ValueError("Target role must be panel or control")
    if ('surface' in value) == ('surface_id' in value):
        raise ValueError("Each target needs exactly one surface or surface_id")
    if 'surface' in value:
        surface, kind = copy.deepcopy(value['surface']), 'projection_eyes'
    else:
        if isinstance(value['surface_id'], bool) or not isinstance(value['surface_id'], int):
            raise ValueError("surface_id must be an integer")
        found = [s for s in geometry.get('surfaces', []) if s.get('id') == value['surface_id']]
        if len(found) != 1:
            raise ValueError("surface_id must identify one captured composition quad")
        surface, kind = copy.deepcopy(found[0]), 'eyes'
    validate_surface(surface)
    _pose(surface['pose'])
    width, height = _numbers(surface['size'], 2, 'surface size')
    cw, ch = _numbers(value.get('canvas_size', [1, 1]), 2, 'canvas_size')
    if min(cw, ch) <= 0:
        raise ValueError("canvas_size must be positive")
    l, t, r, b = _numbers(value.get('rect', [0, 0, cw, ch]), 4, 'rect')
    if not 0 <= l < r <= cw or not 0 <= t < b <= ch:
        raise ValueError("rect must be a nonempty region inside canvas_size")
    visibility = surface.get('visibility', 0)
    if visibility not in (0, 1, 2):
        raise ValueError("Unknown surface eye visibility")
    corners = [_world([(x/cw-.5)*width, (.5-y/ch)*height, 0], surface['pose']) for x, y in ((l, t), (r, t), (r, b), (l, b))]
    return name, role, surface, kind, corners, [l/cw, t/ch, r/cw, b/ch]


def analyze_visibility(capture, targets=None, profile=None, reference_mode='level_head', reference_pose=None):
    if not isinstance(capture, dict) or not capture.get('geometry', {}).get('supported'):
        raise ValueError("Capture needs supported frame-matched XR geometry")
    width, height = _numbers(capture.get('image_size'), 2, 'image_size')
    crop = _numbers(capture.get('crop'), 4, 'crop')
    if min(width, height, crop[2], crop[3]) <= 0:
        raise ValueError("Capture image and crop sizes must be positive")
    geometry = capture['geometry']
    compositor_eyes = _eyes(geometry, 'eyes')
    head = _pose(geometry.get('head'))
    reference, reference_label = _reference(head, reference_mode, reference_pose)
    rules = _profile(profile)
    if targets is None:
        targets = [{'name': 'quad.'+str(s['id']), 'role': 'panel', 'surface_id': s['id']} for s in geometry.get('surfaces', [])]
    if not isinstance(targets, list) or len(targets) > 256:
        raise ValueError("targets must be an array of at most 256 UI regions")
    report = {'status': 'analyzed', 'capture_id': capture.get('capture_id'), 'capture_created_at': capture.get('created_at'),
              'capture_age_seconds': max(0., time.time()-capture['created_at']) if isinstance(capture.get('created_at'), (int, float)) else None,
              'session_id': geometry.get('session_id'), 'frame_id': geometry.get('frame_id'), 'read_only': True,
              'reference': {'pose': reference, 'description': reference_label}, 'captured_head_pose': head,
              'measurement_reference': 'Placement angles use the selected reference. Distance and edge-midpoint angular size use the actual captured head position.',
              'profile': rules, 'sources': copy.deepcopy(SOURCES),
              'limitations': list(LIMITATIONS), 'targets': [], 'eye_frusta': {}}
    report['eye_frusta']['eyes'] = [{'eye': e['eye'], 'fov_deg_left_right_up_down': [math.degrees(a) for a in e['fov']],
                                     'pose': e['pose'], 'viewport': e['viewport'], 'uv': e.get('uv', [0, 0, 1, 1])} for e in compositor_eyes]
    for value in targets:
        name, role, surface, kind, corners, rect_uv = _target(value, geometry)
        eyes = _eyes(geometry, kind)
        report['eye_frusta'][kind] = [{'eye': e['eye'], 'fov_deg_left_right_up_down': [math.degrees(a) for a in e['fov']],
                                     'pose': e['pose'], 'viewport': e['viewport'], 'uv': e.get('uv', [0, 0, 1, 1])} for e in eyes]
        local = [_local(p, reference) for p in corners]
        center = [sum(p[i] for p in corners)/4 for i in range(3)]
        center_local = _local(center, reference)
        viewing_local = [_local(p, head) for p in corners]
        viewing_center = _local(center, head)
        bounds = _angular_bounds(local)
        center_angles = _angles(center_local)
        midpoint = lambda a, b: [(x+y)/2 for x, y in zip(a, b)]
        angle_size = {'width': _angle_between(midpoint(viewing_local[0], viewing_local[3]), midpoint(viewing_local[1], viewing_local[2])),
                      'height': _angle_between(midpoint(viewing_local[0], viewing_local[1]), midpoint(viewing_local[3], viewing_local[2]))}
        distance = math.sqrt(sum(x*x for x in viewing_center))
        visibility = {e['eye']: _coverage(corners, e, capture, surface.get('visibility', 0) in (0, 1 if e['eye'] == 'left' else 2)) for e in eyes}
        warnings = []
        if bounds is None:
            warnings.append('crosses_or_behind_reference_forward_plane')
        else:
            for axis, interval, low, high in (('yaw', 'preferred_yaw_deg', 'left_of_preferred_zone', 'right_of_preferred_zone'),
                                             ('elevation', 'preferred_elevation_deg', 'below_preferred_zone', 'above_preferred_zone')):
                if bounds[axis][0] < rules[interval][0]-1e-6:
                    warnings.append(low)
                if bounds[axis][1] > rules[interval][1]+1e-6:
                    warnings.append(high)
            if any(bounds[axis][0] < rules[key][0] or bounds[axis][1] > rules[key][1]
                   for axis, key in (('yaw', 'review_yaw_deg'), ('elevation', 'review_elevation_deg'))):
                warnings.append('outside_outer_review_envelope')
        if distance < rules['distance_m'][0]:
            warnings.append('nearer_than_distance_guidance')
        if distance > rules['distance_m'][1]:
            warnings.append('farther_than_distance_guidance')
        if role == 'control' and min(angle_size.values()) < rules['min_control_angle_deg']:
            warnings.append('small_angular_control_target')
        current_angles = _angles(viewing_center)
        report['targets'].append({'index': len(report['targets'])+1, 'name': name, 'role': role, 'camera_kind': kind, 'rect_uv': rect_uv, 'world_corners': corners,
                                  'center_angle_deg': {'yaw': center_angles[0], 'elevation': center_angles[1]},
                                  'current_head_center_angle_deg': {'yaw': current_angles[0], 'elevation': current_angles[1]},
                                  'angular_bounds_deg': bounds, 'angular_size_deg': angle_size, 'center_distance_m': distance,
                                  'render_visibility': visibility, 'fully_in_both_submitted_eye_frusta': all(v['status'] == 'full' for v in visibility.values()),
                                  'placement_advisories': warnings, 'preferred_gaze_zone': bounds is not None and not any(
                                      w.endswith('_preferred_zone') for w in warnings)})
    report['summary'] = {'target_count': len(report['targets']),
                         'fully_in_both_eye_frusta': sum(t['fully_in_both_submitted_eye_frusta'] for t in report['targets']),
                         'outside_preferred_gaze_zone': sum(not t['preferred_gaze_zone'] for t in report['targets']),
                         'with_advisories': sum(bool(t['placement_advisories']) for t in report['targets'])}
    if not targets:
        report['note'] = 'No captured composition quads. Supply explicit XR-local surfaces for world-space UI; scene depth is never guessed.'
    return report


def _guide_points(yaw_range, elevation_range):
    left, right = yaw_range
    bottom, top = elevation_range
    return [(left+(right-left)*i/60, top) for i in range(61)] + [(right, top+(bottom-top)*i/60) for i in range(61)] + [
        (right+(left-right)*i/60, bottom) for i in range(61)] + [(left, bottom+(top-bottom)*i/60) for i in range(61)]


def annotate_visibility(source_pixels, capture, report):
    """Return a separate diagnostic PNG; source bytes and capture are untouched."""
    image = Image.open(BytesIO(source_pixels)).convert('RGB')
    if list(image.size) != list(capture['image_size']):
        raise ValueError("Source pixels do not match this capture")
    reference = report['reference']['pose']
    rules = report['profile']
    kinds = {t['camera_kind'] for t in report['targets']} or {'eyes'}
    colors = {'preferred': '#50e6b0', 'outer': '#ffbd45', 'resting': '#9ebcff'}
    # Each camera/eye draws into its own crop, preventing any line from crossing
    # the stereo seam or the image crop into the other eye.
    for kind in sorted(kinds):
        for eye in _eyes(capture['geometry'], kind):
            viewport = _viewport_image(eye, capture)
            l, t, r, b = [int(round(v)) for v in viewport]
            if r <= l or b <= t:
                continue
            tile = image.crop((l, t, r, b))
            draw = ImageDraw.Draw(tile)
            draw.rectangle((0, 0, r-l-1, b-t-1), outline='#42c6ff', width=2)
            for yaw, elev, color in ((rules['preferred_yaw_deg'], rules['preferred_elevation_deg'], colors['preferred']),
                                     (rules['review_yaw_deg'], rules['review_elevation_deg'], colors['outer']),
                                     (rules['preferred_yaw_deg'], rules['resting_elevation_deg'], colors['resting'])):
                previous = None
                for az, el in _guide_points(yaw, elev):
                    az, el = math.radians(az), math.radians(el)
                    direction = rotate(reference['orientation'], [math.sin(az)*math.cos(el), math.sin(el), -math.cos(az)*math.cos(el)])
                    direction = rotate(_inverse(eye['pose']['orientation']), direction)
                    if direction[2] >= -1e-5:
                        previous = None
                        continue
                    x, y = _pixel(direction, eye, capture)
                    point = (x-l, y-t)
                    if previous:
                        draw.line((previous, point), fill=color, width=2)
                    previous = point
            for target in report['targets']:
                if target['camera_kind'] != kind:
                    continue
                region = target['render_visibility'][eye['eye']]
                polygon = [(x-l, y-t) for x, y in region['clipped_polygon_image']]
                if len(polygon) < 3:
                    continue
                color = '#ff6868' if region['status'] != 'full' else '#ffc25a' if target['placement_advisories'] else '#79eeae'
                draw.line(polygon+polygon[:1], fill=color, width=2)
                x, y = polygon[0]
                tag = '#'+str(target['index'])
                tx, ty = max(4, min(r-l-32, x)), max(60, min(b-t-16, y))
                draw.rectangle((tx-1, ty-1, tx+26, ty+12), fill='#10202a')
                draw.text((tx, ty), tag, fill=color)
            # Scale the legend with the eye preview, without relying on a
            # machine-specific system font or Pillow's newer font-size API.
            legend = Image.new('RGB', (520, 56), '#10202a')
            labels = ImageDraw.Draw(legend)
            labels.text((5, 3), 'DIAGNOSTIC GUIDE | '+eye['eye']+' | '+kind, fill='white')
            labels.text((5, 16), 'cyan: viewport | green: preferred | blue: resting', fill='white')
            labels.text((5, 28), 'targets: green=in profile; amber=advisory; red=clipped; #=JSON order', fill='white')
            labels.text((5, 40), 'amber boundary: outer guidance; lens visibility not simulated', fill='white')
            scale = min(1.8, max(1., (r-l)/640), (r-l-8)/520)
            if scale > 0:
                legend = legend.resize((max(1, round(520*scale)), max(1, round(56*scale))), Image.Resampling.LANCZOS)
                tile.paste(legend, (4, 4))
            image.paste(tile, (l, t))
    output = BytesIO()
    image.save(output, 'PNG')
    return output.getvalue()


def tool_specs():
    numbers = lambda n: {'type': 'array', 'items': {'type': 'number'}, 'minItems': n, 'maxItems': n}
    pose = {'type': 'object', 'properties': {'position': numbers(3), 'orientation': numbers(4)},
            'required': ['position', 'orientation'], 'additionalProperties': False}
    surface = {'type': 'object', 'properties': {'pose': pose, 'size': numbers(2)}, 'required': ['pose', 'size'], 'additionalProperties': False}
    target = {'type': 'object', 'properties': {'name': {'type': 'string', 'maxLength': 128}, 'role': {'enum': ['panel', 'control']},
              'surface_id': {'type': 'integer'}, 'surface': surface, 'rect': numbers(4), 'canvas_size': numbers(2)},
              'oneOf': [{'required': ['surface_id']}, {'required': ['surface']}], 'additionalProperties': False}
    profile = {'type': 'object', 'properties': {k: {'type': 'string'} if isinstance(v, str) else numbers(2) if isinstance(v, list) else {'type': 'number'}
                                               for k, v in DEFAULT_PROFILE.items()}, 'additionalProperties': False}
    return [
        {'name': 'get_ui_comfort_guidelines', 'description': 'Read sourced VR placement guidance, configurable review thresholds and limits. Does not read or change a runtime.',
         'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
        {'name': 'analyze_ui_visibility', 'description': 'Read-only spatial UI review on an existing capture: actual per-eye frustum clipping, head-relative angles, distance, target angular size and separate advisory comfort zones. Returns a diagnostic overlay without moving the headset or injecting input. Rendering coverage does not prove lens visibility, occlusion or legibility.',
         'inputSchema': {'type': 'object', 'properties': {'capture_id': {'type': 'string'}, 'targets': {'type': 'array', 'items': target, 'maxItems': 256},
                          'profile': profile, 'reference_mode': {'enum': ['level_head', 'captured_head'], 'default': 'level_head'},
                          'reference_pose': pose, 'include_overlay': {'type': 'boolean', 'default': True}}, 'required': ['capture_id'], 'additionalProperties': False}}
    ]
