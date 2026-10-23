# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Phantom: live garment simulation on the GPU with NVIDIA Newton (its Style3D cloth solver).

Newton (https://github.com/newton-physics/newton, Apache-2.0) and NVIDIA Warp are optional: they
are installed into Phantom's user modules folder (the Garment sidebar's "Install GPU Cloth" button
does it), and imported only when a garment is first simulated.

How a garment becomes a Newton cloth:

- Seams are welded: each group of sewn vertices becomes one particle, placed between them. The
  pieces keep their own flat coordinates (Style3D's per-corner "panel" coordinates), which give the
  cloth its rest shape, so a seam is ordinary mesh connectivity and holds without any gap.
- Each piece's fabric sets its triangles' stretch and shear stiffness; bending is averaged.
- Pinned vertices don't move. Elastic segments are springs pulling to their shortened length.
- The avatar is a static collision mesh. Style3D also handles the cloth colliding with itself.
- The drag brush pulls the particles under it towards the mouse while the simulation runs.

The simulation runs on a timer while it plays (a CUDA graph is captured for speed) and writes the
particle positions into the Garment object's mesh every frame.
"""

import os
import sys
import time

import bpy
import numpy as np

# Simulation rate and quality.
FPS = 60
SUBSTEPS = 10
ITERATIONS = 4
# Fabric stiffness, from Phantom's fabric presets (made for Blender's cloth) to Style3D's units.
STRETCH_SCALE = 6.7
SHEAR_SCALE = 0.67
BENDING_SCALE = 4.0e-5
PARTICLE_RADIUS = 4.0e-3
SELF_CONTACT_RADIUS = 4.0e-3
CONTACT_STIFFNESS = 300.0
CONTACT_FRICTION = 0.4
# Spring strengths for elastics and the drag brush, per unit of particle mass (1/s^2).
ELASTIC_STRENGTH = 20000.0
DRAG_STRENGTH = 3000.0
# Seconds over which gravity eases in at the start, while the welded seams pull together.
GRAVITY_EASE = 0.35

_state = {"available": None, "error": "", "sim": None, "kernels": None}


# -----------------------------------------------------------------------------
# Availability

def modules_path():
    """Where Newton and Warp are installed: Phantom's user modules folder (on ``sys.path``)."""
    return bpy.utils.user_resource('SCRIPTS', path="modules")


def available():
    """True when Newton and Warp can be imported and a CUDA GPU is present (checked once)."""
    if _state["available"] is None:
        path = modules_path()
        if path and os.path.isdir(path) and path not in sys.path:
            sys.path.append(path)
        try:
            import warp as wp
            wp.config.quiet = True
            wp.init()
            import newton  # noqa: F401
            _state["available"] = bool(wp.is_cuda_available())
            if not _state["available"]:
                _state["error"] = "No NVIDIA GPU found for GPU cloth"
        except Exception as ex:
            _state["available"] = False
            _state["error"] = str(ex)
    return _state["available"]


def installed():
    path = modules_path()
    return bool(path) and os.path.isdir(os.path.join(path, "newton")) and os.path.isdir(os.path.join(path, "warp"))


def forget_availability():
    _state["available"] = None


def _kernels():
    """Warp kernels for the extra forces (defined on first use, once Warp is importable)."""
    if _state["kernels"] is not None:
        return _state["kernels"]
    import warp as wp

    @wp.kernel
    def spring_forces(q: wp.array(dtype=wp.vec3), a: wp.array(dtype=wp.int32), b: wp.array(dtype=wp.int32),
                      rest: wp.array(dtype=float), stiffness: float, f: wp.array(dtype=wp.vec3)):
        t = wp.tid()
        i = a[t]
        j = b[t]
        d = q[j] - q[i]
        length = wp.length(d)
        if length > 1.0e-9:
            force = d * (stiffness * (length - rest[t]) / length)
            wp.atomic_add(f, i, force)
            wp.atomic_sub(f, j, force)

    @wp.kernel
    def drag_forces(q: wp.array(dtype=wp.vec3), weight: wp.array(dtype=float), grab: wp.array(dtype=wp.vec3),
                    delta: wp.array(dtype=wp.vec3), stiffness: float, f: wp.array(dtype=wp.vec3)):
        i = wp.tid()
        w = weight[i]
        if w > 0.0:
            wp.atomic_add(f, i, (grab[i] + delta[0] - q[i]) * (stiffness * w))

    _state["kernels"] = (spring_forces, drag_forces)
    return _state["kernels"]


# -----------------------------------------------------------------------------
# Welding seams

def weld(data):
    """Merge sewn vertices. Returns (particle of each original vertex, particle count)."""
    count = len(data.verts3d)
    parent = list(range(count))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, j in data.sew:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)
    roots = np.array([find(i) for i in range(count)])
    unique, particle = np.unique(roots, return_inverse=True)
    return particle, len(unique)


