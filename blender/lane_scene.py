"""BowlersNetwork lane scene: real dimensions, real materials, lit like a centre at night.

Run headless:
  /Applications/Blender.app/Contents/MacOS/Blender -b -P lane_scene.py -- <samples> [hero breakpoint pins] [--no-haze]
Renders each shot to renders/<shot>.png and writes renders/<shot>.json with screen positions
(1080 x 1920 pixels) of the ball path, oil line and pins, so annotations can be drawn in 2D later.
"""

import json
import math
import os
import sys
from pathlib import Path

import bmesh
import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Vector

HERE = Path(__file__).resolve().parent
# The render node sets BN_ASSETS (textures, hdri) and BN_OUT; run by hand, both default to this folder.
ASSETS = Path(os.environ.get('BN_ASSETS', str(HERE)))
TEX = ASSETS / 'textures'
OUT = Path(os.environ.get('BN_OUT', str(HERE / 'renders')))
OUT.mkdir(parents=True, exist_ok=True)

ARGS = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else list()
SAMPLES = int(ARGS[0]) if ARGS and ARGS[0].isdigit() else 64
SHOTS = [arg for arg in ARGS[1:] if not arg.startswith('--')] or ['hero', 'breakpoint', 'pins']
USE_HAZE = '--no-haze' not in ARGS
MARKS_ONLY = '--marks-only' in ARGS

IN = 0.0254
FT = 12 * IN
LANE_W = 41.5 * IN
BOARD = LANE_W / 39
LANE_LEN = 60 * FT
GUTTER_R = 4.7 * IN
CAP_W = 6 * IN
PITCH = LANE_W + 4 * GUTTER_R + CAP_W
BALL_R = 4.25 * IN
FPS = 30
GREEN = (0x8b / 255, 0xc3 / 255, 0x42 / 255)
WIDTH, HEIGHT = 1080, 1920


def linear(color: tuple) -> tuple:
    return tuple((c / 12.92) if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in color) + (1.0,)


def board_x(board: float) -> float:
    return (20 - board) * BOARD


def path_board(feet: float) -> float:
    """Right-hander's line: out from board 12 to board 7 at the 43 ft breakpoint, back into the 1-3 pocket."""
    if feet < 43:
        return 12 - 5 * (feet / 43) ** 1.6
    # Exponent 1.9 gives an entry angle of about 6 degrees at the pocket.
    return 7 + 10.5 * ((feet - 43) / 17) ** 1.9


def path_point(feet: float) -> Vector:
    return Vector((board_x(path_board(feet)), feet * FT, BALL_R))


def path_velocity(feet: float, speed: float) -> Vector:
    ahead, behind = path_point(min(feet + 0.25, 60)), path_point(max(feet - 0.25, 0))
    return (ahead - behind).normalized() * speed


def new_material(name: str) -> tuple:
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    return mat, nodes, links, nodes.get('Principled BSDF')


def plain_material(name: str, color: tuple, roughness: float, coat: float = 0.0, metallic: float = 0.0,
                   specular: float = 0.5) -> bpy.types.Material:
    mat, _, _, bsdf = new_material(name)
    bsdf.inputs['Specular IOR Level'].default_value = specular
    bsdf.inputs['Base Color'].default_value = linear(color)
    bsdf.inputs['Roughness'].default_value = roughness
    bsdf.inputs['Metallic'].default_value = metallic
    bsdf.inputs['Coat Weight'].default_value = coat
    bsdf.inputs['Coat Roughness'].default_value = 0.04
    return mat


def image(nodes: bpy.types.Nodes, path: Path, is_data: bool) -> bpy.types.Node:
    node = nodes.new('ShaderNodeTexImage')
    node.image = bpy.data.images.load(str(path), check_existing=True)
    if is_data:
        node.image.colorspace_settings.name = 'Non-Color'
    return node


