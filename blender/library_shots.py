"""The studio library's 3D shot types (templates), generalised from the Ten Frames and Bowling IQ shot scripts.

A template builds one plate: it animates the scene and a camera over the plate's length and returns a Plate: the scene
frames to render, the camera, the motion-blur shutter, the subjects whose screen boxes go into the frame data, and the
key moments. Every plate is rendered with a 20% margin (1296 x 2304); the centred 1080 x 1920 window frames the shot as
designed, so compose can reframe or push without rendering again. Imported by render_item.py, which sets the arguments
iq_shots reads (template, samples, pocket, speed) before importing this module.
"""

import math
from dataclasses import dataclass
from dataclasses import field

import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Matrix
from mathutils import Quaternion
from mathutils import Vector

import iq_shots as iq
import lane_scene as ls

TEMPLATE_VERSION = 3
MARGIN = 1.2
SAFE_W, SAFE_H = ls.WIDTH, ls.HEIGHT
PLATE_W, PLATE_H = round(SAFE_W * MARGIN), round(SAFE_H * MARGIN)
CROP_X, CROP_Y = (PLATE_W - SAFE_W) // 2, (PLATE_H - SAFE_H) // 2
FPS, FT, IN, BALL_R, LANE_LEN, PITCH = ls.FPS, ls.FT, ls.IN, ls.BALL_R, ls.LANE_LEN, ls.PITCH
RETURN_X = PITCH / 2
RAIL_GAP, RAIL_R, RAIL_Z = 0.12, 0.012, 0.63
RETURN_BALL = Vector((RETURN_X, -5.65, RAIL_Z + math.sqrt((BALL_R + RAIL_R) ** 2 - (RAIL_GAP / 2) ** 2)))
MONITOR = dict(y=-3.4, bottom=2.45, top=2.97, half=0.46)
MONITOR_X = RETURN_X - (MONITOR['half'] + 0.02)
ARROW = Vector((ls.board_x(10), 13.75 * FT, 0.0))  # the second arrow from the right (board 10)
PIN_CENTRE_OF_GRAVITY = 5.6 * IN  # USBC range 5 5/16 to 5 15/16 in
STRIKE_SLOW = 0.1  # the simulation runs at a tenth of real speed: every 10th simulation frame is real time
SPEED_STEPS = dict(real_time=10, slow_2x=5, slow_5x=2, slow_10x=1)  # simulation frames per plate frame
SPEED_SHUTTER = dict(real_time=5.0, slow_2x=2.5, slow_5x=1.0, slow_10x=0.5)  # half a plate frame, in scene frames


@dataclass
class Plate:
    camera: bpy.types.Object
    scene_frames: list
    shutter: float = 0.5
    subjects: dict = field(default_factory=dict)  # name -> list of objects (box of their union) or a Vector
    required: list = field(default_factory=list)
    markers: dict = field(default_factory=dict)  # name -> plate frame (1-based)
    points: dict = field(default_factory=dict)  # name -> list of world points exported per frame (rings, glass corners)
    notes: dict = field(default_factory=dict)  # measured facts (simulation result, entry angle)


# ---------------------------------------------------------------- helpers

def ease(value: float) -> float:
    clamped = min(max(value, 0.0), 1.0)
    return clamped * clamped * (3 - 2 * clamped)


def frames_of(params: dict, default_s: float) -> int:
    return max(2, round(float(params.get('duration_s', default_s)) * FPS))


_TURNS = dict()


def key_camera(cam: bpy.types.Object, eye: Vector, target: Vector, frame: int) -> None:
    cam.rotation_mode = 'QUATERNION'
    turn = (Vector(target) - Vector(eye)).to_track_quat('-Z', 'Y')
    previous = _TURNS.get(cam.name)
    # keep each key on the same side as the last one, so the blur never interpolates the long way round
    if previous is not None and turn.dot(previous) < 0:
        turn.negate()
    _TURNS[cam.name] = turn.copy()
    cam.location = eye
    cam.rotation_quaternion = turn
    cam.keyframe_insert('location', frame=frame)
    cam.keyframe_insert('rotation_quaternion', frame=frame)


def camera(name: str, lens: float, fstop: float | None) -> bpy.types.Object:
    """A camera whose sensor carries the 20% margin, so the centred safe window keeps the designed framing."""
    cam = iq.camera(name, lens, fstop)
    cam.data.sensor_height = cam.data.sensor_height * MARGIN
    return cam


def project(cam: bpy.types.Object, point: Vector) -> tuple:
    view = world_to_camera_view(bpy.context.scene, cam, Vector(point))
    return (view.x * PLATE_W, (1 - view.y) * PLATE_H, view.z)


