// Glass, as a MagicMirror_MusicLearningCompanion game package (sdk 1) -- the example that proves the package format.
//
// One long bow. The line must stay smooth: hold the figure above a target HNR for as long as possible.
// The target rises between rounds. Trains the balance of speed, weight and contact point through a whole
// bow. Score: the longest unbroken stretch above the target, in seconds (8 s = full marks).
//
// A round is ONE note (one bow): from its attack to the moment the reading holds its figure. The stretch
// counts only sustain (the attack's noise is not the line's business) and only while the level stays
// MIN_ABOVE_ROOM over the calibrated room. A note shorter than minNoteS is not a round. The target goes up
// 2 dB after a round of 4 s or more, down 2 dB after a miss. Talking, silence and "too soft for this room"
// pause and never score. The target (game, HNR) is sent to the mirror on each change.
//
// The logic is the mirror's built-in Glass game, line for line; only the plumbing changed:
//   d                -> mirror.d          (only the streams manifest.json declares)
//   the clock        -> mirror.now()
//   the score        -> mirror.score("glass", 0..1, detail)
//   the noise target -> mirror.setNoiseTarget({game, hnr})   (manifest output "target.noise")
// and the Canvas line is drawn with Shapes.
import QtQuick
import QtQuick.Shapes
import "js/Noise.js" as Z

Item {
    id: g
    // the Riso inks of the mirror's games (ink, dim, mute, target)
    readonly property color ink: "#F4F1EA"
    readonly property color inkDim: "#5EC8E5"
    readonly property color inkMute: "#2E6F80"
    readonly property color centred: "#FFE800"
    FontLoader { id: mono; source: "fonts/SpaceMono-Regular.ttf" }
    readonly property string font: mono.status === FontLoader.Ready ? mono.font.family : "monospace"

    readonly property var d: mirror.d
    property real target: 16
    readonly property real minNoteS: 3
    property bool inNote: false
    property real noteS: 0
    property real stretch: 0
    property real longest: 0
    property real live: NaN
    property string why: ""
    property var rounds: []               // {target, longest, note}
    property real lastT: 0
    property real crawl: 0

    function sendTarget() { mirror.setNoiseTarget({game: "glass", hnr: target}) }
    Component.onCompleted: sendTarget()

    function endNote() {
        inNote = false
        if (noteS < minNoteS) { why = "that bow was " + noteS.toFixed(1) + " s — a whole bow, please"; return }
        var sc = Z.clamp(longest / 8, 0, 1)                              // 8 s unbroken is three stars
        rounds = rounds.concat([{target: target, longest: longest, note: noteS}])
        mirror.score("glass", sc, longest.toFixed(1) + " s above " + target + " dB")
        target = longest >= 4 ? Math.min(30, target + 2) : Math.max(8, target - 2)
        sendTarget()
    }

    onDChanged: {
        var t = mirror.now(), dt = lastT ? Math.min(0.25, t - lastT) : 0
        lastT = t
        var n = Z.nz(d), lv = d.level || {}
        // the note ends when the reading starts holding its figure
        if (inNote && !n.live && n.held) { endNote(); return }
        var s = Z.guard(d)
        if (!s.ok) { why = s.why === "play" ? (inNote ? "" : "one long bow") : s.why; stretch = 0; return }
        if (Z.num(lv.above_room_db) && lv.above_room_db < Z.MIN_ABOVE_ROOM) { why = "a little more sound — not a whisper"; stretch = 0; return }
        if (!inNote) { inNote = true; noteS = 0; stretch = 0; longest = 0 }
        why = ""
        noteS += dt
        live = s.hnr
        if (s.phase !== "attack" && s.hnr >= target) { stretch += dt; longest = Math.max(longest, stretch) }
        else if (s.phase !== "attack") stretch = 0
    }

    Text {
        objectName: "glassHead"
        anchors.top: parent.top; anchors.left: parent.left
        text: "One long bow: keep the line smoother than " + g.target + " dB as long as you can"
        color: g.ink; font.family: g.font; font.pixelSize: 18
    }
    Text {
        objectName: "glassWhy"
        anchors.top: parent.top; anchors.right: parent.right
        text: g.why ? g.why : g.inNote ? g.stretch.toFixed(1) + " s smooth  ·  best this bow " + g.longest.toFixed(1) + " s" : ""
        color: g.why ? g.inkDim : g.inkMute; font.family: g.font; font.pixelSize: 14
    }

    // the two lines: the target's roughness, faint, above ("smoother than this"); you, below
    Item {
        id: field
        objectName: "glassField"
        anchors.fill: parent; anchors.topMargin: 60; anchors.bottomMargin: 120
        readonly property bool ok: Z.num(g.live) && g.live >= g.target
        Shape {
            anchors.fill: parent
            opacity: 0.35
            ShapePath {
                strokeColor: g.inkDim; strokeWidth: 1.5; fillColor: "transparent"
                PathPolyline { id: targetLine }
            }
        }
        Shape {
            anchors.fill: parent
            ShapePath {
                strokeColor: field.ok ? g.centred : g.ink; strokeWidth: 2.5; fillColor: "transparent"
                PathPolyline { id: youLine }
            }
        }
        Text {
            x: 4; y: field.height * 0.3 - 26 - height
            text: "the target, " + g.target + " dB"
            color: g.inkMute; font.family: g.font; font.pixelSize: 12
        }
        Text {
            x: 4; y: field.height * 0.68 - 26 - height
            text: Z.num(g.live) && g.inNote ? "you, " + (Math.round(g.live / 2) * 2) + " dB" : "you"
            color: g.inkMute; font.family: g.font; font.pixelSize: 12
        }
        function redraw() {
            var W = width, H = height
            targetLine.path = Z.points(0, W, H * 0.3, H * 0.4, Z.grainOf(g.target), g.crawl, 4)
            youLine.path = Z.points(0, W, H * 0.68, H * 0.4, g.inNote ? Z.grainOf(g.live) : 0, g.crawl, 4)
        }
        onWidthChanged: redraw()
        onHeightChanged: redraw()
    }
    // the line crawls 8 times a second (the built-in's pace), only while this game is on screen
    Timer { running: g.visible; interval: 125; repeat: true; onTriggered: { g.crawl += 0.35; field.redraw() } }

    Row {
        objectName: "glassRounds"
        anchors.bottom: parent.bottom; anchors.horizontalCenter: parent.horizontalCenter
        spacing: 24
        Repeater {
            model: g.rounds.slice(-6)
            delegate: Column {
                Text { anchors.horizontalCenter: parent.horizontalCenter; text: modelData.longest.toFixed(1) + " s"
                       color: g.ink; font.family: g.font; font.pixelSize: 18 }
                Text { anchors.horizontalCenter: parent.horizontalCenter; text: "above " + modelData.target + " dB"
                       color: g.inkMute; font.family: g.font; font.pixelSize: 11 }
            }
        }
    }
}