def maple_material(name: str, texture_id: str) -> bpy.types.Material:
    mat, nodes, links, bsdf = new_material(name)
    coord = nodes.new('ShaderNodeTexCoord')
    info = nodes.new('ShaderNodeObjectInfo')
    spread = nodes.new('ShaderNodeMath')
    spread.operation = 'MULTIPLY'
    spread.inputs[1].default_value = 37.0
    links.new(info.outputs['Random'], spread.inputs[0])
    offset = nodes.new('ShaderNodeCombineXYZ')
    links.new(spread.outputs[0], offset.inputs['X'])
    links.new(spread.outputs[0], offset.inputs['Y'])
    shifted = nodes.new('ShaderNodeVectorMath')
    shifted.operation = 'ADD'
    links.new(coord.outputs['Object'], shifted.inputs[0])
    links.new(offset.outputs[0], shifted.inputs[1])
    mapping = nodes.new('ShaderNodeMapping')
    mapping.inputs['Scale'].default_value = (1.6, 0.5, 1.0)
    mapping.inputs['Rotation'].default_value = (0, 0, math.radians(90))
    links.new(shifted.outputs[0], mapping.inputs['Vector'])
    color = image(nodes, TEX / texture_id / f'{texture_id}_2K-JPG_Color.jpg', False)
    rough = image(nodes, TEX / texture_id / f'{texture_id}_2K-JPG_Roughness.jpg', True)
    normal_map = image(nodes, TEX / texture_id / f'{texture_id}_2K-JPG_NormalGL.jpg', True)
    for node in (color, rough, normal_map):
        links.new(mapping.outputs['Vector'], node.inputs['Vector'])
    # Real boards never match: small per-board shifts in tone, then calm the grain contrast.
    tone = nodes.new('ShaderNodeHueSaturation')
    tone.inputs['Saturation'].default_value = 1.0
    tone_range = nodes.new('ShaderNodeMapRange')
    tone_range.inputs['To Min'].default_value = 0.9
    tone_range.inputs['To Max'].default_value = 1.06
    links.new(info.outputs['Random'], tone_range.inputs['Value'])
    links.new(tone_range.outputs['Result'], tone.inputs['Value'])
    links.new(color.outputs['Color'], tone.inputs['Color'])
    calm = nodes.new('ShaderNodeBrightContrast')
    calm.inputs['Bright'].default_value = -0.02
    calm.inputs['Contrast'].default_value = -0.1
    links.new(tone.outputs['Color'], calm.inputs['Color'])
    links.new(calm.outputs['Color'], bsdf.inputs['Base Color'])
    rough_scale = nodes.new('ShaderNodeMath')
    rough_scale.operation = 'MULTIPLY'
    rough_scale.inputs[1].default_value = 0.5
    links.new(rough.outputs['Color'], rough_scale.inputs[0])
    links.new(rough_scale.outputs[0], bsdf.inputs['Roughness'])
    normal = nodes.new('ShaderNodeNormalMap')
    normal.inputs['Strength'].default_value = 0.15
    links.new(normal_map.outputs['Color'], normal.inputs['Color'])
    links.new(normal.outputs['Normal'], bsdf.inputs['Normal'])
    # Lane finish: a mirror-like coat on the oiled front 40 ft, a touch softer on the dry back end.
    position = nodes.new('ShaderNodeNewGeometry')
    split = nodes.new('ShaderNodeSeparateXYZ')
    links.new(position.outputs['Position'], split.inputs['Vector'])
    oil = nodes.new('ShaderNodeMapRange')
    oil.inputs['From Min'].default_value = 40 * FT - 1.0
    oil.inputs['From Max'].default_value = 40 * FT + 0.8
    oil.inputs['To Min'].default_value = 0.022
    oil.inputs['To Max'].default_value = 0.07
    links.new(split.outputs['Y'], oil.inputs['Value'])
    links.new(oil.outputs['Result'], bsdf.inputs['Coat Roughness'])
    bsdf.inputs['Coat Weight'].default_value = 1.0
    return mat


