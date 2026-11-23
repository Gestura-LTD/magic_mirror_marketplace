// Hold it -- the smallest useful MagicMirror_MusicLearningCompanion game. Start here.
//
// What the mirror gives you: ONE object, `mirror`.
//   mirror.d            the latest reading, ONLY the streams your manifest.json declares
//                       (here d.pitch and d.signal); it changes about 16 times a second
//   mirror.now()        the time in seconds (the simulator replays recorded time through it)
//   mirror.score(id, 0..1, "detail")   end of a round; id must be in manifest "scoreIds"
//   mirror.exit()       leave the game
// The rules of a mirror game (README.md): no score on screen while playing; pause, never punish,
// when the reading is unsure; short rounds with a clear end.
import QtQuick

Item {
    id: game
    readonly property var d: mirror.d
    readonly property real holdS: 2.0
    property real heldSince: -1          // when the note came inside the band, -1 = not now
    property string word: "play a note"
    property int rounds: 0

    onDChanged: {
        var p = d.pitch || {}, sig = d.signal || {}
        // pause, never punish: no clear single note = no judgement
        if (!sig.sounding || typeof p.cents !== "number" || !p.note) { heldSince = -1; word = "play a note"; return }
        if (Math.abs(p.cents) > 10) { heldSince = -1; word = p.note + (p.cents > 0 ? "  a little high" : "  a little low"); return }
        if (heldSince < 0) heldSince = mirror.now()
        var held = mirror.now() - heldSince
        word = p.note + "  hold it…"
        if (held >= holdS) {
            rounds++
            mirror.score("holdit", 1.0, p.note + " held " + holdS + " s")
            heldSince = -1
            word = "yes! another one?"
        }
    }

    Rectangle { anchors.fill: parent; color: "#14181C" }
    // the ring fills while you hold the note
    Rectangle {
        id: ring
        anchors.centerIn: parent
        width: Math.min(parent.width, parent.height) * 0.5; height: width; radius: width / 2
        color: "transparent"; border.width: 6
        border.color: game.heldSince >= 0 ? "#37D2B4" : "#3A4248"
    }
    Text {
        anchors.centerIn: ring
        text: game.word
        color: "#E6EAED"; font.pixelSize: 40
    }
    Text {
        anchors.top: ring.bottom; anchors.topMargin: 40; anchors.horizontalCenter: parent.horizontalCenter
        text: game.rounds > 0 ? game.rounds + " held" : "hold any note in tune for " + game.holdS + " s"
        color: "#8B959C"; font.pixelSize: 24
    }
}
