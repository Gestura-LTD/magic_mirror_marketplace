# Glass (example package)

The mirror's built-in **Glass** game, ported into the package format to prove it works. It is also the
example in the SDK.

- **Play:** one long bow. Keep the line smoother than the target for as long as you can. After a bow with
  4 seconds or more above the target, the target rises 2 dB; after a miss it falls 2 dB.
- **Reads:** the sound only (`sound.tone`, `sound.signal`, `sound.source`, `sound.source2`, `sound.level`).
  Never the camera, the microphone or the pose.
- **Sends:** its score (`glass`) and its noise target (`target.noise`).

Licences: code MIT (`LICENSE`); art (`thumb.png`) CC BY 4.0, credit "MagicMirror_MusicLearningCompanion contributors";
the font Space Mono is under the SIL Open Font License (`fonts/LICENSE-space-mono.txt`).