def hold_on_screen(cam: bpy.types.Object, point: Vector, safe_target: tuple, frame: int) -> None:
    """Solve the lens shift that holds a world point at a position in the safe window, for one frame."""
    bpy.context.scene.frame_set(frame)
    target_x, target_y = safe_target[0] + CROP_X, safe_target[1] + CROP_Y
    for _ in range(8):
        x, y, _depth = project(cam, point)
        cam.data.shift_x += (x - target_x) / PLATE_H
        cam.data.shift_y -= (y - target_y) / PLATE_H
    cam.data.keyframe_insert('shift_x', frame=frame)
    cam.data.keyframe_insert('shift_y', frame=frame)


def hide_ball(parts: dict) -> None:
    parts['ball'].hide_render = True


def rim_light(name: str) -> None:
    ls.add_area(name, (0.6, 0.6), 45, (0.9, LANE_LEN + 0.4, 0.6), (0, LANE_LEN, 0.25))


def key_positions(ball: bpy.types.Object, positions: list, axes: list, step_angle: float) -> None:
    turn = Quaternion((1.0, 0.0, 0.0, 0.0))
    for index, position in enumerate(positions):
        if index:
            turn = Quaternion(axes[index], step_angle) @ turn
        ball.location, ball.rotation_quaternion = position, turn
        ball.keyframe_insert('location', frame=index + 1)
        ball.keyframe_insert('rotation_quaternion', frame=index + 1)


# ---------------------------------------------------------------- the additions to the house

def disc(collection: bpy.types.Collection, material: bpy.types.Material, x: float, y: float, radius: float) -> None:
    steps = 24
    verts = [(x, y, 0.0004)] + [(x + radius * math.cos(2 * math.pi * index / steps), y + radius * math.sin(2 * math.pi * index / steps), 0.0004)
                                for index in range(steps)]
    faces = [(0, 1 + index, 1 + (index + 1) % steps) for index in range(steps)]
    mesh = bpy.data.meshes.new('dot')
    mesh.from_pydata(verts, list(), faces)
    mesh.materials.append(material)
    collection.objects.link(bpy.data.objects.new('dot', mesh))


def add_dots() -> None:
    """Approach dots 12 and 14.5 ft behind the foul line, guide dots 7 ft down the lane."""
    lane, inlay = bpy.data.collections['lane'], bpy.data.materials['inlay']
    for feet in (-12.0, -14.5):
        for board in (3, 5, 8, 11, 14, 20, 26, 29, 32, 35, 37):
            disc(lane, inlay, ls.board_x(board), feet * FT, 0.5 * IN)
    for board in (3, 5, 8, 11, 14, 26, 29, 32, 35, 37):
        disc(lane, inlay, ls.board_x(board), 7 * FT, 0.25 * IN)


def bevel(obj: bpy.types.Object, width: float) -> None:
    modifier = obj.modifiers.new('bevel', 'BEVEL')
    modifier.width, modifier.segments = width, 4


def add_return() -> None:
    """The ball return between the centre lane and its neighbour: body, hood, two chrome rails, a spare ball."""
    house = bpy.data.collections['house']
    body_material = ls.plain_material('return_body', (0.06, 0.065, 0.07), 0.35, coat=0.5)
    chrome = ls.plain_material('chrome', (0.85, 0.85, 0.87), 0.12, metallic=1.0)
    bevel(ls.add_box('return_body', (0.44, 1.3, RAIL_Z - 0.03), (RETURN_X, -5.6, (RAIL_Z - 0.03) / 2), body_material, house), 0.03)
    bevel(ls.add_box('return_hood', (0.44, 0.38, 0.32), (RETURN_X, -4.98, RAIL_Z + 0.12), body_material, house), 0.06)
    for side in (-1, 1):
        bpy.ops.mesh.primitive_cylinder_add(radius=RAIL_R, depth=1.0, vertices=32, location=(RETURN_X + side * RAIL_GAP / 2, -5.7, RAIL_Z),
                                            rotation=(math.pi / 2, 0, 0))
        bpy.context.active_object.data.materials.append(chrome)
    bpy.ops.mesh.primitive_uv_sphere_add(radius=BALL_R, segments=96, ring_count=48, location=(RETURN_X, -5.31, RETURN_BALL.z))
    spare = bpy.context.active_object
    bpy.ops.object.shade_smooth()
    spare.data.materials.append(ls.plain_material('spare_ball', (0.05, 0.09, 0.22), 0.1, coat=1.0))