def pin_material() -> bpy.types.Material:
    mat, nodes, links, bsdf = new_material('pin_lacquer')
    bsdf.inputs['Coat Weight'].default_value = 1.0
    coord = nodes.new('ShaderNodeTexCoord')
    # Wear: pins are hit thousands of times, so the lacquer is never perfectly even.
    wear = nodes.new('ShaderNodeTexNoise')
    wear.inputs['Scale'].default_value = 180.0
    wear.inputs['Detail'].default_value = 6.0
    links.new(coord.outputs['Object'], wear.inputs['Vector'])
    wear_rough = nodes.new('ShaderNodeMapRange')
    wear_rough.inputs['To Min'].default_value = 0.012
    wear_rough.inputs['To Max'].default_value = 0.09
    links.new(wear.outputs['Fac'], wear_rough.inputs['Value'])
    links.new(wear_rough.outputs['Result'], bsdf.inputs['Coat Roughness'])
    bsdf.inputs['Roughness'].default_value = 0.4
    scuffs = nodes.new('ShaderNodeBump')
    scuffs.inputs['Strength'].default_value = 0.03
    links.new(wear.outputs['Fac'], scuffs.inputs['Height'])
    links.new(scuffs.outputs['Normal'], bsdf.inputs['Normal'])
    split = nodes.new('ShaderNodeSeparateXYZ')
    links.new(coord.outputs['Object'], split.inputs['Vector'])
    height = nodes.new('ShaderNodeMath')
    height.operation = 'DIVIDE'
    height.inputs[1].default_value = 15 * IN
    links.new(split.outputs['Z'], height.inputs[0])
    ramp = nodes.new('ShaderNodeValToRGB')
    ramp.color_ramp.interpolation = 'CONSTANT'
    white, stripe = linear((0.9, 0.885, 0.85)), linear(GREEN)
    elements = ramp.color_ramp.elements
    elements[0].position, elements[0].color = 0.0, white
    elements[1].position, elements[1].color = 1.0, white
    # The neck stripes are BowlersNetwork green instead of the usual red: our pins, our house.
    for position, color in ((8.55 / 15, stripe), (8.95 / 15, white), (9.6 / 15, stripe), (10.0 / 15, white)):
        elements.new(position).color = color
    links.new(height.outputs[0], ramp.inputs['Fac'])
    links.new(ramp.outputs['Color'], bsdf.inputs['Base Color'])
    return mat


def ball_material() -> bpy.types.Material:
    mat, nodes, links, bsdf = new_material('ball_pearl')
    coord = nodes.new('ShaderNodeTexCoord')
    swirl = nodes.new('ShaderNodeTexNoise')
    swirl.inputs['Scale'].default_value = 5.0
    swirl.inputs['Detail'].default_value = 8.0
    swirl.inputs['Distortion'].default_value = 1.8
    links.new(coord.outputs['Object'], swirl.inputs['Vector'])
    ramp = nodes.new('ShaderNodeValToRGB')
    elements = ramp.color_ramp.elements
    elements[0].position, elements[0].color = 0.36, linear((0.02, 0.04, 0.015))
    elements[1].position, elements[1].color = 0.8, linear(GREEN)
    for position, color in ((0.5, (0.1, 0.2, 0.05)), (0.6, GREEN), (0.7, (0.75, 0.86, 0.55))):
        elements.new(position).color = linear(color)
    links.new(swirl.outputs['Fac'], ramp.inputs['Fac'])
    links.new(ramp.outputs['Color'], bsdf.inputs['Base Color'])
    # Pearl: fine mica flakes under a glass-smooth coat.
    flakes = nodes.new('ShaderNodeTexVoronoi')
    flakes.inputs['Scale'].default_value = 900.0
    links.new(coord.outputs['Object'], flakes.inputs['Vector'])
    bump = nodes.new('ShaderNodeBump')
    bump.inputs['Strength'].default_value = 0.05
    links.new(flakes.outputs['Distance'], bump.inputs['Height'])
    links.new(bump.outputs['Normal'], bsdf.inputs['Normal'])
    bsdf.inputs['Roughness'].default_value = 0.32
    bsdf.inputs['Metallic'].default_value = 0.1
    bsdf.inputs['Coat Weight'].default_value = 0.3
    bsdf.inputs['Coat Roughness'].default_value = 0.12
    return mat