# -----------------------------------------------------------------------------
# The simulation

class Simulation:
    def __init__(self, scene, data):
        import warp as wp
        import newton
        from newton.solvers import style3d
        from phantom_garment import FABRICS

        self.wp = wp
        settings = scene.phantom_garment
        self.particle_of, count = weld(data)
        self.count = count
        # Particles start between the vertices they were welded from.
        sums = np.zeros((count, 3))
        np.add.at(sums, self.particle_of, data.verts3d)
        members = np.bincount(self.particle_of, minlength=count)[:, None]
        start = sums / members

        # Triangles, dropping any a seam weld collapsed. Each corner keeps its own flat position.
        tris, panel, tri_fabric = [], [], []
        for face, material in zip(data.faces, data.materials):
            for k in range(1, len(face) - 1):
                corners = (face[0], face[k], face[k + 1])
                welded = [int(self.particle_of[c]) for c in corners]
                if len(set(welded)) == 3:
                    tris.append(welded)
                    panel.append(corners)
                    tri_fabric.append(data.fabrics[material])
        self.tris = np.array(tris, dtype=np.int32)
        self.face_material = [data.fabrics.index(f) for f in tri_fabric]
        fabric_info = [FABRICS[f] for f in tri_fabric]
        tri_ke = [wp.vec3(f["tension"] * STRETCH_SCALE, f["tension"] * STRETCH_SCALE, f["shear"] * SHEAR_SCALE)
                  for f in fabric_info]
        total_area = max(1e-9, sum(data.area.values()))
        density = sum(FABRICS[f]["density"] * data.area[f] for f in data.fabrics) / total_area
        bending = sum(FABRICS[f]["bending"] * data.area[f] for f in data.fabrics) / total_area * BENDING_SCALE

        builder = newton.ModelBuilder(up_axis=newton.Axis.Z)
        newton.solvers.SolverStyle3D.register_custom_attributes(builder)
        style3d.add_cloth_mesh(
            builder,
            pos=wp.vec3(0.0, 0.0, 0.0),
            rot=wp.quat_identity(),
            vel=wp.vec3(0.0, 0.0, 0.0),
            vertices=[wp.vec3(*p) for p in start],
            indices=self.tris.ravel().tolist(),
            panel_verts=[wp.vec2(float(x), float(y)) for x, y, _z in data.flat],
            panel_indices=np.array(panel, dtype=np.int32).ravel().tolist(),
            density=density,
            scale=1.0,
            particle_radius=PARTICLE_RADIUS,
            tri_aniso_ke=tri_ke,
            edge_aniso_ke=wp.vec3(bending, bending, bending * 0.5),
        )
        avatar_points, avatar_tris = _avatar_mesh(settings.avatar)
        if len(avatar_tris):
            builder.add_shape_mesh(body=-1, mesh=newton.Mesh(avatar_points, avatar_tris.ravel()))
            # A floor at the avatar's feet, as in Marvelous Designer, so dropped pieces land.
            builder.add_ground_plane(height=float(avatar_points[:, 2].min()))
        model = builder.finalize()
        # Stiff enough for the avatar to hold the garment up (Newton's example's 10 lets a top
        # slide off the shoulders), with some friction.
        model.soft_contact_ke = CONTACT_STIFFNESS
        model.soft_contact_kd = 1.0e-5
        model.soft_contact_mu = CONTACT_FRICTION
        self.model = model
        self.gravity = -9.81
        model.set_gravity((0.0, 0.0, 0.0))

        # Pins: particles that don't move.
        flags = model.particle_flags.numpy()
        pins = sorted({int(self.particle_of[i]) for i in data.pins})
        if pins:
            flags[pins] = flags[pins] & ~int(newton.ParticleFlags.ACTIVE)
            model.particle_flags = wp.array(flags, device=model.device)

        mass = model.particle_mass.numpy()
        self.mass = float(mass[mass > 0].mean()) if np.any(mass > 0) else 1e-4

        # Elastic segments: springs along them at their shortened length.
        a, b, rest = [], [], []
        for indices, ratio in data.elastic:
            for u, v in zip(indices, indices[1:]):
                pu, pv = int(self.particle_of[u]), int(self.particle_of[v])
                if pu != pv:
                    a.append(pu)
                    b.append(pv)
                    rest.append(float(np.linalg.norm(data.flat[u] - data.flat[v])) * ratio)
        device = model.device
        self.springs = len(a)
        self.spring_a = wp.array(np.array(a or [0], dtype=np.int32), dtype=wp.int32, device=device)
        self.spring_b = wp.array(np.array(b or [0], dtype=np.int32), dtype=wp.int32, device=device)
        self.spring_rest = wp.array(np.array(rest or [0.0], dtype=np.float32), dtype=float, device=device)

        # The drag brush: a weight and grab position per particle, and one shared offset.
        self.drag_weight = wp.zeros(count, dtype=float, device=device)
        self.drag_grab = wp.zeros(count, dtype=wp.vec3, device=device)
        self.drag_delta = wp.zeros(1, dtype=wp.vec3, device=device)
        self.dragging = False

        self.solver = newton.solvers.SolverStyle3D(model=model, iterations=ITERATIONS)
        if self.solver.collision is not None:
            self.solver.collision.radius = SELF_CONTACT_RADIUS
        self.state_0 = model.state()
        self.state_1 = model.state()
        self.control = model.control()
        self.collision = newton.CollisionPipeline(model)
        self.contacts = self.collision.contacts()
        self.dt = 1.0 / FPS / SUBSTEPS
        self.time = 0.0
        self.graph = None
        self.step_ms = 0.0

    def _simulate(self):
        wp = self.wp
        spring_forces, drag_forces = _kernels()
        self.collision.collide(self.state_0, self.contacts)
        for _ in range(SUBSTEPS):
            self.state_0.clear_forces()
            q, f = self.state_0.particle_q, self.state_0.particle_f
            if self.springs:
                wp.launch(spring_forces, dim=self.springs,
                          inputs=[q, self.spring_a, self.spring_b, self.spring_rest, self.mass * ELASTIC_STRENGTH],
                          outputs=[f], device=self.model.device)
            wp.launch(drag_forces, dim=self.count,
                      inputs=[q, self.drag_weight, self.drag_grab, self.drag_delta, self.mass * DRAG_STRENGTH],
                      outputs=[f], device=self.model.device)
            self.solver.step(self.state_0, self.state_1, self.control, self.contacts, self.dt)
            self.state_0, self.state_1 = self.state_1, self.state_0

    def step(self):
        """Advance one frame (1 / FPS seconds)."""
        wp = self.wp
        t0 = time.perf_counter()
        ease = min(1.0, self.time / GRAVITY_EASE)
        self.model.set_gravity((0.0, 0.0, self.gravity * ease))
        if self.graph is None:
            # Compile and warm up, then capture the frame as a CUDA graph.
            self._simulate()
            with wp.ScopedCapture(device=self.model.device) as capture:
                self._simulate()
            self.graph = capture.graph
        else:
            wp.capture_launch(self.graph)
        self.time += 1.0 / FPS
        self.positions()
        self.step_ms = 1000.0 * (time.perf_counter() - t0)

    def positions(self):
        return self.state_0.particle_q.numpy()

    def set_drag(self, weights=None, grab=None, delta=None):
        wp = self.wp
        if weights is not None:
            self.drag_weight.assign(wp.array(weights.astype(np.float32), dtype=float, device=self.model.device))
            self.drag_grab.assign(wp.array(grab.astype(np.float32), dtype=wp.vec3, device=self.model.device))
            self.dragging = bool(np.any(weights > 0.0))
        if delta is not None:
            self.drag_delta.assign(wp.array(np.array([delta], dtype=np.float32), dtype=wp.vec3,
                                            device=self.model.device))

    def release_drag(self):
        self.drag_weight.zero_()
        self.dragging = False