def add_monitors() -> None:
    """Two lane monitors hanging over the back of the approach, screens facing the settee."""
    house = bpy.data.collections['house']
    body = ls.plain_material('monitor_body', (0.06, 0.065, 0.07), 0.35, coat=0.4)
    glass = ls.plain_material('monitor_glass', (0.008, 0.01, 0.014), 0.06, coat=1.0)
    height = MONITOR['top'] - MONITOR['bottom']
    for x in (MONITOR_X, RETURN_X + MONITOR['half'] + 0.02):
        ls.add_box('monitor', (2 * MONITOR['half'], 0.06, height), (x, MONITOR['y'] + 0.03, MONITOR['bottom'] + height / 2), body, house)
        ls.add_box('monitor_glass', (2 * MONITOR['half'] - 0.06, 0.004, height - 0.06), (x, MONITOR['y'] - 0.002, MONITOR['bottom'] + height / 2), glass, house)
        ls.add_box('monitor_pole', (0.04, 0.04, 1.2), (x, MONITOR['y'] + 0.03, MONITOR['top'] + 0.6), body, house)
    ls.add_area('monitor_rim', (2.4, 0.3), 30, (RETURN_X, MONITOR['y'] + 0.6, MONITOR['top'] + 0.5), (RETURN_X, MONITOR['y'], MONITOR['bottom']), color=(0.92, 0.95, 1.0))


def screen_corners(x: float) -> list:
    """The glass of a monitor, clockwise from top-left as seen from the settee."""
    left, right = x - MONITOR['half'] + 0.03, x + MONITOR['half'] - 0.03
    top, bottom, y = MONITOR['top'] - 0.03, MONITOR['bottom'] + 0.03, MONITOR['y'] - 0.004
    return [Vector((left, y, top)), Vector((right, y, top)), Vector((right, y, bottom)), Vector((left, y, bottom))]


def setup() -> dict:
    parts = iq.setup()
    add_dots()
    add_return()
    add_monitors()
    scene = parts['scene']
    scene.render.resolution_x, scene.render.resolution_y = PLATE_W, PLATE_H
    return parts


# ---------------------------------------------------------------- the house

def t_rack_lowering(parts: dict, params: dict) -> Plate:
    """The setting table lowers ten pins onto the deck; they settle with a small wobble; the table lifts away."""
    frames, pins = frames_of(params, 4.0), parts['pins']
    hide_ball(parts)
    touchdown = round(float(params.get('touchdown_s', 0.6)) * FPS) + 1
    lift_start, lift_end = touchdown + 13, touchdown + 27
    table = ls.add_box('setting_table', (1.1, 1.15, 0.06), (0, LANE_LEN + 0.42, 0.0),
                       ls.plain_material('table', (0.07, 0.07, 0.075), 0.45, metallic=0.6), bpy.context.scene.collection)
    rest = {pin.name: pin.location.copy() for pin in pins}
    phase = {pin.name: 1.7 * index for index, pin in enumerate(pins)}
    hold_height = 0.37  # pin height 0.381: the heads sit just inside the table's cups
    for frame in range(1, frames + 1):
        if frame <= touchdown:
            height = hold_height + 0.26 * (1 - ease((frame - 1) / max(1, touchdown - 1)) ** 0.7)
        elif frame < lift_start:
            height = hold_height
        else:
            height = hold_height + 0.75 * ease((frame - lift_start) / (lift_end - lift_start))
        table.location.z = height + 0.03
        table.keyframe_insert('location', frame=frame)
        for pin in pins:
            base = rest[pin.name]
            if frame <= touchdown:
                pin.location = (base.x, base.y, height - hold_height)
                pin.rotation_euler = (0, 0, 0)
            else:
                age = (frame - touchdown) / FPS
                wobble = 0.03 * math.exp(-age / 0.18) * math.sin(2 * math.pi * age / 0.22 + phase[pin.name])
                pin.location = base
                pin.rotation_euler = (wobble, 0.6 * wobble * math.cos(phase[pin.name]), 0)
            pin.keyframe_insert('location', frame=frame)
            pin.keyframe_insert('rotation_euler', frame=frame)
    cam = camera('cam_rack', 35, 2.8)
    eye, target = Vector((0.30, LANE_LEN - 1.25, 0.10)), Vector((0.0, LANE_LEN + 0.4, 0.22))
    key_camera(cam, eye, target, 1)
    cam.data.dof.focus_distance = (Vector((0, LANE_LEN, 0.2)) - eye).length
    rim_light('rack_rim')
    return Plate(cam, list(range(1, frames + 1)), subjects=dict(rack=list(pins)), required=['rack'], markers=dict(touchdown=touchdown))


def t_lane_push(parts: dict, params: dict) -> Plate:
    """High on the centre line, a slow push down the full 60 ft."""
    frames = frames_of(params, 5.0)
    hide_ball(parts)
    cam = camera('cam_lane', 28, None)
    for frame in range(1, frames + 1):
        progress = ease((frame - 1) / (frames - 1))
        key_camera(cam, Vector((0.0, -2.4 + 0.7 * progress, 2.9 - 0.1 * progress)), Vector((0.0, 13.0 + 0.5 * progress, 0.0)), frame)
    return Plate(cam, list(range(1, frames + 1)), subjects=dict(rack=list(parts['pins'])), required=['rack'])