def add_box(name: str, size: tuple, location: tuple, mat: bpy.types.Material, collection: bpy.types.Collection) -> bpy.types.Object:
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    bmesh.ops.scale(bm, vec=size, verts=bm.verts)
    bm.to_mesh(mesh)
    bm.free()
    mesh.materials.append(mat)
    obj = bpy.data.objects.new(name, mesh)
    obj.location = location
    collection.objects.link(obj)
    return obj


def add_gutter(name: str, centre_x: float, length: float, mat: bpy.types.Material, collection: bpy.types.Collection) -> None:
    steps = 32
    verts, faces = list(), list()
    for y in (-0.05, length):
        for i in range(steps + 1):
            angle = math.pi + math.pi * i / steps
            verts.append((centre_x + GUTTER_R * math.cos(angle), y, GUTTER_R * math.sin(angle)))
    for i in range(steps):
        faces.append((i, i + 1, steps + 1 + i + 1, steps + 1 + i))
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, list(), faces)
    for polygon in mesh.polygons:
        polygon.use_smooth = True
    mesh.materials.append(mat)
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)


def add_rack(collection: bpy.types.Collection, mesh: bpy.types.Mesh, prefix: str = 'pin') -> list:
    row_step = 12 * IN * math.cos(math.radians(30))
    pins = list()
    for row in range(4):
        for k in range(row + 1):
            pin = bpy.data.objects.new(f'{prefix}_{row}_{k}', mesh)
            pin.location = ((k - row / 2) * 12 * IN, LANE_LEN + row * row_step, 0.0)
            collection.objects.link(pin)
            pins.append(pin)
    return pins


def build_lane(scene_collection: bpy.types.Collection, with_pins: bool = True) -> bpy.types.Collection:
    lane = bpy.data.collections.new('lane')
    scene_collection.children.link(lane)
    maple_a, maple_b = maple_material('maple_a', 'Wood095'), maple_material('maple_b', 'Wood050')
    lane_end = LANE_LEN + 3 * FT
    for board in range(1, 40):
        mat = maple_a if board % 3 == 0 else maple_b
        x = board_x(board)
        add_box(f'board_{board:02d}', (BOARD * 0.985, lane_end, 0.02), (x, lane_end / 2, -0.01), mat, lane)
        add_box(f'approach_{board:02d}', (BOARD * 0.985, 15 * FT, 0.02), (x, -7.5 * FT - 0.003, -0.01), mat, lane)
    inlay = plain_material('inlay', (0.05, 0.04, 0.035), 0.4, coat=1.0)
    add_box('foul_line', (LANE_W, 0.01, 0.0004), (0, 0, 0.0002), inlay, lane)
    for board, feet in ((5, 13), (10, 14), (15, 15), (20, 16), (25, 15), (30, 14), (35, 13)):
        x, tip = board_x(board), feet * FT
        mesh = bpy.data.meshes.new(f'arrow_{board}')
        mesh.from_pydata([(x, tip, 0.0004), (x - 0.5 * IN, tip - 6 * IN, 0.0004), (x + 0.5 * IN, tip - 6 * IN, 0.0004)], list(), [(0, 1, 2)])
        mesh.materials.append(inlay)
        arrow = bpy.data.objects.new(f'arrow_{board}', mesh)
        lane.objects.link(arrow)
    gutter = plain_material('gutter', (0.045, 0.045, 0.05), 0.22, coat=0.6)
    for side in (-1, 1):
        add_gutter(f'gutter_{side}', side * (LANE_W / 2 + GUTTER_R), lane_end, gutter, lane)
    capping = plain_material('capping', (0.03, 0.03, 0.032), 0.35, coat=0.4)
    cap_x = LANE_W / 2 + 2 * GUTTER_R + CAP_W / 2
    add_box('capping', (CAP_W, lane_end + 15 * FT, 0.03), (cap_x, (lane_end - 15 * FT) / 2, -0.005), capping, lane)
    kickback = plain_material('kickback', (0.008, 0.008, 0.009), 0.8, specular=0.04)
    add_box('kickback', (CAP_W, 1.9, 0.8), (cap_x, LANE_LEN + 0.75, 0.4), kickback, lane)
    if with_pins:
        add_rack(lane, pin_mesh())
    return lane


