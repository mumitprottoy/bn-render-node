"""Bowling IQ, 'Why does the ball hook?': the animated shots for the 30 s Short.

One shot per call:
  /Applications/Blender.app/Contents/MacOS/Blender -b -P iq_shots.py -- <shot> <samples> [--test] [--sim-only]
      [--pocket=17.5] [--speed=6.6]
Shots: hero, skid, broadcast, strike (one simulation, rendered from overhead then from the front), endcard.
Frames go to renders/iq/<shot>[/<camera>]/####.png and per-frame screen marks to renders/iq/<shot>.json.
"""

import json
import math
import os
import sys
from pathlib import Path

import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Quaternion, Vector

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import lane_scene as ls  # noqa: E402

ARGS = sys.argv[sys.argv.index('--') + 1:]
SHOT = ARGS[0]
SAMPLES = int(ARGS[1]) if len(ARGS) > 1 and ARGS[1].isdigit() else 48
TEST = '--test' in ARGS
SIM_ONLY = '--sim-only' in ARGS
OPTIONS = dict(arg[2:].split('=', 1) for arg in ARGS if arg.startswith('--') and '=' in arg)
POCKET = float(OPTIONS.get('pocket', 17.5))
STRIKE_SPEED = float(OPTIONS.get('speed', 6.6))
OUT = Path(os.environ.get('BN_OUT', str(HERE / 'renders'))) / 'iq'
FPS, FT, IN, BALL_R, LANE_LEN = ls.FPS, ls.FT, ls.IN, ls.BALL_R, ls.LANE_LEN
UP = Vector((0.0, 0.0, 1.0))


def board_at(feet: float) -> float:
    board = ls.path_board(feet)
    if feet > 43:
        board += (POCKET - 17.5) * ((feet - 43) / 17) ** 1.9
    return board


def point_at(feet: float) -> Vector:
    return Vector((ls.board_x(board_at(feet)), feet * FT, BALL_R))


def direction_at(feet: float) -> Vector:
    return (point_at(min(feet + 0.2, 60.5)) - point_at(max(feet - 0.2, 0.0))).normalized()


def entry_angle_degrees() -> float:
    direction = direction_at(59.4)
    return math.degrees(math.atan2(abs(direction.x), direction.y))


def setup() -> dict:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    ls.USE_HAZE = False
    ls.SAMPLES = SAMPLES
    ls.render_settings()
    scene = bpy.context.scene
    scene.render.use_persistent_data = True
    # Measured on the M4 Pro: the GPU alone renders faster than GPU plus CPU together.
    cpu_only = os.environ.get('BN_DEVICE', 'METAL') == 'CPU'
    for device in bpy.context.preferences.addons['cycles'].preferences.devices:
        device.use = (device.type == 'CPU') if cpu_only else (device.type != 'CPU')
    if cpu_only:
        scene.cycles.device = 'CPU'
    scene.cycles.use_adaptive_sampling = True
    scene.cycles.adaptive_threshold = 0.02
    scene.render.image_settings.color_depth = '8'
    root = scene.collection
    lane = ls.build_lane(root, with_pins=False)
    ls.build_house(root, lane)
    mesh = ls.pin_mesh()
    rack = bpy.data.collections.new('pins_static')
    ls.add_rack(rack, mesh, 'rack')
    for index in (-2, -1, 1, 2):
        neighbour = bpy.data.objects.new(f'rack_{index}', None)
        neighbour.instance_type = 'COLLECTION'
        neighbour.instance_collection = rack
        neighbour.location = (index * ls.PITCH, 0, 0)
        root.objects.link(neighbour)
    live = bpy.data.collections.new('pins_live')
    root.children.link(live)
    pins = ls.add_rack(live, mesh, 'pin')
    ls.world_and_haze(root)
    ls.house_lights()
    ball = ls.build_ball()
    # Freeze the drilled ball: live boolean cuts re-evaluate on every frame and motion-blur step (about 5 s a frame).
    evaluated = ball.evaluated_get(bpy.context.evaluated_depsgraph_get())
    frozen = bpy.data.meshes.new_from_object(evaluated)
    ball.modifiers.clear()
    ball.data = frozen
    ball.rotation_mode = 'QUATERNION'
    return dict(scene=scene, pins=pins, ball=ball, root=root)