def t_foul_line(parts: dict, params: dict) -> Plate:
    """Low on the foul line, looking along it across the house; the line runs from the frame's corner to the gutter."""
    frames = frames_of(params, 6.0)
    hide_ball(parts)
    cam = camera('cam_foul', 28, 4.0)
    for frame in range(1, frames + 1):
        progress = ease((frame - 1) / (frames - 1))
        eye = Vector((-0.50 + 0.25 * progress, -0.03, 0.07))
        key_camera(cam, eye, Vector((eye.x + 3.0, -0.01, 0.0)), frame)
    cam.data.dof.focus_distance = 0.6
    return Plate(cam, list(range(1, frames + 1)))


def t_return_orbit(parts: dict, params: dict) -> Plate:
    """Our ball waiting on the return; a slow orbit around it."""
    frames, ball = frames_of(params, 6.0), parts['ball']
    ball.location = RETURN_BALL
    ball.rotation_quaternion = (1, 0, 0, 0)
    cam = camera('cam_orbit', 40, 2.8)
    radius = 1.1
    for frame in range(1, frames + 1):
        angle = math.radians(242 + 46 * ease((frame - 1) / (frames - 1)))
        eye = RETURN_BALL + Vector((radius * math.cos(angle), radius * math.sin(angle), 0.24))
        key_camera(cam, eye, RETURN_BALL + Vector((0, 0, 0.01)), frame)
    cam.data.dof.focus_distance = math.hypot(radius, 0.24) - 0.8 * BALL_R
    ls.add_area('return_key', (0.5, 0.5), 28, (RETURN_X - 0.6, -6.2, 1.5), tuple(RETURN_BALL), color=(1.0, 0.94, 0.86))
    ls.add_area('return_rim', (0.4, 0.4), 30, (RETURN_X + 0.45, -5.0, 1.1), tuple(RETURN_BALL), color=(0.9, 0.95, 1.0))
    return Plate(cam, list(range(1, frames + 1)), subjects=dict(ball=[ball]), required=['ball'])


def stride(seconds: float, steps: list) -> float:
    """Continuous step count: k at the k-th footstep, linear in between."""
    marks = [-0.4] + steps
    for index in range(len(marks) - 1):
        if seconds < marks[index + 1]:
            return index + (seconds - marks[index]) / (marks[index + 1] - marks[index])
    return float(len(marks) - 1)


def t_walk_pov(parts: dict, params: dict) -> Plate:
    """The walk to the line through the bowler's eyes: four steps with a dip on each, the slide, then a hold at the line."""
    frames = frames_of(params, 4.0)
    hide_ball(parts)
    steps = [float(value) for value in params.get('steps_s', [0.10, 0.62, 0.98, 1.25])]
    walk_s = float(params.get('walk_s', 1.55))
    slide_from = steps[-1]
    cam = camera('cam_walk', 24, None)
    for frame in range(1, frames + 1):
        seconds = (frame - 1) / FPS
        walked = min(seconds, walk_s)
        y = -4.27 + 3.85 * ease(walked / walk_s)
        dip = sum(math.exp(-((seconds - step) / 0.07) ** 2) for step in steps[:-1])
        crouch = 0.22 * ease((walked - slide_from) / (walk_s - slide_from))
        side = 0.012 * math.cos(math.pi * stride(walked, steps))
        eye = Vector((-0.06 + side, y, 1.62 - 0.03 * dip - crouch))
        look_ahead = 3.0 + 3.0 * ease((walked - slide_from) / (walk_s - slide_from))
        key_camera(cam, eye, Vector((eye.x + 0.03, y + look_ahead, 0.0)), frame)
    markers = {f'step_{index + 1}': round(step * FPS) + 1 for index, step in enumerate(steps)}
    return Plate(cam, list(range(1, frames + 1)), markers=markers)


def t_focus_pull(parts: dict, params: dict) -> Plate:
    """Locked low at the line; focus pulls from the foul line to the pins 60 ft away."""
    frames = frames_of(params, 6.0)
    hide_ball(parts)
    cam = camera('cam_focus', 40, 1.4)
    eye = Vector((0.08, -0.9, 0.30))
    pitch = math.radians(8)
    key_camera(cam, eye, eye + Vector((0, math.cos(pitch), -math.sin(pitch))) * 10, 1)
    near, far = (Vector((0.08, 0.0, 0.0)) - eye).length, (Vector((0.0, LANE_LEN, 0.2)) - eye).length
    pull_from, pull_to = round(float(params.get('pull_from_s', 1.0)) * FPS), round(float(params.get('pull_to_s', 2.6)) * FPS)
    for frame in range(1, frames + 1):
        cam.data.dof.focus_distance = near + (far - near) * ease((frame - pull_from) / max(1, pull_to - pull_from))
        cam.data.dof.keyframe_insert('focus_distance', frame=frame)
    return Plate(cam, list(range(1, frames + 1)), subjects=dict(rack=list(parts['pins'])), markers=dict(focus_on_pins=pull_to))