def smooth_profile(points: list, subdivisions: int) -> list:
    """Catmull-Rom through the measured profile so the pin's silhouette has no facets."""
    padded = [points[0]] + points + [points[-1]]
    smooth = list()
    for i in range(1, len(padded) - 2):
        p0, p1, p2, p3 = padded[i - 1], padded[i], padded[i + 1], padded[i + 2]
        for step in range(subdivisions):
            t = step / subdivisions
            smooth.append(tuple(0.5 * (2 * p1[k] + (-p0[k] + p2[k]) * t + (2 * p0[k] - 5 * p1[k] + 4 * p2[k] - p3[k]) * t * t
                                       + (-p0[k] + 3 * p1[k] - 3 * p2[k] + p3[k]) * t ** 3) for k in range(2)))
    smooth.append(points[-1])
    return [(max(radius, 0.0), height) for radius, height in smooth]


def pin_mesh() -> bpy.types.Mesh:
    profile = [(0.0, 0.0), (1.016, 0.0), (1.41, 0.75), (2.0, 2.25), (2.32, 3.375), (2.383, 4.5), (2.29, 5.875), (1.97, 7.25),
               (1.47, 8.625), (1.18, 9.375), (0.95, 10.0), (0.9, 10.875), (1.0, 11.75), (1.15, 12.5), (1.27, 13.5), (1.12, 14.3),
               (0.75, 14.8), (0.0, 15.0)]
    profile = smooth_profile(profile, 8)
    mesh = bpy.data.meshes.new('pin')
    bm = bmesh.new()
    verts = [bm.verts.new((radius * IN, 0.0, height * IN)) for radius, height in profile]
    edges = [bm.edges.new((verts[i], verts[i + 1])) for i in range(len(verts) - 1)]
    bmesh.ops.spin(bm, geom=verts + edges, cent=(0, 0, 0), axis=(0, 0, 1), angle=math.radians(360), steps=96, use_duplicate=False)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-6)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(mesh)
    bm.free()
    for polygon in mesh.polygons:
        polygon.use_smooth = True
    mesh.materials.append(pin_material())
    return mesh


def build_house(scene_collection: bpy.types.Collection, lane: bpy.types.Collection) -> None:
    for index in (-2, -1, 1, 2):
        neighbour = bpy.data.objects.new(f'lane_{index}', None)
        neighbour.instance_type = 'COLLECTION'
        neighbour.instance_collection = lane
        neighbour.location = (index * PITCH, 0, 0)
        scene_collection.objects.link(neighbour)
    house = bpy.data.collections.new('house')
    scene_collection.children.link(house)
    # Velvet-black house: no sheen, so the deck light cannot grey the walls.
    black = plain_material('house_black', (0.004, 0.004, 0.0045), 0.9, specular=0.0)
    masking = plain_material('masking', (0.006, 0.006, 0.007), 0.85, specular=0.02)
    span = 5 * PITCH + 1.0
    add_box('masking_unit', (span, 0.12, 0.9), (0, LANE_LEN - 1.45, 1.45), masking, house)
    add_box('pit_curtain', (span, 0.05, 1.4), (0, LANE_LEN + 1.6, 0.6), black, house)
    add_box('pit_floor', (span, 0.6, 0.05), (0, LANE_LEN + 1.25, -0.25), black, house)
    add_box('house_floor', (span + 6, 8, 0.05), (0, -8, -0.04), black, house)
    add_box('approach_floor', (span, 15 * FT, 0.02), (0, -7.5 * FT, -0.012), bpy.data.materials['maple_b'], house)


