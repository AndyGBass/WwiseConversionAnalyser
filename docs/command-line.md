# Command-line options

The right-click commands run `WwiseConversionAnalyser.py`. It can also be run from a terminal, with Wwise open:

```
py WwiseConversionAnalyser.py [options] [Sound ...]
```

`Sound` is a GUID (`{...}`) or a project path (`\Containers\Default Work Unit\MySound`). With none given, the
Sound selected in Wwise is used.

| Option | Meaning |
|---|---|
| `--per-channel` | Analyse each channel on its own instead of mixing them to mono. |
| `--convert-if-missing` | Ask Wwise to convert a Sound whose converted file does not exist yet. |
| `--output-dir FOLDER` | Keep the report in this folder, named after its Sound, instead of writing over the one page in the temp folder. |
| `--no-open` | Do not open the report in the browser. |
| `--scale {1,2,3,4}` | Spectrogram resolution. Default 2, or 1 for more than six Sounds or channels. |
| `--window-size N` | Analysis window in samples (a power of two from 1024 to 32768; default 16384). Larger gives finer frequency detail and coarser timing. |
| `--difference-range DB` | Colour range of the level-change image, in dB either side of no change (default 6). |
| `--vgmstream PATH` | The `vgmstream-cli` to use, if it is not beside the script or on the `PATH`. |
| `--at9tool PATH` | Sony's `at9tool`, which decodes ATRAC9 encoded for motion/haptics, if it is not in Wwise's `plugins` folder or on the `PATH`. |
| `--waapi-url URL` | Address of the Wwise Authoring API, if not the default `ws://127.0.0.1:8080/waapi`. |
| `--verbose` | Also print trim, alignment and timing details, as they happen. |
| `--version` | Print the version and exit. |
| `--help` | List the options. |

## Comparing two files without Wwise

```
py WwiseConversionAnalyser.py --original source.wav --converted converted.wem
```

| Option | Meaning |
|---|---|
| `--original FILE` | The original WAV file. |
| `--converted FILE` | The converted file. A `.wav` is read as it is; anything else is decoded with vgmstream first. |
| `--trim-begin SECONDS`, `--trim-end SECONDS` | Where the converted range starts and ends in the original, if it is only part of it. |
| `--name NAME` | A name for the report; by default, the original file's name. |

## Changing what the right-click commands do

The two commands are defined in `WwiseConversionAnalyser.json`. Options added to an `args` line there apply
every time that command is run. Restart Wwise after editing the file.