def t_arrows_overhead(parts: dict, params: dict) -> Plate:
    """Overhead on the target arrows, holding the board-10 arrow at a fixed spot in the safe window; the ring around
    it and the ball's line are exported per frame so compose can draw them."""
    frames = frames_of(params, 7.0)
    hide_ball(parts)
    target = tuple(params.get('arrow_at', (500, 980)))
    cam = camera('cam_arrows', 30, None)
    for frame in range(1, frames + 1):
        progress = ease((frame - 1) / (frames - 1))
        eye = Vector((0.13, ARROW.y + 0.35 + 0.12 * progress, 2.45 - 0.25 * progress))
        key_camera(cam, eye, Vector((eye.x, eye.y + 0.01, 0.0)), frame)
        hold_on_screen(cam, ARROW, target, frame)
    ring = [ARROW + Vector((0.21 * math.cos(2 * math.pi * index / 64), 0.21 * math.sin(2 * math.pi * index / 64), 0.001)) for index in range(64)]
    line = [iq.point_at(feet) - Vector((0, 0, BALL_R - 0.001)) for feet in [10 + 0.5 * index for index in range(101)]]
    return Plate(cam, list(range(1, frames + 1)), subjects=dict(arrow=ARROW), required=['arrow'], points=dict(ring=ring, line=line))


def t_approach_pool(parts: dict, params: dict) -> Plate:
    """The approach under one pool of light, the house dimmed; a slow low push toward the line."""
    frames = frames_of(params, 6.0)
    hide_ball(parts)
    for light in bpy.data.objects:
        if light.type == 'LIGHT' and light.name.split('_')[0] in ('pool', 'house', 'ceiling', 'approach'):
            light.data.energy *= 0.3
    data = bpy.data.lights.new('approach_pool', 'SPOT')
    data.energy, data.spot_size, data.spot_blend, data.color = 1400, math.radians(40), 0.45, (1.0, 0.9, 0.78)
    data.shadow_soft_size = 0.15
    pool = bpy.data.objects.new('approach_pool', data)
    pool.location = (-0.05, -1.0, 3.2)
    bpy.context.scene.collection.objects.link(pool)
    cam = camera('cam_approach', 32, 2.8)
    for frame in range(1, frames + 1):
        progress = ease((frame - 1) / (frames - 1))
        key_camera(cam, Vector((0.30 - 0.08 * progress, -3.9 + 0.8 * progress, 0.24 - 0.03 * progress)), Vector((-0.05, 1.0 + 0.5 * progress, 0.0)), frame)
    cam.data.dof.focus_distance = 2.6
    return Plate(cam, list(range(1, frames + 1)))


def t_monitor_push(parts: dict, params: dict) -> Plate:
    """A level push in on the lane monitor; lens shift holds the glass so it stays a true rectangle. The glass corners
    of both monitors are exported per frame so compose can lay any screen into them."""
    frames = frames_of(params, 7.0)
    hide_ball(parts)
    target = tuple(params.get('glass_at', (450, 727)))
    cam = camera('cam_monitor', 32, None)
    centre = Vector((MONITOR_X, MONITOR['y'], (MONITOR['top'] + MONITOR['bottom']) / 2))
    for frame in range(1, frames + 1):
        progress = ease((frame - 1) / (frames - 1))
        eye = Vector((MONITOR_X, -8.0 + 1.15 * progress, 1.65))
        key_camera(cam, eye, eye + Vector((0, 1, 0)), frame)
        hold_on_screen(cam, centre, target, frame)
    return Plate(cam, list(range(1, frames + 1)), subjects=dict(monitor_glass=centre), required=['monitor_glass'],
                 points=dict(monitor_glass=screen_corners(MONITOR_X), neighbour_glass=screen_corners(RETURN_X + MONITOR['half'] + 0.02)))


# ---------------------------------------------------------------- ball motion

def t_laydown(parts: dict, params: dict) -> Plate:
    """Low and side on in slow motion: the ball drops into frame and lays down on the oil, skidding."""
    frames, ball = frames_of(params, 4.0), parts['ball']
    slow, speed = float(params.get('slow', 0.25)), float(params.get('speed', 7.4))
    land = round(float(params.get('land_s', 1.2)) * FPS) + 1
    drop, start_velocity = 0.10, 0.3
    land_velocity = math.sqrt(start_velocity ** 2 + 2 * 9.81 * drop)
    y_land = 1.0 * FT
    positions, axes = list(), list()
    for frame in range(1, frames + 1):
        until = (land - frame) * slow / FPS  # physical seconds until touchdown (negative after it)
        y = y_land - speed * until
        if until > 0:
            height = land_velocity * until - 4.905 * until ** 2
        else:
            age = -until
            height = 0.003 * max(0.0, math.sin(math.pi * age / 0.03)) if age < 0.03 else 0.0
        board = ls.path_board(max(y / FT, 0.0))
        positions.append(Vector((ls.board_x(board), y, BALL_R + max(height, 0.0))))
        axes.append(iq.skid_axis(Vector((0, 1, 0))))
    key_positions(ball, positions, axes, 2 * math.pi * 5 * slow / FPS)
    follow = iq.rig('laydown_rig')
    for frame, position in enumerate(positions, 1):
        follow.location = Vector((position.x, position.y, BALL_R))
        follow.keyframe_insert('location', frame=frame)
    cam = camera('cam_laydown', 45, 2.8)
    iq.attach(cam, follow, Vector((-0.75, -0.04, 0.07 - BALL_R)), Vector((0.0, 0.02, 0.12 - BALL_R)))
    cam.data.dof.focus_distance = 0.75
    iq.attach(ls.add_area('laydown_key', (0.6, 0.6), 24, (0, 0, 0), None), follow, Vector((-0.5, -0.5, 0.7)), Vector((0, 0, 0)))
    iq.attach(ls.add_area('laydown_rim', (0.5, 0.5), 36, (0, 0, 0), None), follow, Vector((0.6, 0.6, 0.4)), Vector((0, 0, 0)))
    return Plate(cam, list(range(1, frames + 1)), subjects=dict(ball=[ball]), required=['ball'], markers=dict(landing=land))


