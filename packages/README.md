# packages/

One folder per game, named after its id; one file per version:

    packages/io.github.yourname.mygame/1.0.0.json
    packages/io.github.yourname.mygame/thumb.png      (optional, 512 x 288 PNG, at most 512 kB)

Do not write these by hand: `python3 tools/market_tool.py make-entry --zip <zip> --source <release address>`
makes both. The format is in [../schemas/entry.schema.json](../schemas/entry.schema.json); the whole path is in
[../CONTRIBUTING.md](../CONTRIBUTING.md). A pull request from a fork may change only its own folder here.