def build_ball() -> bpy.types.Object:
    bpy.ops.mesh.primitive_uv_sphere_add(radius=BALL_R, segments=192, ring_count=96, location=(0, 0, BALL_R))
    ball = bpy.context.active_object
    ball.name = 'ball'
    bpy.ops.object.shade_smooth()
    ball.data.materials.append(ball_material())
    hole = plain_material('hole', (0.01, 0.01, 0.01), 0.6)
    # Holes face up and back toward a camera behind the ball.
    axis = Vector((0.0, -0.55, 0.84)).normalized()
    turn = axis.to_track_quat('Z', 'Y')
    for name, offset, diameter in (('finger_l', (-0.5 * IN, 1.0 * IN), 0.86 * IN), ('finger_r', (0.5 * IN, 1.0 * IN), 0.86 * IN),
                                   ('thumb', (0.0, -2.9 * IN), 1.0 * IN)):
        bpy.ops.mesh.primitive_cylinder_add(radius=diameter / 2, depth=5 * IN, vertices=96)
        cutter = bpy.context.active_object
        cutter.name = f'cutter_{name}'
        cutter.data.materials.append(hole)
        cutter.parent = ball
        cutter.rotation_mode = 'QUATERNION'
        cutter.rotation_quaternion = turn
        cutter.location = turn @ Vector((offset[0], offset[1], BALL_R))
        modifier = ball.modifiers.new(name, 'BOOLEAN')
        modifier.operation = 'DIFFERENCE'
        modifier.object = cutter
        if hasattr(modifier, 'material_mode'):
            modifier.material_mode = 'TRANSFER'
        cutter.hide_render = True
        cutter.hide_viewport = True
    return ball


def world_and_haze(scene_collection: bpy.types.Collection) -> None:
    world = bpy.data.worlds.new('world')
    bpy.context.scene.world = world
    world.use_nodes = True
    nodes, links = world.node_tree.nodes, world.node_tree.links
    output = nodes.get('World Output')
    lit = nodes.get('Background')
    environment = nodes.new('ShaderNodeTexEnvironment')
    environment.image = bpy.data.images.load(str(ASSETS / 'hdri' / 'studio_small_09_2k.hdr'))
    lit.inputs['Strength'].default_value = 0.12
    links.new(environment.outputs['Color'], lit.inputs['Color'])
    dark = nodes.new('ShaderNodeBackground')
    dark.inputs['Color'].default_value = (0.0, 0.0, 0.0, 1.0)
    light_path = nodes.new('ShaderNodeLightPath')
    mix = nodes.new('ShaderNodeMixShader')
    links.new(light_path.outputs['Is Camera Ray'], mix.inputs['Fac'])
    links.new(lit.outputs['Background'], mix.inputs[1])
    links.new(dark.outputs['Background'], mix.inputs[2])
    links.new(mix.outputs['Shader'], output.inputs['Surface'])
    if not USE_HAZE:
        return
    haze_mat = bpy.data.materials.new('haze')
    haze_mat.use_nodes = True
    haze_nodes = haze_mat.node_tree.nodes
    haze_nodes.remove(haze_nodes.get('Principled BSDF'))
    volume = haze_nodes.new('ShaderNodeVolumePrincipled')
    volume.inputs['Density'].default_value = 0.0025
    volume.inputs['Anisotropy'].default_value = 0.35
    haze_mat.node_tree.links.new(volume.outputs['Volume'], haze_nodes.get('Material Output').inputs['Volume'])
    add_box('haze', (5 * PITCH + 2, LANE_LEN + 14, 4.5), (0, LANE_LEN / 2 - 2, 2.0), haze_mat, scene_collection)


def aim(obj: bpy.types.Object, target: Vector) -> None:
    obj.rotation_euler = (Vector(target) - obj.location).to_track_quat('-Z', 'Y').to_euler()


def add_area(name: str, size: tuple, energy: float, location: tuple, target: tuple | None, rotation_x: float | None = None,
             color: tuple = (1.0, 0.95, 0.88)) -> bpy.types.Object:
    data = bpy.data.lights.new(name, 'AREA')
    data.shape = 'RECTANGLE'
    data.size, data.size_y = size
    data.energy = energy
    data.color = color
    obj = bpy.data.objects.new(name, data)
    obj.location = location
    obj.visible_camera = False
    bpy.context.scene.collection.objects.link(obj)
    if target is not None:
        aim(obj, Vector(target))
    elif rotation_x is not None:
        obj.rotation_euler = (math.radians(rotation_x), 0, 0)
    return obj


