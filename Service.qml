import QtQuick
import Quickshell
import Quickshell.Hyprland
import Quickshell.Io
import Quickshell.Services.Mpris
import Quickshell.Services.UPower
import qs.Commons

// Decides when the lightbar should run and what colours it uses. The drawing
// and audio analysis happen in lightbar.py, a separate lightweight process.
Item {
  id: root

  property var manifest: null

  // Services get no settings injection, so read our own shell.json entry.
  property var entries: []
  readonly property var settings: {
    for (var i = 0; i < entries.length; i++)
      if (entries[i] && manifest && entries[i].id === manifest.id) return entries[i]
    return ({})
  }
  // "edges" takes "all", one side, or a list of sides.
  readonly property var edges: {
    var sides = ["bottom", "top", "left", "right"]
    var want = settings.edges
    if (want === "all") return sides
    var list = Array.isArray(want) ? want : [want]
    var out = sides.filter(function(side) { return list.indexOf(side) !== -1 })
    return out.length > 0 ? out : ["bottom"]
  }
  readonly property real thickness: Number(settings.thickness) > 0 ? Number(settings.thickness) : 4
  readonly property real sensitivity: Number(settings.sensitivity) > 0 ? Number(settings.sensitivity) : 1
  readonly property bool useArt: settings.colors !== "theme"
  readonly property bool glow: settings.glow !== false
  readonly property bool pauseOnBattery: settings.pauseOnBattery === true
  // Each update makes the compositor redraw, so run slower on battery unless told otherwise.
  readonly property real fps: Number(settings.fps) > 0 ? Number(settings.fps) : (UPower.onBattery ? 30 : 50)

  property real gap: 10

  readonly property var players: Mpris.players ? Mpris.players.values : []
  readonly property var player: {
    for (var i = 0; i < players.length; i++)
      if (players[i] && players[i].isPlaying) return players[i]
    return null
  }
  readonly property bool playing: player !== null
  readonly property string artUrl: player && player.trackArtUrl ? player.trackArtUrl : ""

  // A fullscreen window hides the gap, so there is nothing to draw on that monitor.
  readonly property bool seen: {
    var monitors = Hyprland.monitors.values
    for (var i = 0; i < monitors.length; i++) {
      var workspace = monitors[i].activeWorkspace
      if (!workspace || !workspace.hasFullscreen) return true
    }
    return false
  }
  readonly property bool active: playing && seen && !(pauseOnBattery && UPower.onBattery)

  property string artFile: ""
  property var palette: []
  readonly property color colorA: palette.length > 0 ? palette[0] : Color.accent
  readonly property color colorB: palette.length > 1 ? palette[1] : colorA
  readonly property color colorC: palette.length > 2 ? palette[2] : colorB

  function vivid(c) {
    return Qt.hsva(c.hsvHue < 0 ? 0 : c.hsvHue, Math.min(1, c.hsvSaturation * 1.25), Math.max(0.92, c.hsvValue), 1)
  }

  function buildPalette(colors) {
    var list = []
    for (var i = 0; i < colors.length; i++) list.push(colors[i])
    list.sort(function(a, b) {
      return b.hsvSaturation * (0.4 + b.hsvValue) - a.hsvSaturation * (0.4 + a.hsvValue)
    })
    var out = []
    for (var j = 0; j < list.length && out.length < 3; j++) {
      if (list[j].hsvSaturation < 0.18) break
      out.push(vivid(list[j]))
    }
    // Black-and-white art gets a plain white light.
    return out.length > 0 || colors.length === 0 ? out : [Qt.rgba(0.95, 0.95, 0.95, 1)]
  }

  function refreshArt() {
    if (!useArt || !artUrl) {
      artFile = ""
      return
    }
    if (artUrl.indexOf("http") === 0) {
      fetchArt.target = Quickshell.env("XDG_RUNTIME_DIR") + "/omarchy-lightbar-" + Qt.md5(artUrl)
      fetchArt.command = ["curl", "-fsSL", "--max-time", "10", "-o", fetchArt.target, artUrl]
      fetchArt.running = true
    } else {
      artFile = artUrl
    }
  }

  onArtUrlChanged: refreshArt()
  onUseArtChanged: refreshArt()
  onArtFileChanged: if (!artFile) palette = []
  Component.onCompleted: refreshArt()

  Process {
    id: fetchArt
    property string target: ""
    onExited: function(code) {
      if (code === 0) root.artFile = "file://" + target
    }
  }

  ColorQuantizer {
    source: root.artFile
    depth: 3
    rescaleSize: 64
    onColorsChanged: if (root.artFile) root.palette = root.buildPalette(colors)
  }

  FileView {
    path: Quickshell.env("HOME") + "/.config/omarchy/shell.json"
    watchChanges: true
    onFileChanged: reload()
    onLoaded: {
      try {
        root.entries = JSON.parse(text()).plugins || []
      } catch (e) {
        root.entries = []
      }
    }
  }

  Process {
    running: true
    command: ["hyprctl", "-j", "getoption", "general:gaps_out"]
    stdout: StdioCollector {
      onStreamFinished: {
        try {
          var parts = String(JSON.parse(text).css || "").split(" ")
          var value = Number(parts[0])
          if (value > 0) root.gap = value
        } catch (e) {}
      }
    }
  }

  IpcHandler {
    target: "lightbar"

    function status(): string {
      return JSON.stringify({
        playing: root.playing,
        active: root.active,
        running: renderer.running,
        failed: root.failed,
        colors: [String(root.colorA), String(root.colorB), String(root.colorC)],
        edges: root.edges,
        gap: root.gap
      })
    }
  }

  readonly property var args: ["--edges", edges.join(","), "--gap", String(gap),
    "--thickness", String(thickness), "--sensitivity", String(sensitivity), "--glow", String(glow), "--fps", String(fps)]
  property double startedAt: 0
  property bool stopping: false
  property int failures: 0
  readonly property bool failed: failures >= 3

  function sendColors() {
    if (renderer.running) renderer.write("colors " + colorA + " " + colorB + " " + colorC + "\n")
  }
  onColorAChanged: sendColors()
  onColorBChanged: sendColors()
  onColorCChanged: sendColors()

  // Asking the renderer to quit lets it fade out; onExited brings it back if it is still wanted.
  function stop() {
    stopping = true
    renderer.write("quit\n")
  }
  function sync() {
    if (active && !renderer.running && !failed) renderer.running = true
    else if (!active && renderer.running) stop()
  }
  onActiveChanged: {
    if (active) failures = 0
    sync()
  }
  onArgsChanged: if (renderer.running) stop()

  Process {
    id: renderer
    stdinEnabled: true
    command: ["python3", Qt.resolvedUrl("lightbar.py").toString().replace("file://", "")].concat(root.args)
    onStarted: {
      root.startedAt = Date.now()
      root.sendColors()
    }
    onExited: {
      // An exit we did not ask for, straight after starting, is a failure; give up
      // after three in a row so a broken system does not respawn forever.
      if (!root.stopping && Date.now() - root.startedAt < 2000) {
        root.failures += 1
        if (root.failed) console.warn("lightbar: renderer keeps exiting; run lightbar.py by hand to see why")
      } else {
        root.failures = 0
      }
      root.stopping = false
      retry.restart()
    }
  }

  Timer { id: retry; interval: 500; onTriggered: root.sync() }
}