def key_ball(ball: bpy.types.Object, feet_list: list, step_angles: list, axes: list, first_frame: int = 1) -> None:
    turn = Quaternion((1.0, 0.0, 0.0, 0.0))
    for index, feet in enumerate(feet_list):
        if index:
            turn = Quaternion(axes[index], step_angles[index]) @ turn
        ball.location = point_at(feet)
        ball.rotation_quaternion = turn
        ball.keyframe_insert('location', frame=first_frame + index)
        ball.keyframe_insert('rotation_quaternion', frame=first_frame + index)


def skid_axis(direction: Vector) -> Vector:
    roll = UP.cross(direction).normalized()
    # Right-hander: seen from behind the ball turns anti-clockwise, so the axis tips back against the direction of travel.
    # The contact point then moves right, friction pushes the ball left, and it hooks into the 1-3 pocket.
    return (roll * math.cos(math.radians(50)) - direction * math.sin(math.radians(50))).normalized()


def roll_axis(direction: Vector) -> Vector:
    return UP.cross(direction).normalized()


def travel(start_feet: float, frames: int, speed: float, slow: float) -> list:
    step_feet = speed * slow / FPS / FT
    return [start_feet + step_feet * index for index in range(frames)]


def spin_plan(feet_list: list, speed: float, slow: float) -> tuple:
    """Skid spin (5 rev/s on a tilted axis) blending into end-over-end roll after the breakpoint."""
    skid_step = 2 * math.pi * 5 * slow / FPS
    roll_step = speed / BALL_R * slow / FPS
    steps, axes = list(), list()
    for feet in feet_list:
        blend = min(max((feet - 41) / 12, 0.0), 1.0)
        direction = direction_at(feet)
        axes.append((skid_axis(direction) * (1 - blend) + roll_axis(direction) * blend).normalized())
        steps.append(skid_step * (1 - blend) + roll_step * blend)
    return steps, axes


def camera(name: str, lens: float, fstop: float | None) -> bpy.types.Object:
    cam = ls.add_camera(name, lens, fstop)
    bpy.context.scene.camera = cam
    return cam


def rig(name: str) -> bpy.types.Object:
    empty = bpy.data.objects.new(name, None)
    bpy.context.scene.collection.objects.link(empty)
    return empty


def attach(child: bpy.types.Object, parent: bpy.types.Object, offset: Vector, look_at_offset: Vector) -> None:
    """Place a camera or light at offset from the rig, aimed at look_at_offset; the rig only ever translates."""
    child.parent = parent
    child.location = offset
    child.rotation_euler = (look_at_offset - offset).to_track_quat('-Z', 'Y').to_euler()


def key_rig(rig_object: bpy.types.Object, feet_list: list, first_frame: int = 1) -> None:
    for index, feet in enumerate(feet_list):
        rig_object.location = point_at(feet)
        rig_object.keyframe_insert('location', frame=first_frame + index)


def project(cam: bpy.types.Object, point: Vector) -> list:
    view = world_to_camera_view(bpy.context.scene, cam, Vector(point))
    return [round(view.x * ls.WIDTH, 1), round((1 - view.y) * ls.HEIGHT, 1)]


