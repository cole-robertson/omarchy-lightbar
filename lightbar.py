#!/usr/bin/env python3
"""Lightbar renderer: draws beat-reactive light strips in the screen-edge gap.

A dependency-free Wayland client. The gradient is drawn once into a small
buffer; on each beat the compositor stretches and fades it (viewporter and
alpha-modifier), so the per-frame cost is a few bytes on a socket.

The plugin starts this while music plays, passes settings as arguments, and
sends `colors #rrggbb #rrggbb #rrggbb` lines on stdin. `quit` fades the bar
out and exits; closing stdin exits at once.
"""
import array
import math
import mmap
import os
import select
import socket
import struct
import subprocess
import sys
import time

ALONG, ACROSS = 512, 32  # texture size: along the strip, across the gap
RATE, FRAME = 2000, 40
DT = FRAME / RATE  # 20 ms


def parse_args():
    config = {"edges": ["bottom"], "gap": 10, "thickness": 4.0, "sensitivity": 1.0, "glow": True, "fps": 50.0}
    args = sys.argv[1:]
    for key, value in zip(args[::2], args[1::2]):
        if key == "--edges":
            config["edges"] = [e for e in value.split(",") if e in ("bottom", "top", "left", "right")]
        elif key == "--gap":
            config["gap"] = max(1, round(float(value)))
        elif key == "--thickness":
            config["thickness"] = float(value)
        elif key == "--sensitivity":
            config["sensitivity"] = float(value)
        elif key == "--glow":
            config["glow"] = value != "false"
        elif key == "--fps":
            config["fps"] = max(1.0, float(value))
    return config


def string(text):
    data = text.encode() + b"\0"
    return struct.pack("<I", len(data)) + data + b"\0" * (-len(data) % 4)


class Wayland:
    """Just enough of the wire protocol: send requests, read events."""

    def __init__(self):
        path = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
        if not path.startswith("/"):
            path = os.path.join(os.environ["XDG_RUNTIME_DIR"], path)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.next_id = 2
        self.out = bytearray()
        self.fds = []
        self.pending = b""

    def new_id(self):
        self.next_id += 1
        return self.next_id - 1

    def send(self, obj, opcode, payload=b"", fd=None):
        self.out += struct.pack("<IHH", obj, opcode, 8 + len(payload)) + payload
        if fd is not None:
            self.fds.append(fd)

    def flush(self):
        if not self.out:
            return
        if self.fds:
            socket.send_fds(self.sock, [bytes(self.out)], self.fds)
        else:
            self.sock.sendall(self.out)
        self.out.clear()
        self.fds.clear()

    def events(self):
        """Read what is available and yield (object, opcode, body)."""
        data = self.sock.recv(65536)
        if not data:
            raise SystemExit(0)
        self.pending += data
        while len(self.pending) >= 8:
            obj, opcode, size = struct.unpack_from("<IHH", self.pending)
            if len(self.pending) < size:
                break
            body, self.pending = self.pending[8:size], self.pending[size:]
            yield obj, opcode, body


def parse_color(text):
    text = text.lstrip("#")
    text = text[2:] if len(text) == 8 else text  # Qt prints alpha as #aarrggbb
    return tuple(int(text[i:i + 2], 16) / 255 for i in (0, 2, 4))