def _avatar_mesh(avatar):
    if avatar is None:
        return np.zeros((0, 3), dtype=np.float32), np.zeros((0, 3), dtype=np.int32)
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = avatar.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        mesh.calc_loop_triangles()
        co = np.empty(len(mesh.vertices) * 3, dtype=np.float64)
        mesh.vertices.foreach_get("co", co)
        co = co.reshape(-1, 3)
        mat = np.array(avatar.matrix_world)
        co = co @ mat[:3, :3].T + mat[:3, 3]
        tris = np.empty(len(mesh.loop_triangles) * 3, dtype=np.int32)
        mesh.loop_triangles.foreach_get("vertices", tris)
    finally:
        evaluated.to_mesh_clear()
    return co.astype(np.float32), tris.reshape(-1, 3)


# -----------------------------------------------------------------------------
# The Garment object and playback

def build(scene, data):
    """Build the Garment object and a Newton simulation for it. Returns (object, message)."""
    import phantom_garment as garment
    stop()
    try:
        sim = Simulation(scene, data)
    except Exception as ex:
        print("Phantom: GPU cloth failed, using Blender cloth:", ex)
        _state["error"] = str(ex)
        return None, "GPU cloth failed: %s" % ex
    mesh = bpy.data.meshes.new(garment.GARMENT_OBJECT)
    mesh.from_pydata(sim.positions().tolist(), [], sim.tris.tolist())
    for fabric in data.fabrics:
        mesh.materials.append(garment.fabric_material(fabric))
    mesh.polygons.foreach_set("material_index", sim.face_material)
    mesh.update()
    mesh.shade_smooth()
    ob = garment.garment_object(scene, mesh)
    _state["sim"] = sim
    return ob, ""


