# How it works

This page describes what WwiseConversionAnalyser does with a Sound, the settings it uses and how far its
numbers can be relied on. The settings are constants at the top of `WwiseConversionAnalyser.py`.

## Steps

1. **Ask Wwise.** Through the Wwise Authoring API (WAAPI), the script reads which platform is selected in
   Wwise and, for that platform, the Sound's active source: the original file, the converted file, the trim
   set on the source, and the Channels option of its conversion settings. If the converted file does not exist and `--convert-if-missing`
   was given, it asks Wwise to convert the Sound. Nothing else in the project is touched.
2. **Decode.** `vgmstream-cli`, which comes with the tool in the `vgmstream-win64` folder (see
   [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)), decodes the converted `.wem` to a WAV file in a scratch folder, which is deleted
   as soon as the file has been read. Looping information is ignored, so the file is decoded once, as it is.
   A decode that has not finished in 60 seconds is abandoned.
3. **Cut to the trim.** The original is cut to the trim range, because that is the audio Wwise converted.
4. **Match the sample rate.** If the converted file has a different sample rate, it is resampled to the
   original's, so that both are measured over the same stretches of time. The resampling resizes the signal's
   spectrum, which leaves everything below the lower Nyquist frequency unchanged.
5. **Line up.** A stretch from the middle of the original is cross-correlated against the converted file over
   offsets of up to half a second either way, and the converted file is shifted by the best match. Both are
   then cropped to the range they share.
6. **Analyse and draw.** Levels, clipping, spectrum, cutoffs and spectrograms are computed; the spectrograms are
   encoded as PNG images and embedded in one HTML page.
7. **Report.** The page is written to the temp folder and opened in the default web browser.

## Settings

| Setting | Value | Notes |
|---|---|---|
| Analysis window | 16,384 samples, Hann | At 48 kHz: one measurement every 2.9 Hz, from 341 ms of audio. Scaled so that a full-scale sine reads 0 dBFS. |
| Spectrogram size | 2,172 × 800 cells | 1,086 × 400 at half resolution. One cell per screen pixel at twice the size the image is shown. |
| Frequency axis | 20 Hz to half the original's sample rate, logarithmic | Each row is the mean of the spectrum across its span. |
| Level colours | −100 to 0 dBFS | |
| Level-change colours | ±6 dB | Cells where both files are below −110 dB count as unchanged. |
| Average spectrum | Welch's method, 75% overlap | Over the whole compared range. |
| Difference curve | Smoothed over 1/12 octave | |
| Cutoff | 3 dB below the typical difference | "Nothing left" is 40 dB below it. |
| Alignment search | ±0.5 s | A shift is applied only if the match is good enough (correlation of at least 0.2). |
| Length tolerance | 10 ms | Codecs pad to whole blocks, so a few milliseconds are normal. |

## The numbers

**Peak and RMS level** are measured on the audio that is compared: after the trim and, for a converted file at
a lower sample rate, after resampling. The peak of a resampled file is that of the reconstructed waveform,
which can be a little higher than the highest stored sample.

**Clipping** counts samples that sit in a flat spot at full scale: two or more in a row at the highest or
lowest value the file's format can hold, in any one channel. A single sample at full scale is taken to be a
peak that just reaches it. The count covers the whole file, every channel. It reports clipping that is in the
decoded file; it does not predict peaks that would exceed full scale only between samples.

**Cutoffs** are read from the difference curve, measured against its typical (median) value, so that an overall
level change is not mistaken for a cutoff. Frequencies at which the original itself has nothing are ignored:
there is nothing there to lose. The 3 dB point comes from the smoothed curve and the "nothing left" point from
the unsmoothed one, because smoothing carries a steep edge outward.

**Frequency resolution** limits the low end. With a 2.9 Hz measurement spacing, a "nothing below" figure can
sit a few hertz under the true edge.

## Per-channel analysis

**Pairing.** Ten seconds from the middle of every channel of the original are cross-correlated against the
same ten seconds of every channel of the converted file. The best matches are paired first; where two matches
are equally good, a channel keeps its position.

**Ambisonic names.** A `.wem` states in its header whether its channels are ambisonic. If they are, each channel
is named by letter from its position in the converted file, whose order is Wwise's own (ACN): W, Y, Z, X for
first order; V, T, R, S, U are added for second; Q, O, M, K, L, N, P for third. Higher orders have no letters
and are named `ACN 16` and up.

**A different number of channels.** Each converted channel is fitted as a weighted sum of the original's
channels by least squares. The fit uses only the part of the spectrum that holds the lowest 90% of the
converted channel's energy, because audio that the conversion removed from the top would otherwise make every
share come out too small. Shares below 5% of the largest are dropped as noise. The converted channel is then
compared with the original channels mixed in those shares.

## Output to Wwise's log

Wwise reads a command's output only when the command has ended, through a pipe that holds about 4 KB. A script
that writes more than that to a pipe nobody is reading waits for ever. The script therefore holds its output
until the end and writes no more than the pipe can take. In a console, or with `--verbose`, it prints as it
goes.

## Performance

Measured on the development machine with 48 kHz files:

| Sound | Time |
|---|---|
| 1.5 seconds, mono | about 2.5 s |
| 10 minutes, mono | about 10 s |
| 10 minutes, first-order ambisonics, per-channel | about 40 s |

Memory use grows with length and channel count: roughly 0.5 GB per channel for ten minutes of audio.

## Tested with

Wwise 2025.1.3, Windows 11, Python 3.14, numpy 2.5, waapi-client 0.8 and vgmstream r2117, on conversions to
PCM, ADPCM, Vorbis and Opus.
