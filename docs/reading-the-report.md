# Reading the report

The report compares a Sound's original file with the media Wwise converted it to. Everything in it describes
the stretch of audio the two have in common: if the source has a trim in Wwise, only the trimmed range is
compared.

- [Conversion Information](#conversion-information)
- [Spectrum](#spectrum)
- [Spectrograms](#spectrograms)
- [Per-channel reports](#per-channel-reports)
- [Reports on several Sounds](#reports-on-several-sounds)
- [The warning box](#the-warning-box)

## Conversion Information

### Original and Converted

The two files side by side, one fact per line, so that a change is easy to spot.

| Line | Meaning |
|---|---|
| Path | Where the file is. The converted file is in the project's cache. |
| Codec | `PCM` for the original. For the converted file, the codec as vgmstream names it, such as `Vorbis`, `libopus Opus` or `Platinum 4-bit ADPCM`. |
| Bit-Rate | For the original, its bit depth. For the converted file, its bit rate. |
| Sample Rate | A lower figure on the right means the conversion reduced the sample rate. |
| Channels | A different figure on the right means the conversion mixed channels down, dropped some, or added some. |
| Length | The whole file. The converted file is shorter than the original when the source is trimmed. |
| Size | In bytes, on disk. |

### Key numbers

| Row | Meaning |
|---|---|
| Converted size | The converted file's size as a percentage of the original's, and the saving. |
| Peak level | The highest sample level of each, in dBFS, and the difference. Lossy codecs change the waveform's shape, so the peak can go up as well as down. |
| RMS level | The average level of each and the difference. A drop usually means part of the spectrum was removed. |
| Clipping | `None`, or how many samples of the converted file sit in flat spots at full scale, which is the mark a cut-off waveform leaves. The original's count is given too, so that clipping the conversion added can be told from clipping that was already there. |
| Low-frequency cutoff | Where the converted file's low end falls away, or `None`. |
| High-frequency cutoff | Where its high end falls away, or `None`. |
| Platform | The Wwise platform whose conversion this is: the one selected in Wwise when the tool was run. Each platform has its own conversion settings and its own converted file. |

A cutoff is given as two frequencies, for example `4.5 kHz (nothing above 4.8 kHz)`:

- the first is where the converted file has fallen **3 dB** below the original, the usual definition of a
  filter's cutoff;
- the second is where it is **40 dB** below, which is taken to mean nothing is left.

Close together, they mean a steep cut; far apart, a gentle roll-off. A file converted to a lower sample rate
has a high-frequency cutoff a little under half the new rate.

## Spectrum

The average spectrum of the whole compared range.

- **Upper panel.** Level against frequency for the original (blue) and the converted file (orange). Where only
  one line is visible, the two agree.
- **Lower panel.** The converted file minus the original, in dB, smoothed over a twelfth of an octave. A flat
  line at 0 means no change. The panel shows ±3 dB; larger differences run off its edge.

Move the pointer over the chart to read the frequency, both levels and the difference.

## Spectrograms

Three images with time running left to right and frequency bottom to top, on a logarithmic scale from 20 Hz.

- **Original** and **Converted** show level by colour, from black (−100 dBFS or lower) through purple and
  orange to pale yellow (0 dBFS).
- **Converted minus Original** shows the change: red where the converted file is louder, blue where it is
  quieter, grey where it is unchanged. The colours run to ±6 dB; anything beyond shows as the end colour.

Things to look for:

- **A solid blue band across the top** of the change image: a high-frequency cutoff.
- **Blue and red speckle in quiet regions**: the codec's noise, most visible where the original is quiet.
- **Blue or red at the start or end only**: a fade on the source, or the converted file being out of step.

Move the pointer over any spectrogram to read the time, the frequency, both levels and the difference. The
line with the values stays at the bottom of the window.

Each column of a spectrogram summarises about a third of a second of audio (at 48 kHz), which gives fine
frequency detail at the cost of timing: a click appears as a broad stripe, not a thin line.

## Per-channel reports

The **WwiseConversionAnalyser (per-channel)** command compares each channel on its own. The report has:

- a table under Conversion Information with a column per channel (a row per channel when there are more than
  six), holding each channel's peak level, RMS level, clipping and cutoffs;
- a tab per channel, which switches the Spectrum and Spectrograms sections.

In the table, levels are written `original → converted (difference)`.

### Channel names

| Layout | Names |
|---|---|
| Mono, stereo | `Mono`; `Left`, `Right` |
| Surround, when the original file states its speakers | `Front Left`, `Front Right`, `Centre`, `LFE`, `Back Left`, and so on |
| Ambisonics | By letter: `W`, `Y`, `Z`, `X` for first order, in the order Wwise keeps them |
| Anything else | `Channel 1`, `Channel 2`, ... |

### When channels move

A conversion may reorder channels. Wwise does so with ambisonics: a first-order file read as W, X, Y, Z is
stored as W, Y, Z, X. The tool therefore pairs channels by what they contain, not by their position, and adds a
row saying where each sits, for example `2nd in the original, 4th in the converted file`.

### When the number of channels changes

If the conversion mixed channels down, dropped some or added some, the report follows the channels of the
converted file. Three more lines appear:

| Line | Meaning |
|---|---|
| Channels setting in Wwise | The Channels option of the Sound's conversion settings: `Mono`, `Stereo`, `Mono Drop`, `Stereo Drop` or `As Input`. |
| Original channels left out | Channels of the original that are in none of the converted channels. |
| Made from (channels of the original) | What each converted channel carries, for example `0.50 × Front Left + 0.35 × Centre + 0.35 × Back Left`. A name on its own means the channel was kept as it was. |

Each converted channel is then compared with that same mix of the original, so its charts show what the
conversion did beyond mixing the channels. The shares are measured from the audio.

## Reports on several Sounds

When several Sounds are selected, the report has a row of tabs at the top, one per Sound. Each tab holds
exactly what that Sound's own report would. In a per-channel report, a Sound's channel tabs sit under the row
of Sound tabs.

A report on more than six Sounds draws its spectrograms at half resolution to keep the page a usable size.

## The warning box

A yellow box headed **Check before trusting this comparison** appears when the converted file does not fit
the Sound as it is set up now, for example when its length is wrong for the trim, or its audio does not match
the original's. The usual cause is a converted file that is out of date. Convert the Sound again in Wwise and
run the tool again.