class Lightbar:
    def __init__(self, config):
        self.config = config
        self.wl = Wayland()
        self.globals = {}
        self.outputs = []
        self.strips = {}  # layer surface id -> strip
        self.colors = self.target = [(0.95, 0.95, 0.95)] * 3
        self.fresh = True  # the first palette snaps in; later ones fade
        self.level = self.flash = 0.0
        self.slot = 0
        self.drawn_at = 0.0
        self.connect()

    # -- setup ---------------------------------------------------------------

    def connect(self):
        wl = self.wl
        self.registry = wl.new_id()
        wl.send(1, 1, struct.pack("<I", self.registry))  # wl_display.get_registry
        done = wl.new_id()
        wl.send(1, 0, struct.pack("<I", done))  # wl_display.sync
        wl.flush()
        synced = False
        while not synced:
            for obj, opcode, body in wl.events():
                if obj == self.registry and opcode == 0:
                    name, length = struct.unpack_from("<II", body)
                    interface = body[8:8 + length - 1].decode()
                    version = struct.unpack_from("<I", body, 8 + length + (-length % 4))[0]
                    if interface == "wl_output":
                        self.outputs.append(name)
                    else:
                        self.globals[interface] = (name, version)
                elif obj == done:
                    synced = True

        self.compositor = self.bind_global("wl_compositor", 4)
        self.shm = self.bind_global("wl_shm", 1)
        self.subcompositor = self.bind_global("wl_subcompositor", 1)
        self.viewporter = self.bind_global("wp_viewporter", 1)
        self.layer_shell = self.bind_global("zwlr_layer_shell_v1", 1)
        self.alpha = self.bind_global("wp_alpha_modifier_v1", 1, required=False)

        # One shared pool: a transparent pixel for the parent surfaces, then two
        # alternating textures for each orientation.
        texture = ALONG * ACROSS * 4
        size = 4096 + texture * 4
        fd = os.memfd_create("omarchy-lightbar")
        os.ftruncate(fd, size)
        self.pixels = mmap.mmap(fd, size)
        pool = wl.new_id()
        wl.send(self.shm, 0, struct.pack("<Ii", pool, size), fd=fd)
        wl.flush()
        os.close(fd)

        def buffer(offset, width, height):
            ident = wl.new_id()
            # wl_shm_pool.create_buffer, format 0 = ARGB8888
            wl.send(pool, 0, struct.pack("<IiiiiI", ident, offset, width, height, width * 4, 0))
            return ident

        self.blank = buffer(0, 1, 1)
        self.textures = {
            False: [(4096 + texture * i, buffer(4096 + texture * i, ALONG, ACROSS)) for i in (0, 1)],
            True: [(4096 + texture * i, buffer(4096 + texture * i, ACROSS, ALONG)) for i in (2, 3)],
        }
        self.paint()
        for name in self.outputs:
            self.add_output(name)
        wl.flush()

    def add_output(self, name):
        output = self.bind(name, "wl_output", 1)
        for edge in self.config["edges"]:
            self.add_strip(output, edge)

    def bind(self, name, interface, version):
        ident = self.wl.new_id()
        self.wl.send(self.registry, 0, struct.pack("<I", name) + string(interface) + struct.pack("<II", version, ident))
        return ident

    def bind_global(self, interface, version, required=True):
        if interface not in self.globals:
            if required:
                raise SystemExit("compositor does not support " + interface)
            return None
        name, available = self.globals[interface]
        return self.bind(name, interface, min(version, available))

    def surface(self):
        """A surface that never takes pointer input."""
        wl = self.wl
        surface, region = wl.new_id(), wl.new_id()
        wl.send(self.compositor, 0, struct.pack("<I", surface))  # create_surface
        wl.send(self.compositor, 1, struct.pack("<I", region))  # create_region
        wl.send(surface, 5, struct.pack("<I", region))  # set_input_region
        wl.send(region, 0)  # destroy
        viewport = wl.new_id()
        wl.send(self.viewporter, 1, struct.pack("<II", viewport, surface))  # get_viewport
        return surface, viewport

    def add_strip(self, output, edge):
        wl = self.wl
        gap = self.config["gap"]
        vertical = edge in ("left", "right")
        parent, parent_viewport = self.surface()
        layer = wl.new_id()
        # Bottom layer (1): tiled windows leave the gap clear, while floating and
        # fullscreen windows cover the bar.
        wl.send(self.layer_shell, 0, struct.pack("<IIII", layer, parent, output, 1) + string("omarchy-lightbar"))
        wl.send(layer, 0, struct.pack("<II", gap if vertical else 0, 0 if vertical else gap))  # set_size
        anchors = {"top": 1 | 4 | 8, "bottom": 2 | 4 | 8, "left": 4 | 1 | 2, "right": 8 | 1 | 2}
        wl.send(layer, 1, struct.pack("<I", anchors[edge]))  # set_anchor
        # Zero keeps the strips clear of the bar without reserving space.
        wl.send(layer, 2, struct.pack("<i", 0))  # set_exclusive_zone
        wl.send(parent, 6)  # commit

        child, child_viewport = self.surface()
        subsurface = wl.new_id()
        wl.send(self.subcompositor, 1, struct.pack("<III", subsurface, child, parent))  # get_subsurface
        fade = None
        if self.alpha:
            fade = wl.new_id()
            wl.send(self.alpha, 1, struct.pack("<II", fade, child))  # get_surface
        self.strips[layer] = {
            "vertical": vertical, "parent": parent, "parent_viewport": parent_viewport,
            "child": child, "child_viewport": child_viewport, "subsurface": subsurface,
            "fade": fade, "size": None, "drawn": None,
        }

    # -- drawing -------------------------------------------------------------

    def paint(self):
        """Render the gradient into the idle texture slot of each orientation."""
        a, b, c = self.colors
        stops = [(0.0, c, 0.0), (0.18, c, 1.0), (0.36, b, 1.0), (0.5, a, 1.0),
                 (0.64, b, 1.0), (0.82, c, 1.0), (1.0, c, 0.0)]
        line = []
        for i in range(ALONG):
            t = i / (ALONG - 1)
            k = next(n for n in range(1, 7) if stops[n][0] >= t)
            (p0, c0, a0), (p1, c1, a1) = stops[k - 1], stops[k]
            f = (t - p0) / (p1 - p0)
            line.append(tuple(c0[n] + (c1[n] - c0[n]) * f for n in range(3)) + (a0 + (a1 - a0) * f,))

        # Soft falloff across the gap; a hard-edged core when glow is off.
        thickness, gap = self.config["thickness"], self.config["gap"]
        profile = []
        for j in range(ACROSS):
            d = abs(j + 0.5 - ACROSS / 2) * gap / ACROSS
            if self.config["glow"]:
                profile.append(math.exp(-((d / (thickness * 0.75)) ** 2)))
            else:
                profile.append(min(1.0, max(0.0, thickness / 2 - d + 0.5)))

        def pixel(i, j):
            r, g, b_, alpha = line[i]
            alpha *= profile[j]
            # Premultiplied ARGB8888, little endian.
            return int(b_ * alpha * 255), int(g * alpha * 255), int(r * alpha * 255), int(alpha * 255)

        self.slot ^= 1
        edges = self.config["edges"]
        if "bottom" in edges or "top" in edges:
            flat = array.array("B")
            for j in range(ACROSS):
                for i in range(ALONG):
                    flat.extend(pixel(i, j))
            offset = self.textures[False][self.slot][0]
            self.pixels[offset:offset + len(flat)] = flat.tobytes()
        if "left" in edges or "right" in edges:
            tall = array.array("B")
            for i in range(ALONG):
                for j in range(ACROSS):
                    tall.extend(pixel(i, j))
            offset = self.textures[True][self.slot][0]
            self.pixels[offset:offset + len(tall)] = tall.tobytes()
        for strip in self.strips.values():
            strip["drawn"] = None
            if strip["size"]:
                self.attach(strip)

    def attach(self, strip):
        vertical = strip["vertical"]
        width, height = (ACROSS, ALONG) if vertical else (ALONG, ACROSS)
        self.wl.send(strip["child"], 1, struct.pack("<Iii", self.textures[vertical][self.slot][1], 0, 0))
        self.wl.send(strip["child"], 9, struct.pack("<iiii", 0, 0, width, height))  # damage_buffer

    def configure(self, layer, serial, width, height):
        wl = self.wl
        strip = self.strips[layer]
        wl.send(layer, 6, struct.pack("<I", serial))  # ack_configure
        first = strip["size"] is None
        strip["size"] = (width, height)
        strip["drawn"] = None
        wl.send(strip["parent"], 1, struct.pack("<Iii", self.blank, 0, 0))  # attach
        wl.send(strip["parent_viewport"], 2, struct.pack("<ii", width, height))  # set_destination
        if first:
            self.attach(strip)
        self.draw(strip)

    def draw(self, strip):
        """Stretch and fade the cached texture; no pixels are touched here."""
        if not strip["size"]:
            return
        wl = self.wl
        width, height = strip["size"]
        gap = self.config["gap"]
        span = (height if strip["vertical"] else width) - gap * 2
        length = max(2, 2 * round(span * (0.06 + 0.94 * self.level) / 2))
        fade = round((0.55 + 0.45 * self.flash) * 64)
        if strip["drawn"] == (length, fade):
            return
        strip["drawn"] = (length, fade)
        if strip["vertical"]:
            size, position = (width, length), (0, (height - length) // 2)
        else:
            size, position = (length, height), ((width - length) // 2, 0)
        # Hyprland only picks up a new destination size along with a buffer attach.
        wl.send(strip["child"], 1, struct.pack("<Iii", self.textures[strip["vertical"]][self.slot][1], 0, 0))
        wl.send(strip["child_viewport"], 2, struct.pack("<ii", *size))  # set_destination
        if strip["fade"]:
            wl.send(strip["fade"], 1, struct.pack("<I", fade * 0xFFFFFFFF // 64))  # set_multiplier
        wl.send(strip["child"], 6)  # commit
        wl.send(strip["subsurface"], 1, struct.pack("<ii", *position))  # set_position
        wl.send(strip["parent"], 6)  # commit

    def redraw(self, force=False):
        now = time.monotonic()
        if not force and now - self.drawn_at < 1 / self.config["fps"] - 0.004:
            return
        self.drawn_at = now
        for strip in self.strips.values():
            self.draw(strip)
        self.wl.flush()

    def set_colors(self, colors):
        self.target = colors
        if self.fresh:
            self.fresh = False
            self.colors = colors
            self.paint()

    def fade_colors(self):
        """Ease one step toward a new palette; repainting is cheap but not free."""
        if self.colors == self.target:
            return
        mixed = [tuple(c + (t - c) * 0.4 for c, t in zip(color, goal)) for color, goal in zip(self.colors, self.target)]
        close = all(abs(c - t) < 0.02 for color, goal in zip(mixed, self.target) for c, t in zip(color, goal))
        self.colors = self.target if close else mixed
        self.paint()

    def fade_out(self):
        for _ in range(14):
            self.level *= 0.7
            self.flash = 0.0
            self.redraw(force=True)
            time.sleep(0.02)

    def dispatch(self):
        for obj, opcode, body in self.wl.events():
            if obj == self.registry and opcode == 0:  # a monitor was plugged in
                name, length = struct.unpack_from("<II", body)
                if body[8:8 + length - 1] == b"wl_output":
                    self.add_output(name)
            elif obj in self.strips and opcode == 0:
                self.configure(obj, *struct.unpack("<III", body))
            elif obj in self.strips and opcode == 1:  # closed
                del self.strips[obj]
            elif obj == 1 and opcode == 0:  # wl_display.error
                raise SystemExit("wayland error: " + body[12:].split(b"\0")[0].decode(errors="replace"))
        self.wl.flush()


class Beat:
    """Turns raw 4 kHz mono samples into bar level and beat flash."""

    def __init__(self):
        # Biquad low-pass at 150 Hz isolates kick and bass.
        w = 2 * math.pi * 150 / RATE
        alpha = math.sin(w) / (2 * 0.707)
        a0 = 1 + alpha
        self.b1 = (1 - math.cos(w)) / a0
        self.b0 = self.b2 = self.b1 / 2
        self.a1 = -2 * math.cos(w) / a0
        self.a2 = (1 - alpha) / a0
        self.x1 = self.x2 = self.y1 = self.y2 = 0.0
        self.bass_ceiling = self.full_ceiling = 0.02
        self.average = self.punch = self.flash = self.level = 0.0

    def feed(self, samples):
        b0, b1, b2, a1, a2 = self.b0, self.b1, self.b2, self.a1, self.a2
        x1, x2, y1, y2 = self.x1, self.x2, self.y1, self.y2
        bass = full = 0.0
        for s in samples:
            x = s / 32768
            y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
            x2, x1, y2, y1 = x1, x, y1, y
            bass += y * y
            full += x * x
        self.x1, self.x2, self.y1, self.y2 = x1, x2, y1, y2
        bass = math.sqrt(bass / FRAME)
        full = math.sqrt(full / FRAME)

        # Auto-gain against slowly decaying ceilings so it works at any volume.
        self.bass_ceiling = max(0.004, bass, self.bass_ceiling * math.exp(-DT / 8))
        self.full_ceiling = max(0.004, full, self.full_ceiling * math.exp(-DT / 8))
        kick = bass / self.bass_ceiling
        body = full / self.full_ceiling

        # A beat is bass jumping well above its recent average.
        onset = kick - self.average
        self.flash *= math.exp(-DT * 9)
        self.punch *= math.exp(-DT * 5.5)
        if onset > 0.22 and kick > 0.35:
            self.flash = 1.0
            self.punch = max(self.punch, min(1.0, 0.55 + onset))
        self.average += (kick - self.average) * min(1.0, DT * 4)

        # Beats throw the bar outward; a low bed of overall level keeps it alive between them.
        target = min(1.0, max(self.punch, 0.3 * body * body + 0.25 * kick * kick))
        self.level += (target - self.level) * min(1.0, DT * (45 if target > self.level else 9))


def main():
    config = parse_args()
    bar = Lightbar(config)
    beat = Beat()
    # Captures the default sink at 2 kHz mono; PipeWire does the resampling.
    capture = subprocess.Popen(
        ["pw-cat", "-r", "-P", "{ stream.capture.sink=true node.name=omarchy-lightbar }",
         "--rate", str(RATE), "--channels", "1", "--format", "s16", "--latency", "20ms", "-"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    audio = capture.stdout.fileno()
    samples = b""
    commands = b""
    ticks = 0
    try:
        while True:
            ready, _, _ = select.select([bar.wl.sock, audio, 0], [], [])
            if bar.wl.sock in ready:
                bar.dispatch()
            if audio in ready:
                chunk = os.read(audio, 4096)
                if not chunk:
                    break
                samples += chunk
                while len(samples) >= FRAME * 2:
                    beat.feed(struct.unpack("<%dh" % FRAME, samples[:FRAME * 2]))
                    samples = samples[FRAME * 2:]
                bar.level = min(1.0, beat.level * config["sensitivity"])
                bar.flash = beat.flash
                ticks += 1
                if ticks % 4 == 0:
                    bar.fade_colors()
                bar.redraw()
            if 0 in ready:
                chunk = os.read(0, 4096)
                if not chunk:
                    break
                commands += chunk
                *lines, commands = commands.split(b"\n")
                for line in lines:
                    words = line.decode(errors="replace").split()
                    if len(words) > 1 and words[0] == "colors":
                        colors = [parse_color(word) for word in words[1:4]]
                        bar.set_colors((colors + [colors[-1]] * 2)[:3])
                    elif words == ["quit"]:
                        bar.fade_out()
                        return
    finally:
        capture.kill()


if __name__ == "__main__":
    main()
