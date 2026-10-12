# Changelog

All notable changes to WwiseConversionAnalyser are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the version numbers follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-10-12

First release.

### Added

- Right-click command **WwiseConversionAnalyser** for Sounds: compares the original file with its converted
  media, with all channels mixed to mono.
- Right-click command **WwiseConversionAnalyser (per-channel)**: compares every channel on its own, pairing
  channels by content and naming ambisonic channels by letter. Where a conversion changes the number of
  channels, it shows which channels of the original each converted channel was made from.
- One self-contained HTML report per run, opened in the default web browser, with a tab per Sound when several
  are selected.
- Report contents: codec, bit rate, sample rate, channels, length and size of both files; converted size as a
  share of the original; peak and RMS level; clipping; low- and high-frequency cutoffs; the platform; average
  spectrum with a difference curve; spectrograms of the original, the converted file and the level change, with values read out
  under the pointer.
- Handling of the trim set on a source in Wwise, of conversions to a lower sample rate, and of converted files
  that are out of step with the original.
- Command-line use, including a mode that compares two files without Wwise.
- The vgmstream command-line decoder (r2117) is included, unmodified, in `vgmstream-win64`, under its own
  licence; see `THIRD_PARTY_NOTICES.md`.