def house_lights() -> None:
    add_area('deck_light', (5 * PITCH, 0.14), 1500, (0, LANE_LEN - 1.42, 0.98), None, rotation_x=62, color=(1.0, 0.97, 0.93))
    # Strip lights light the lanes but stay out of reflections (they read as painted bars); a broad dim
    # ceiling glow gives the lane finish a soft sheen instead.
    for index, y in enumerate((1.0, 5.0, 9.0, 13.0)):
        add_area(f'pool_{index}', (1.3 * PITCH, 0.3), 150, (0, y, 3.3), None, rotation_x=0).visible_glossy = False
        add_area(f'house_{index}', (5 * PITCH, 0.3), 30, (0, y + 2.0, 3.6), None, rotation_x=0, color=(1.0, 0.9, 0.8)).visible_glossy = False
    glow = add_area('ceiling_glow', (5 * PITCH, 26.0), 260, (0, 8.0, 4.2), None, rotation_x=0, color=(1.0, 0.93, 0.84))
    glow.visible_diffuse = False
    add_area('approach_spill', (5 * PITCH, 1.2), 40, (0, -3.0, 3.0), None, rotation_x=-20, color=(1.0, 0.9, 0.8))


def add_camera(name: str, lens: float, fstop: float | None) -> bpy.types.Object:
    data = bpy.data.cameras.new(name)
    data.lens = lens
    data.sensor_fit = 'VERTICAL'
    data.sensor_height = 24.0
    data.clip_start = 0.02
    if fstop is not None:
        data.dof.use_dof = True
        data.dof.aperture_fstop = fstop
    cam = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(cam)
    return cam


def key_motion(obj: bpy.types.Object, centre: Vector, velocity: Vector) -> None:
    step = velocity / FPS
    for frame, sign in ((1, -1), (3, 1)):
        obj.location = centre + step * sign
        obj.keyframe_insert('location', frame=frame)


def key_spin(ball: bpy.types.Object, revs_per_second: float) -> None:
    turn = 2 * math.pi * revs_per_second / FPS
    for frame, sign in ((1, 1), (3, -1)):
        ball.rotation_euler = (turn * sign, 0.0, 0.0)
        ball.keyframe_insert('rotation_euler', frame=frame)


def project(cam: bpy.types.Object, point: Vector) -> list:
    view = world_to_camera_view(bpy.context.scene, cam, Vector(point))
    return [round(view.x * WIDTH, 1), round((1 - view.y) * HEIGHT, 1)]


def shot_hero(ball: bpy.types.Object, lights: dict) -> tuple:
    feet = 25
    centre, velocity = path_point(feet), path_velocity(feet, 7.4)
    cam = add_camera('cam_hero', 35, 2.8)
    eye = centre + Vector((0.0, -0.9, 0.21))
    cam.location = eye
    aim(cam, Vector((0.0, centre.y + 9.1, -0.45)))
    cam.data.dof.focus_distance = (centre - eye).length - BALL_R * 0.6
    key_motion(ball, centre, velocity)
    key_spin(ball, 3.3)
    key_motion(cam, eye, velocity)
    lights['key'] = add_area('hero_key', (0.7, 0.7), 22, tuple(centre + Vector((-0.55, -0.7, 1.0))), tuple(centre))
    lights['rim'] = add_area('hero_rim', (0.5, 0.5), 34, tuple(centre + Vector((0.65, 1.1, 0.6))), tuple(centre))
    return cam, dict(ball=centre, head_pin=Vector((0, LANE_LEN, 0.2)))


def shot_breakpoint(ball: bpy.types.Object, lights: dict) -> tuple:
    feet = 45
    centre, velocity = path_point(feet), path_velocity(feet, 6.8)
    # Centred behind the bowler, like a broadcast: the line goes out to the breakpoint and visibly comes back.
    cam = add_camera('cam_breakpoint', 60, None)
    cam.location = (0.0, 22 * FT, 1.45)
    aim(cam, Vector((0.0, 50 * FT, 0.0)))
    key_motion(ball, centre, velocity)
    key_spin(ball, 4.5)
    lights['key'] = add_area('bp_key', (0.8, 0.8), 30, tuple(centre + Vector((0.7, -0.6, 1.2))), tuple(centre))
    floor = Vector((0, 0, BALL_R))
    path = [path_point(33 + i * 0.5) - floor for i in range(55)]
    return cam, dict(path=path, ball=centre, ball_floor=centre - floor, breakpoint=path_point(43) - floor,
                     oil_left=Vector((-LANE_W / 2, 40 * FT, 0)), oil_right=Vector((LANE_W / 2, 40 * FT, 0)),
                     head_pin=Vector((0, LANE_LEN, 0.38)), pocket=path_point(60) - floor)