def render_range(cam: bpy.types.Object, folder: Path, first: int, last: int) -> None:
    scene = bpy.context.scene
    scene.camera = cam
    folder.mkdir(parents=True, exist_ok=True)
    frames = [first, (first + last) // 2, last] if TEST else None
    if frames is None:
        scene.frame_start, scene.frame_end = first, last
        scene.render.filepath = str(folder) + '/'
        bpy.ops.render.render(animation=True)
        return
    for frame in frames:
        scene.frame_set(frame)
        scene.render.filepath = str(folder / f'{frame:04d}.png')
        bpy.ops.render.render(write_still=True)


def shot_hero(parts: dict) -> None:
    frames, slow, speed = 108, 0.25, 7.4
    feet_list = travel(8.0, frames, speed, slow)
    steps, axes = spin_plan(feet_list, speed, slow)
    key_ball(parts['ball'], feet_list, steps, axes)
    follow = rig('hero_rig')
    cam = camera('cam_hero', 35, 2.8)
    attach(cam, follow, Vector((0.0, -0.9, 0.21)), Vector((-point_at(25).x, 9.1, -0.45 - BALL_R)))
    cam.data.dof.focus_distance = 0.9 - BALL_R * 0.6
    attach(ls.add_area('hero_key', (0.7, 0.7), 22, (0, 0, 0), None), follow, Vector((-0.55, -0.7, 1.0)), Vector((0, 0, 0)))
    attach(ls.add_area('hero_rim', (0.5, 0.5), 34, (0, 0, 0), None), follow, Vector((0.65, 1.1, 0.6)), Vector((0, 0, 0)))
    key_rig(follow, feet_list)
    render_range(cam, OUT / 'hero', 1, frames)


def shot_skid(parts: dict) -> None:
    frames, slow, speed = 168, 0.25, 7.4
    feet_list = travel(2.0, frames, speed, slow)
    steps, axes = [2 * math.pi * 5 * slow / FPS] * frames, [skid_axis(direction_at(feet)) for feet in feet_list]
    key_ball(parts['ball'], feet_list, steps, axes)
    follow = rig('skid_rig')
    cam = camera('cam_skid', 35, 2.8)
    attach(cam, follow, Vector((-1.0, 0.05, 0.06 - BALL_R)), Vector((0.0, 0.0, 0.12)))
    cam.data.dof.focus_distance = 1.0 - BALL_R * 0.8
    attach(ls.add_area('skid_key', (0.8, 0.8), 28, (0, 0, 0), None), follow, Vector((-0.6, -0.5, 0.9)), Vector((0, 0, 0)))
    attach(ls.add_area('skid_rim', (0.5, 0.5), 40, (0, 0, 0), None), follow, Vector((0.7, 0.4, 0.35)), Vector((0, 0, 0)))
    key_rig(follow, feet_list)
    marks = list()
    for frame in range(1, frames + 1):
        bpy.context.scene.frame_set(frame)
        marks.append(dict(ball=project(cam, parts['ball'].matrix_world.translation), feet=round(feet_list[frame - 1], 2)))
    (OUT / 'skid.json').write_text(json.dumps(dict(frames=marks)))
    render_range(cam, OUT / 'skid', 1, frames)


def shot_broadcast(parts: dict) -> None:
    frames, slow, speed = 234, 0.095, 7.0
    feet_list = travel(36.9, frames, speed, slow)
    steps, axes = spin_plan(feet_list, speed, slow)
    key_ball(parts['ball'], feet_list, steps, axes)
    cam = camera('cam_broadcast', 60, None)
    target = Vector((0.0, 50 * FT, 0.0))
    for frame in range(1, frames + 1):
        cam.location = Vector((0.0, (22 + 1.5 * (frame - 1) / (frames - 1)) * FT, 1.45))
        cam.rotation_euler = (target - cam.location).to_track_quat('-Z', 'Y').to_euler()
        cam.keyframe_insert('location', frame=frame)
        cam.keyframe_insert('rotation_euler', frame=frame)
    ls.add_area('broadcast_key', (1.2, 3.0), 70, (0.5, 47 * FT, 1.5), (0.25, 47 * FT, 0.0))
    floor = Vector((0, 0, BALL_R))
    path_feet = [36.0 + 0.5 * i for i in range(49)]
    marks = list()
    for frame in range(1, frames + 1):
        bpy.context.scene.frame_set(frame)
        marks.append(dict(feet=round(feet_list[frame - 1], 2),
                          ball=project(cam, parts['ball'].matrix_world.translation),
                          ball_floor=project(cam, parts['ball'].matrix_world.translation - floor),
                          path=[project(cam, point_at(feet) - floor) for feet in path_feet],
                          oil=[project(cam, Vector((-ls.LANE_W / 2, 40 * FT, 0))), project(cam, Vector((ls.LANE_W / 2, 40 * FT, 0)))],
                          near=[project(cam, Vector((-ls.LANE_W / 2, 30 * FT, 0))), project(cam, Vector((ls.LANE_W / 2, 30 * FT, 0)))],
                          breakpoint=project(cam, point_at(43) - floor)))
    (OUT / 'broadcast.json').write_text(json.dumps(dict(path_feet=path_feet, frames=marks)))
    render_range(cam, OUT / 'broadcast', 1, frames)


def add_rigid(obj: bpy.types.Object, kind: str, shape: str, mass: float = 1.0, friction: float = 0.5,
              bounce: float = 0.2) -> None:
    view_layer = bpy.context.view_layer
    for selected in list(bpy.context.selected_objects):
        selected.select_set(False)
    view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.rigidbody.object_add(type=kind)
    body = obj.rigid_body
    body.collision_shape = shape
    body.mass = mass
    body.friction = friction
    body.restitution = bounce
    body.use_margin = True
    body.collision_margin = 0.001
    obj.select_set(False)


def collider(name: str, size: tuple, location: tuple, friction: float, bounce: float) -> bpy.types.Object:
    mat = bpy.data.materials.get('house_black')
    obj = ls.add_box(name, size, location, mat, bpy.context.scene.collection)
    obj.hide_render = True
    add_rigid(obj, 'PASSIVE', 'BOX', friction=friction, bounce=bounce)
    return obj


def simulate(parts: dict, frames: int) -> dict:
    scene = parts['scene']
    ball, pins = parts['ball'], parts['pins']
    bpy.ops.rigidbody.world_add()
    world = scene.rigidbody_world
    world.time_scale = 0.1
    world.substeps_per_frame = 12
    world.solver_iterations = 40
    world.point_cache.frame_start, world.point_cache.frame_end = 1, frames
    cap_x = ls.LANE_W / 2 + 2 * ls.GUTTER_R + ls.CAP_W / 2
    collider('deck_collider', (ls.PITCH + 0.2, 4.0, 0.1), (0, LANE_LEN - 0.5, -0.05), friction=0.45, bounce=0.2)
    collider('pit_collider', (ls.PITCH + 0.6, 1.2, 0.1), (0, LANE_LEN + 1.6, -0.3), friction=0.6, bounce=0.1)
    collider('back_collider', (ls.PITCH + 0.6, 0.1, 1.4), (0, LANE_LEN + 1.65, 0.5), friction=0.4, bounce=0.2)
    for side in (-1, 1):
        collider(f'kick_collider_{side}', (ls.CAP_W, 2.2, 0.8), (side * cap_x, LANE_LEN + 0.75, 0.4), friction=0.4, bounce=0.35)
        collider(f'gutter_collider_{side}', (2 * ls.GUTTER_R, 4.0, 0.05), (side * (ls.LANE_W / 2 + ls.GUTTER_R), LANE_LEN - 0.5, -ls.GUTTER_R - 0.025),
                 friction=0.5, bounce=0.1)
    for pin in pins:
        add_rigid(pin, 'ACTIVE', 'CONVEX_HULL', mass=1.53, friction=0.45, bounce=0.55)
        pin.rigid_body.use_deactivation = True
        pin.rigid_body.use_start_deactivated = True
    add_rigid(ball, 'ACTIVE', 'SPHERE', mass=7.0, friction=0.3, bounce=0.3)
    slow = world.time_scale
    contact_feet = 59.5
    start_feet = contact_feet - 120 * STRIKE_SPEED * slow / FPS / FT
    feet_list = travel(start_feet, 116, STRIKE_SPEED, slow)
    release = len(feet_list)
    steps = [STRIKE_SPEED / BALL_R * slow / FPS] * release
    axes = [roll_axis(direction_at(feet)) for feet in feet_list]
    key_ball(ball, feet_list, steps, axes)
    ball.rigid_body.kinematic = True
    ball.rigid_body.keyframe_insert('kinematic', frame=1)
    ball.rigid_body.keyframe_insert('kinematic', frame=release)
    ball.rigid_body.kinematic = False
    ball.rigid_body.keyframe_insert('kinematic', frame=release + 1)
    rest = {pin.name: pin.matrix_world.translation.copy() for pin in pins}
    track, contact = list(), None
    for frame in range(1, frames + 1):
        scene.frame_set(frame)
        position = ball.matrix_world.translation.copy()
        track.append(position)
        if contact is None and any((pin.matrix_world.translation - rest[pin.name]).length > 0.002 for pin in pins):
            contact = frame
    standing = [pin.name for pin in pins if (pin.matrix_world.to_3x3() @ UP).z > 0.8 and pin.matrix_world.translation.z > -0.02
                and (pin.matrix_world.translation - rest[pin.name]).length < 0.3]
    after = (track[release + 3] - track[release + 1]).length / 2 / (slow / FPS)
    print(f'SIM pocket={POCKET} speed={STRIKE_SPEED} entry_angle={entry_angle_degrees():.1f} contact_frame={contact} '
          f'ball_speed_after_release={after:.2f} m/s standing={standing}')
    return dict(track=track, contact=contact, feet=feet_list, standing=standing)


def shot_strike(parts: dict) -> None:
    frames = 260
    result = simulate(parts, frames)
    if SIM_ONLY:
        return
    scene, ball = parts['scene'], parts['ball']
    contact = result['contact'] or 120
    track = result['track']
    masking = bpy.data.objects['masking_unit']
    overhead = camera('cam_overhead', 32, None)
    hold_y = LANE_LEN + 0.25
    for frame in range(1, frames + 1):
        overhead.location = Vector((0.05, min(track[frame - 1].y + 0.55, hold_y), 3.0))
        overhead.keyframe_insert('location', frame=frame)
    fill = ls.add_area('overhead_fill', (1.6, 2.5), 110, (0, LANE_LEN - 1.0, 2.6), None, rotation_x=0)
    floor = Vector((0, 0, BALL_R))
    path_feet = [50.0 + 0.25 * i for i in range(39)]
    marks = list()
    for frame in range(1, frames + 1):
        scene.frame_set(frame)
        marks.append(dict(ball=project(overhead, ball.matrix_world.translation),
                          path=[project(overhead, point_at(feet) - floor) for feet in path_feet],
                          board_line=[project(overhead, Vector((ls.board_x(board_at(59.5)), 56 * FT, 0))),
                                      project(overhead, Vector((ls.board_x(board_at(59.5)), 61 * FT, 0)))]))
    (OUT / 'strike.json').write_text(json.dumps(dict(contact=contact, entry_angle=round(entry_angle_degrees(), 1),
                                                     path_feet=path_feet, overhead=marks, standing=result['standing'])))
    if OPTIONS.get('cameras') != 'front':
        masking.hide_render = True
        render_range(overhead, OUT / 'strike' / 'overhead', 1, contact)
        masking.hide_render = False
    fill.hide_render = True
    front = camera('cam_front', 40, 2.8)
    front.location = (-0.55, LANE_LEN - 1.6, 0.12)
    ls.aim(front, Vector((0.03, LANE_LEN + 0.15, 0.17)))
    front.data.dof.focus_distance = (Vector((0, LANE_LEN, 0.2)) - front.location).length
    ls.add_area('front_rim', (0.6, 0.6), 45, (0.9, LANE_LEN + 0.4, 0.6), (0, LANE_LEN, 0.25))
    render_range(front, OUT / 'strike' / 'front', contact - 8, min(contact + 127, frames))


def shot_endcard(parts: dict) -> None:
    scene = parts['scene']
    parts['ball'].hide_render = True
    scene.view_settings.exposure = -0.35
    scene.render.resolution_percentage = 130
    cam = camera('cam_pins', 65, 2.2)
    cam.location = (0.22, LANE_LEN - 2.3, 0.07)
    ls.aim(cam, Vector((0.0, LANE_LEN + 0.3, 0.32)))
    cam.data.dof.focus_distance = (Vector((0, LANE_LEN, 0.2)) - cam.location).length
    ls.add_area('pins_rim', (0.6, 0.6), 45, (0.9, LANE_LEN + 0.4, 0.6), (0, LANE_LEN, 0.25))
    ls.add_area('pins_key', (0.9, 0.9), 18, (-0.9, LANE_LEN - 1.2, 0.7), (0, LANE_LEN, 0.2), color=(1.0, 0.93, 0.85))
    (OUT / 'endcard').mkdir(parents=True, exist_ok=True)
    scene.frame_set(1)
    scene.render.filepath = str(OUT / 'endcard' / 'still.png')
    bpy.ops.render.render(write_still=True)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    parts = setup()
    dict(hero=shot_hero, skid=shot_skid, broadcast=shot_broadcast, strike=shot_strike, endcard=shot_endcard)[SHOT](parts)
    print('done', SHOT)


if __name__ == '__main__':
    main()
