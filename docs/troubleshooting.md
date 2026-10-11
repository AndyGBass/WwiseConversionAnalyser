# Troubleshooting and limitations

## Troubleshooting

Messages from the tool appear in Wwise's **Logs** view, on the **General** tab.

| What you see | What to do |
|---|---|
| The commands are not in the right-click menu. | Check the folder is named `WwiseConversionAnalyser` and sits directly inside `Add-ons\Commands`, then restart Wwise. The commands appear only for Sounds. |
| `can't open file ...WwiseConversionAnalyser.py` | The folder is misnamed or one level too deep. See step 2 of the installation. |
| `failed to execute: 'py'` | Python's `py` launcher is not installed. Reinstall Python from python.org with the launcher option ticked. |
| `numpy is required` or `waapi-client is required` | Run step 1 of the installation, using the same Python that `py` starts. |
| `Could not connect to Wwise` | Enable the Wwise Authoring API (step 3 of the installation). If Wwise uses a WAMP port other than 8080, add `--waapi-url ws://127.0.0.1:PORT/waapi` to both `args` lines in `WwiseConversionAnalyser.json`. |
| `vgmstream-cli is needed ... but was not found` | The `vgmstream-win64` folder is missing from the tool's folder. Copy the whole download again. |
| `Converted file not found` | Wwise could not convert the Sound. Convert it in Wwise and look at the errors it reports. |
| A yellow box headed "Check before trusting this comparison" | The converted file does not match the Sound as it is set up now, usually because it is out of date. Convert the Sound again and re-run. |
| Nothing seems to happen with a large selection. | It is working. Each short Sound takes a second or two, long files longer, and Wwise shows no output until the end. |

## Good to know

- **The current platform and language are used.** Switch platform in Wwise to analyse another platform's
  conversion.
- **Several Sounds make one report**, with a tab per Sound.
- **The report is one file, written over on every run**:
  `%TEMP%\WwiseConversionAnalyser\WwiseConversionAnalyser_Report.html`. It is self-contained, so save a copy
  if you want to keep or share one. A browser tab that is already open shows the earlier report until it is
  refreshed.
- **Wwise's log** shows the figures for a single Sound. For a multi selection it only says that the tool ran
  and that the report was opened.
- **To uninstall**, delete the `WwiseConversionAnalyser` folder and restart Wwise.

## Limitations

- Windows only.
- Original files must be PCM WAV (8, 16, 24 or 32-bit integer, or 32-bit float), which is what Wwise imports.
- The standard command mixes all channels to mono before comparing, which hides per-channel differences and
  anything that cancels between channels. Use the per-channel command to see those.
- Each spectrogram column summarises about a third of a second of audio (at 48 kHz), so very short events are
  smeared in time. The spectrum and the key numbers are not affected.
- Clipping is reported where the decoded file has flat spots at full scale. Peaks that would exceed full scale
  only between samples are not predicted.
- Long, many-channel files need memory: roughly 0.5 GB per channel for ten minutes of 48 kHz audio.
- Fades set on a source in Wwise are part of the converted file, so they show as level differences at its edges.

## Tested with

Wwise 2025.1.3, Windows 11, Python 3.14, numpy 2.5, waapi-client 0.8 and vgmstream r2117, on conversions to
PCM, ADPCM, Vorbis and Opus.