def t_hook_behind(parts: dict, params: dict) -> Plate:
    """Behind the ball as it turns and hooks left past the breakpoint, boards streaming by."""
    frames, ball = frames_of(params, 4.0), parts['ball']
    slow, speed = float(params.get('slow', 0.25)), float(params.get('speed', 7.4))
    start_feet = float(params.get('start_feet', 30.0))
    feet_list = iq.travel(start_feet, frames, speed, slow)
    steps, axes = iq.spin_plan(feet_list, speed, slow)
    iq.key_ball(ball, feet_list, steps, axes)
    follow = iq.rig('hook_rig')
    cam = camera('cam_hook', 35, 2.8)
    iq.attach(cam, follow, Vector((0.0, -0.9, 0.21)), Vector((-iq.point_at(start_feet).x, 9.1, -0.45 - BALL_R)))
    cam.data.dof.focus_distance = 0.9 - BALL_R * 0.6
    iq.attach(ls.add_area('hook_key', (0.7, 0.7), 22, (0, 0, 0), None), follow, Vector((-0.55, -0.7, 1.0)), Vector((0, 0, 0)))
    iq.attach(ls.add_area('hook_rim', (0.5, 0.5), 34, (0, 0, 0), None), follow, Vector((0.65, 1.1, 0.6)), Vector((0, 0, 0)))
    iq.key_rig(follow, feet_list)
    breakpoint_frame = next((index + 1 for index, feet in enumerate(feet_list) if feet >= 41), None)
    markers = dict(breakpoint=breakpoint_frame) if breakpoint_frame is not None else dict()
    return Plate(cam, list(range(1, frames + 1)), subjects=dict(ball=[ball]), required=['ball'], markers=markers)


# ---------------------------------------------------------------- the strike: one verified simulation, many cameras

