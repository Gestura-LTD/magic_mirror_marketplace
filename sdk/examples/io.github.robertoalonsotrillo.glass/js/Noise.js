// The Tone / noise reading for Glass, as a package (sdk 1). Ported unchanged from the mirror's built-in
// games/common/Noise.js (guard, grainOf, clamp); the Canvas line became `points`, drawn by a Shape.
// The figure is `tone.noise.hnr_db` (room-corrected harmonics-to-noise ratio, running median over ~0.4 s of
// sustain). Rules: pause when not `live`, never score while `floor_limited` ("too soft for this room"),
// steps under 2 dB are not told apart.
.pragma library

var FLOOR_DB = 2.0
var MIN_ABOVE_ROOM = 15.0      // the counter to a whispered tone: dB over the calibrated room

function num(v) { return typeof v === "number" && isFinite(v) }
function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v }
function nz(d) { return ((d && d.tone) || {}).noise || {} }

function guard(d) {
    var n = nz(d), sig = (d && d.signal) || {}, src = (d && d.source) || {}, s2 = (d && d.source2) || {}
    if (src.label === "voice" || s2.label === "voice") return {ok: false, why: "talking"}
    if (n.floor_limited) return {ok: false, why: "too soft for this room"}
    if (!sig.sounding || !n.live || !num(n.hnr_db)) return {ok: false, why: "play"}
    return {ok: true, hnr: n.hnr_db, grain: n.grain, word: n.word || "", phase: n.phase}
}
function grainOf(hnr) { return num(hnr) ? clamp((25 - hnr) / 25, 0, 1) : 0 }
// the roughening line as points for a PathPolyline: roughness from grain, `crawl` moves it along
function points(x0, x1, cy, h, grain, crawl, step) {
    var out = [], amp = grain * 0.35 * h, k1 = 0.04 + 0.10 * grain, k2 = 0.11 + 0.25 * grain
    step = step || 4
    for (var x = x0; x <= x1; x += step) {
        var y = cy + amp * (0.6 * Math.sin(x * k1 + crawl * 1.7) * Math.sin(x * 0.013 + crawl * 0.6)
                            + 0.4 * grain * Math.sin(x * k2 - crawl * 2.3) * Math.cos(x * 0.031 + crawl))
        out.push(Qt.point(x, y))
    }
    return out
}