def shot_pins(ball: bpy.types.Object, lights: dict) -> tuple:
    ball.hide_render = True
    bpy.context.scene.view_settings.exposure = -0.35
    cam = add_camera('cam_pins', 65, 2.2)
    cam.location = (0.22, LANE_LEN - 2.3, 0.07)
    aim(cam, Vector((0.0, LANE_LEN + 0.3, 0.32)))
    cam.data.dof.focus_distance = (Vector((0, LANE_LEN, 0.2)) - cam.location).length
    lights['rim'] = add_area('pins_rim', (0.6, 0.6), 45, (0.9, LANE_LEN + 0.4, 0.6), (0, LANE_LEN, 0.25))
    lights['key'] = add_area('pins_key', (0.9, 0.9), 18, (-0.9, LANE_LEN - 1.2, 0.7), (0, LANE_LEN, 0.2), color=(1.0, 0.93, 0.85))
    return cam, dict(head_pin_top=Vector((0, LANE_LEN, 0.381)), head_pin_base=Vector((0, LANE_LEN, 0.0)))


def render_settings() -> None:
    scene = bpy.context.scene
    scene.render.engine = 'CYCLES'
    prefs = bpy.context.preferences.addons['cycles'].preferences
    # METAL on the Mac, HIP on the render node's Radeon, CPU as the fallback (BN_DEVICE)
    device_type = os.environ.get('BN_DEVICE', 'METAL')
    prefs.compute_device_type = 'NONE' if device_type == 'CPU' else device_type
    prefs.get_devices()
    for device in prefs.devices:
        device.use = True
    scene.cycles.device = 'GPU'
    scene.cycles.samples = SAMPLES
    scene.cycles.use_denoising = True
    scene.cycles.blur_glossy = 0.5
    scene.cycles.sample_clamp_indirect = 6.0
    scene.render.resolution_x, scene.render.resolution_y = WIDTH, HEIGHT
    scene.render.resolution_percentage = 100
    scene.render.fps = FPS
    scene.render.use_motion_blur = True
    scene.render.motion_blur_shutter = 0.5
    scene.render.image_settings.file_format = 'PNG'
    scene.render.image_settings.color_depth = '16'
    scene.view_settings.view_transform = 'AgX'
    for look in ('AgX - Medium High Contrast', 'Medium High Contrast'):
        try:
            scene.view_settings.look = look
            break
        except TypeError:
            continue
    bpy.context.preferences.edit.keyframe_new_interpolation_type = 'LINEAR'


def main() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    render_settings()
    root = bpy.context.scene.collection
    lane = build_lane(root)
    build_house(root, lane)
    world_and_haze(root)
    house_lights()
    ball = build_ball()
    builders = dict(hero=shot_hero, breakpoint=shot_breakpoint, pins=shot_pins)
    scene = bpy.context.scene
    for shot in SHOTS:
        lights = dict()
        ball.hide_render = False
        scene.view_settings.exposure = 0.0
        cam, points = builders[shot](ball, lights)
        scene.camera = cam
        scene.frame_set(2)
        bpy.context.view_layer.update()
        marks = {name: [project(cam, p) for p in value] if isinstance(value, list) else project(cam, value) for name, value in points.items()}
        if not MARKS_ONLY:
            scene.render.filepath = str(OUT / f'{shot}.png')
            bpy.ops.render.render(write_still=True)
        (OUT / f'{shot}.json').write_text(json.dumps(marks, indent=1))
        for light in lights.values():
            light.hide_render = True
        print('rendered', shot)
    if not MARKS_ONLY:
        bpy.ops.wm.save_as_mainfile(filepath=str(HERE / 'lane_scene.blend'))


if __name__ == '__main__':
    main()
