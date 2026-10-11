# Third-party notices

WwiseConversionAnalyser is released under the MIT License (see [LICENSE](LICENSE)). That licence covers
`WwiseConversionAnalyser.py`, `WwiseConversionAnalyser.json` and this documentation only.

The software below is not part of WwiseConversionAnalyser and is not covered by its licence. Each belongs to
its own authors and is used under its own terms. **Read those terms before you use or share it.**

## Included in this download

### vgmstream

The `vgmstream-win64` folder is the unmodified Windows 64-bit command-line build of
[vgmstream](https://github.com/vgmstream/vgmstream), version r2117. WwiseConversionAnalyser runs
`vgmstream-cli.exe` to decode `.wem` files.

- Licence: [`vgmstream-win64/COPYING`](vgmstream-win64/COPYING)
- Readme: [`vgmstream-win64/README.md`](vgmstream-win64/README.md)
- Source code: <https://github.com/vgmstream/vgmstream>

### Libraries that come with vgmstream

vgmstream's Windows build ships with the libraries below, and `vgmstream-cli.exe` needs them to run. The
licences are as vgmstream's own build notes
([`doc/BUILD.md`](https://github.com/vgmstream/vgmstream/blob/master/doc/BUILD.md)) and the libraries
themselves state them.

| Files | Library | Licence | Source |
|---|---|---|---|
| `avcodec-vgmstream-59.dll`, `avformat-vgmstream-59.dll`, `avutil-vgmstream-57.dll`, `swresample-vgmstream-4.dll` | FFmpeg (including libopus) | LGPL 2.1 or later; libopus under a BSD-style licence | <https://github.com/FFmpeg/FFmpeg>, <https://github.com/xiph/opus> |
| `libmpg123-0.dll` | mpg123 | LGPL 2.1 | <https://www.mpg123.de> |
| `libvorbis.dll` | libvorbis and libogg | BSD 3-Clause | <https://xiph.org/vorbis/> |
| `libspeex-1.dll` | Speex | BSD (Xiph.Org variant) | <https://gitlab.xiph.org/xiph/speex> |
| `libcelt-0061.dll`, `libcelt-0110.dll` | CELT | MIT, as vgmstream's build notes give it | <https://gitlab.xiph.org/xiph/celt> |
| `libatrac9.dll` | LibAtrac9 | MIT | <https://github.com/Thealexbarney/LibAtrac9> |
| `libg719_decode.dll` | G.719 reference decoder | vgmstream's build notes give this as "unknown license (reference decoder, possibly not valid/Polycom's)" | <https://github.com/kode54/libg719_decode> |

These files are passed on exactly as vgmstream distributes them. If you need a build without a particular
library, or the source for one, get it from vgmstream or from the library's own project.

## Installed separately

These are not in this download. `pip` installs them from the Python Package Index.

| Package | Licence |
|---|---|
| [numpy](https://numpy.org) | BSD 3-Clause, with bundled components under other permissive licences |
| [waapi-client](https://github.com/audiokinetic/waapi-client-python) | Apache 2.0 |