def simulate_strike(parts: dict, approach_frames: int, total: int, params: dict) -> dict:
    """The verified strike roll (render-node-plan.md; Ten Frames search): pins weighted at their real centre of
    gravity, Bullet contact values set to measured pairs, the ball let go 30 simulation frames before the pins. The
    simulation is baked to keyframes so a resumed render never runs it again."""
    scene, ball, pins = parts['scene'], parts['ball'], parts['pins']
    bpy.ops.rigidbody.world_add()
    world = scene.rigidbody_world
    world.time_scale, world.substeps_per_frame, world.solver_iterations = STRIKE_SLOW, 12, 40
    world.point_cache.frame_start, world.point_cache.frame_end = 1, total
    cap_x = ls.LANE_W / 2 + 2 * ls.GUTTER_R + ls.CAP_W / 2
    iq.collider('deck_collider', (PITCH + 0.2, 4.0, 0.1), (0, LANE_LEN - 0.5, -0.05), friction=0.45, bounce=0.2)
    iq.collider('pit_collider', (PITCH + 0.6, 1.2, 0.1), (0, LANE_LEN + 1.6, -0.3), friction=0.6, bounce=0.1)
    iq.collider('back_collider', (PITCH + 0.6, 0.1, 1.4), (0, LANE_LEN + 1.65, 0.5), friction=0.4, bounce=0.2)
    for side in (-1, 1):
        iq.collider(f'kick_collider_{side}', (ls.CAP_W, 2.2, 0.8), (side * cap_x, LANE_LEN + 0.75, 0.4), friction=0.4, bounce=0.35)
        iq.collider(f'gutter_collider_{side}', (2 * ls.GUTTER_R, 4.0, 0.05), (side * (ls.LANE_W / 2 + ls.GUTTER_R), LANE_LEN - 0.5, -ls.GUTTER_R - 0.025),
                    friction=0.5, bounce=0.1)
    weighted = pins[0].data.copy()
    weighted.transform(Matrix.Translation((0.0, 0.0, -PIN_CENTRE_OF_GRAVITY)))
    for pin in pins:
        pin.data = weighted
        pin.location.z += PIN_CENTRE_OF_GRAVITY
    bpy.context.view_layer.update()
    for pin in pins:
        iq.add_rigid(pin, 'ACTIVE', 'CONVEX_HULL', mass=1.53, friction=0.45, bounce=0.85)
        pin.rigid_body.use_deactivation = True
        pin.rigid_body.use_start_deactivated = True
    iq.add_rigid(ball, 'ACTIVE', 'SPHERE', mass=7.0, friction=0.3, bounce=0.75)
    speed = iq.STRIKE_SPEED
    free_frames = 30
    start_feet = 59.5 - (approach_frames + free_frames) * speed * STRIKE_SLOW / FPS / FT
    feet_list = iq.travel(start_feet, approach_frames, speed, STRIKE_SLOW)
    iq.key_ball(ball, feet_list, [speed / BALL_R * STRIKE_SLOW / FPS] * approach_frames, [iq.roll_axis(iq.direction_at(feet)) for feet in feet_list])
    ball.rigid_body.kinematic = True
    ball.rigid_body.keyframe_insert('kinematic', frame=1)
    ball.rigid_body.keyframe_insert('kinematic', frame=approach_frames)
    ball.rigid_body.kinematic = False
    ball.rigid_body.keyframe_insert('kinematic', frame=approach_frames + 1)
    rest = {pin.name: pin.matrix_world.translation.copy() for pin in pins}
    contact = None
    for frame in range(1, total + 1):
        scene.frame_set(frame)
        if contact is None and any((pin.matrix_world.translation - rest[pin.name]).length > 0.002 for pin in pins):
            contact = frame
    standing = [pin.name for pin in pins if (pin.matrix_world.to_3x3() @ Vector((0, 0, 1))).z > 0.8 and pin.matrix_world.translation.z > -0.02
                and (pin.matrix_world.translation - rest[pin.name]).length < 0.3]
    settled = None
    if contact is not None:
        previous = {pin.name: pin.matrix_world.translation.copy() for pin in pins}
        for frame in range(contact + 30, total + 1, 30):
            scene.frame_set(frame)
            moved = max((pin.matrix_world.translation - previous[pin.name]).length for pin in pins)
            previous = {pin.name: pin.matrix_world.translation.copy() for pin in pins}
            if moved < 0.01:  # under 1 cm in 0.1 s of physical time: only slow rolling in the pit is left
                settled = frame
                break
    bake_to_keyframes([ball] + list(pins), total)
    return dict(contact=contact, standing=standing, settled=settled, entry_angle=round(iq.entry_angle_degrees(), 2),
                pocket=iq.POCKET, speed=speed)


def bake_to_keyframes(objects: list, total: int) -> None:
    """Record every simulated transform, switch the physics off and key the transforms instead. (Blender's own
    bake-to-keyframes operator needs a window and fails in background mode.) Quaternions are kept on one side, so
    motion blur between keys never turns the long way round."""
    scene = bpy.context.scene
    record = {obj.name: list() for obj in objects}
    for frame in range(1, total + 1):
        scene.frame_set(frame)
        for obj in objects:
            record[obj.name].append(obj.matrix_world.copy())
    scene.rigidbody_world.enabled = False
    for obj in objects:
        obj.animation_data_clear()
        obj.rotation_mode = 'QUATERNION'
        previous = None
        for frame, matrix in enumerate(record[obj.name], 1):
            location, rotation, _scale = matrix.decompose()
            if previous is not None and rotation.dot(previous) < 0:
                rotation.negate()
            previous = rotation
            obj.location, obj.rotation_quaternion = location, rotation
            obj.keyframe_insert('location', frame=frame)
            obj.keyframe_insert('rotation_quaternion', frame=frame)


STRIKE_CAMERAS = dict(
    # name: (lens, f-stop, eye, target, focus point), relative to the head pin spot (0, LANE_LEN, 0); focus point None
    # means focus on the rack
    pin_cam=(40, 2.8, (0.07, 1.50, 0.80), (0.06, -1.6, 0.0), None),
    low_front=(40, 2.8, (-0.55, -1.6, 0.12), (0.03, 0.15, 0.17), None),
    side_high=(20, 4.0, (-0.62, -0.35, 1.05), (0.15, 0.45, 0.20), None),
    elevated_three_quarter=(28, 4.0, (0.65, -0.85, 0.70), (0.0, 0.45, 0.22), None),
    overhead=(24, None, (0.0, -0.25, 2.6), (0.0, -0.24, 0.0), None),
    # broadcast style, high behind the line: the ball at the foul line and the pins 60 ft away both in frame
    behind_bowler=(30, None, (0.25, -LANE_LEN - 2.2, 1.9), (0.05, -LANE_LEN + 7.07, -1.85), None),
    # low beside the ball's path on the right (never in it), about 0.55 m from the pins: the ball comes in from the
    # left into the 1-3 pocket with the head pin and the 3 pin in frame; focus on the contact point
    macro=(28, 2.8, (0.42, -0.62, 0.11), (0.06, 0.02, 0.13), (0.05, -0.12, 0.11)),
)


