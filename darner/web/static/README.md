# static

`dejima-wordmark-darkmode.png` is a copy of `assets/`'s, and `favicon.png` is dejima's. Like those in `assets/`, **they are trademarks and are not covered by this repository's GPL licence**; see `assets/README.md`. The wordmark stands in for darner's own until it's drawn.

The fonts in `fonts/` are Jost, IBM Plex Serif and JetBrains Mono, each under the SIL Open Font License with its licence beside it. They're the files from the npm packages `@fontsource/jost`, `@fontsource/ibm-plex-serif` and `@fontsource/jetbrains-mono`, version 5.3.0.

`sources.sha256` holds the hash of each font and image here, and a test checks them against it, so a changed binary shows in review. Update it with `sha256sum fonts/*.woff2 *.png > sources.sha256`, run here.