def simulation():
    return _state["sim"]


def running():
    return bpy.app.timers.is_registered(_tick)


def start():
    if _state["sim"] is not None and not running():
        bpy.app.timers.register(_tick, first_interval=0.0)


def stop():
    if running():
        bpy.app.timers.unregister(_tick)


def discard():
    stop()
    _state["sim"] = None


def write_positions(sim=None):
    import phantom_garment as garment
    sim = sim or _state["sim"]
    ob = bpy.data.objects.get(garment.GARMENT_OBJECT)
    if sim is None or ob is None or len(ob.data.vertices) != sim.count:
        return False
    ob.data.vertices.foreach_set("co", sim.positions().astype(np.float32).ravel())
    ob.data.update()
    return True


def _tick():
    sim = _state["sim"]
    if sim is None:
        return None
    try:
        sim.step()
    except Exception as ex:
        print("Phantom: GPU cloth stopped:", ex)
        _state["error"] = str(ex)
        return None
    if not write_positions(sim):
        return None
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
    return max(0.0, 1.0 / FPS - sim.step_ms / 1000.0)


# -----------------------------------------------------------------------------
# Installing

_install = {"process": None, "status": "", "log": ""}


def install_status():
    return _install["status"]


def installing():
    return _install["process"] is not None


def install():
    """Install Newton and Warp into Phantom's user modules folder, in the background (pip)."""
    import subprocess
    if installing():
        return
    target = modules_path()
    os.makedirs(target, exist_ok=True)
    python = sys.executable
    log = os.path.join(bpy.app.tempdir or target, "phantom_gpu_cloth_install.log")
    _install["log"] = log
    _install["status"] = "Installing GPU cloth (about 140 MB)..."
    with open(log, "w", encoding="utf-8") as out:
        _install["process"] = subprocess.Popen(
            [python, "-m", "pip", "install", "--no-deps", "--upgrade", "--target", target, "newton", "warp-lang"],
            stdout=out, stderr=subprocess.STDOUT)
    bpy.app.timers.register(_watch_install, first_interval=1.0)


def _watch_install():
    process = _install["process"]
    if process is None:
        return None
    if process.poll() is None:
        return 1.0
    _install["process"] = None
    forget_availability()
    if process.returncode == 0 and available():
        _install["status"] = "GPU cloth installed"
    else:
        _install["status"] = "Couldn't install GPU cloth (see %s)" % _install["log"]
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            area.tag_redraw()
    return None


class PHANTOM_OT_garment_install_gpu(bpy.types.Operator):
    """Download and install NVIDIA Newton and Warp (about 140 MB) for live GPU cloth"""
    bl_idname = "phantom.garment_install_gpu"
    bl_label = "Install GPU Cloth"

    def execute(self, _context):
        install()
        return {'FINISHED'}


def register():
    bpy.utils.register_class(PHANTOM_OT_garment_install_gpu)


def unregister():
    discard()
    bpy.utils.unregister_class(PHANTOM_OT_garment_install_gpu)