def t_strike(parts: dict, params: dict) -> Plate:
    """One verified strike filmed from one camera at one speed. Params: camera (a STRIKE_CAMERAS key), speed (a
    SPEED_STEPS key), duration_s, contact_at_s (where in the plate the ball meets the pins)."""
    view, speed_name = params.get('camera', 'low_front'), params.get('speed', 'slow_5x')
    step, frames = SPEED_STEPS[speed_name], frames_of(params, 6.0)
    contact_index = round(float(params.get('contact_at_s', 1.5)) * FPS)
    before = step * contact_index + 40
    approach = max(220, min(700, before - 30))
    # the result is judged 3.2 s (960 simulation frames) after contact, as in the verified search, however short the plate
    after = max(step * (frames - contact_index) + 60, 960)
    total = approach + 4 + after
    result = simulate_strike(parts, approach, total, params)
    contact = result['contact']
    if contact is None:
        raise RuntimeError('the strike simulation never reached the pins')
    if result['standing']:
        raise RuntimeError(f'the strike left pins standing: {result["standing"]}')
    scene_frames = [max(1, min(total, contact + step * (index - contact_index))) for index in range(frames)]
    lens, fstop, eye_offset, target_offset, focus_offset = STRIKE_CAMERAS[view]
    head_spot = Vector((0.0, LANE_LEN, 0.0))
    cam = camera(f'cam_{view}', lens, fstop)
    eye, target = head_spot + Vector(eye_offset), head_spot + Vector(target_offset)
    if view == 'pin_cam':
        # a slow push over the shot; the ball comes straight at us into the 1-3 pocket and is the focus
        key_camera(cam, eye, target, scene_frames[0])
        key_camera(cam, eye + Vector((0.0, -0.14, -0.02)), target, scene_frames[-1])
        cam.data.dof.focus_object = parts['ball']
    else:
        key_camera(cam, eye, target, 1)
        if fstop is not None:
            focus_point = head_spot + Vector(focus_offset if focus_offset is not None else (0.0, 0.3, 0.2))
            cam.data.dof.focus_distance = (focus_point - eye).length
    rim_light('strike_rim')
    markers = dict(contact=contact_index + 1)
    if result['settled'] is not None:
        settled_index = round((result['settled'] - contact) / step) + contact_index + 1
        if settled_index <= frames:
            markers['pins_settled'] = settled_index
    return Plate(cam, scene_frames, shutter=SPEED_SHUTTER[speed_name], subjects=dict(ball=[parts['ball']], rack=list(parts['pins'])),
                 required=['rack'], markers=markers, notes=dict(simulation=result))


TEMPLATES = dict(
    rack_lowering=t_rack_lowering, lane_push=t_lane_push, foul_line=t_foul_line, return_orbit=t_return_orbit,
    walk_pov=t_walk_pov, focus_pull=t_focus_pull, arrows_overhead=t_arrows_overhead, approach_pool=t_approach_pool,
    monitor_push=t_monitor_push, laydown=t_laydown, hook_behind=t_hook_behind, strike=t_strike,
)


# ---------------------------------------------------------------- frame data

def object_box(cam: bpy.types.Object, objects: list) -> list | None:
    xs, ys = list(), list()
    for obj in objects:
        if obj.hide_render:
            continue
        for corner in obj.bound_box:
            x, y, depth = project(cam, obj.matrix_world @ Vector(corner))
            if depth > 0:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return [round(min(xs), 1), round(min(ys), 1), round(max(xs), 1), round(max(ys), 1)]


def frame_data(plate: Plate) -> list:
    scene, cam, data = bpy.context.scene, plate.camera, list()
    for scene_frame in plate.scene_frames:
        scene.frame_set(scene_frame)
        quat = cam.matrix_world.to_quaternion()
        entry = dict(camera=dict(location=[round(value, 4) for value in cam.matrix_world.translation],
                                 rotation_quat=[round(value, 5) for value in quat],
                                 lens=round(cam.data.lens, 3), shift=[round(cam.data.shift_x, 5), round(cam.data.shift_y, 5)]),
                     boxes=dict())
        for name, subject in plate.subjects.items():
            if isinstance(subject, Vector):
                x, y, _depth = project(cam, subject)
                entry['boxes'][name] = [round(x, 1), round(y, 1), round(x, 1), round(y, 1)]
            else:
                entry['boxes'][name] = object_box(cam, subject)
        for name, world_points in plate.points.items():
            entry.setdefault('points', dict())[name] = [[round(value, 1) for value in project(cam, point)[:2]] for point in world_points]
        data.append(entry)
    return data
