"""
WwiseConversionAnalyser.py

Wwise right-click command for Sounds: shows what conversion did to each
selected Sound, as one web page with a tab per Sound.

For each Sound (Sound SFX) it reads the original file and the converted file.
If there is a Trim in Wwise, the Trim is replicated on the source and lines 
them up for better comparison. The wems are decoded with vgmstream.
The Analysis reports size, levels, clipping, cutoffs, spectrum and spectrograms.
The files are summed to mono for the overall comparison, however there is a 
per-channel option which compares every channel on its own.
Nothing is changed within the Wwise project.

Needs Python 3.8+ with numpy, Wwise with its Authoring API
enabled, and vgmstream-cli (in the vgmstream-win64 folder beside this script).
The folder holding this script must be named WwiseConversionAnalyser and sit
directly in Add-ons/Commands, as the .json finds the script by that path.
README.md has the setup steps; the docs folder explains the report and method.

Copyright (c) 2026 AndyGBass. MIT License, see LICENSE. That licence does not
cover vgmstream or the libraries that come with it.
"""

import argparse
import base64
import html
import json
import math
import os
import shutil
import stat
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

try:
    import numpy as np
except ImportError:
    sys.exit("ERROR: numpy is required. Install it with:  py -m pip install numpy")

__version__ = "1.0.0"

# The tool's one name: the menu entry (in the .json), the report and log headings, and its folders
TOOL_NAME = "WwiseConversionAnalyser"
SCRIPT_DIR = Path(__file__).resolve().parent
# One report page, written over on every run, in the temp folder to keep it out of source control
REPORT_PATH = Path(tempfile.gettempdir()) / TOOL_NAME / f"{TOOL_NAME}_Report.html"
# Longest one file's decode may take, so a stuck decoder cannot hang the command
DECODE_TIMEOUT_SECONDS = 60

FREQ_MIN = 20.0
FREQ_TICKS = (20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000)
DB_MIN, DB_MAX = -100.0, 0.0

# Alignment: how far to look for the match, and how good and how distinct it must be to be trusted
MAX_LAG_SECONDS = 0.5
MIN_CORRELATION = 0.2
MIN_DISTINCTNESS = 5.0
# Beyond this the converted file's length is reported as wrong. Codecs pad by a few milliseconds.
LENGTH_TOLERANCE_SECONDS = 0.010

# Per-channel reports: audio used to tell which converted channel carries which original one
PAIRING_SECONDS = 10.0
# Mix fitting: least resemblance to an original channel to attempt it, smallest share kept (as a
# fraction of the largest), and the fraction of the converted channel's energy, from the bottom up, used
MIN_MIX_CORRELATION = 0.1
MIX_SHARE_FLOOR = 0.05
MIX_FIT_ENERGY = 0.9
# Above these, channels become rows of the summary table and spectrograms are made at half resolution
MAX_CHANNEL_COLUMNS = 6
MAX_SOUNDS_AT_FULL_RESOLUTION = 6
# Ambisonic channels by letter in Wwise's order (ACN), up to third order
AMBISONIC_LETTERS = "WYZXVTRSUQOMKLNP"
# Speakers in the order of a WAV channel mask's bits, which is the order of the channels
SPEAKERS = ("Front Left", "Front Right", "Centre", "LFE", "Back Left", "Back Right", "Front Left of Centre",
            "Front Right of Centre", "Back Centre", "Side Left", "Side Right", "Top Centre", "Top Front Left",
            "Top Front Centre", "Top Front Right", "Top Back Left", "Top Back Centre", "Top Back Right")

# Spectrogram cells at --scale 1, one per unit of the panel it is drawn in
BASE_COLUMNS = 1086
BASE_ROWS = 400
# Analysis window in samples (a power of two). At 48 kHz: 2.9 Hz resolution from 341 ms per column.
WINDOW_SIZE = 16384
# Windows transformed per numpy call, which bounds the memory used
BLOCK = 256
# Cells where both files are below this power count as unchanged
QUIET_FLOOR = 1e-11
# Past a cutoff, the converted file has "nothing left" once this many dB under the original
GONE_DB = 40.0

# Approximation of the "inferno" colour map, and a blue / grey / red map for level change
INFERNO_STOPS = ((0.0, (0, 0, 4)), (0.13, (31, 12, 72)), (0.25, (85, 15, 109)), (0.38, (136, 34, 106)),
                 (0.5, (186, 54, 85)), (0.63, (227, 89, 51)), (0.75, (249, 140, 10)), (0.88, (249, 201, 50)),
                 (1.0, (252, 255, 164)))
DIVERGING_STOPS = ((0.0, (158, 197, 244)), (0.25, (37, 106, 191)), (0.5, (56, 56, 53)), (0.75, (196, 58, 58)),
                   (1.0, (246, 172, 166)))


class AnalysisError(Exception):
    """A problem to report to the user without a traceback."""


def to_db(power: Optional[float]) -> Optional[float]:
    return None if power is None else 10 * math.log10(power + 1e-20)


# ---------------------------------------------------------------------------
# Reading audio
# ---------------------------------------------------------------------------

@dataclass
class WavFile:
    """A PCM file as read: every channel, and what the file says about itself."""
    frames: np.ndarray  # shaped (frames, channels), float32, full scale = 1.0
    sample_rate: int
    bits: int
    is_float: bool
    channel_mask: int  # which speaker each channel feeds; 0 = not stated
    clipped: List[int]  # per channel, samples in flat spots at full scale

    @property
    def channels(self) -> int:
        return self.frames.shape[1]

    @property
    def duration(self) -> float:
        return len(self.frames) / self.sample_rate


@dataclass
class Audio:
    """One signal to analyse: a single channel, or a mix of several."""
    samples: np.ndarray  # float64, full scale = 1.0
    sample_rate: int

    @property
    def duration(self) -> float:
        return len(self.samples) / self.sample_rate


def mono_mix(wav: WavFile) -> Audio:
    """Every channel of the file mixed to one, each at the same weight."""
    return Audio(wav.frames.mean(axis=1, dtype=np.float64), wav.sample_rate)


def one_channel(wav: WavFile, channel: int) -> Audio:
    return Audio(wav.frames[:, channel].astype(np.float64), wav.sample_rate)


def read_wav(path: str) -> WavFile:
    """Reads a PCM WAV file (8/16/24/32-bit integer or 32-bit float), keeping every channel."""
    data = Path(path).read_bytes()
    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise AnalysisError(f"Not a RIFF/WAVE file: {path}")

    fmt = body = None
    pos = 12
    while pos + 8 <= len(data):
        chunk_id = data[pos:pos + 4]
        size = struct.unpack_from("<I", data, pos + 4)[0]
        if chunk_id == b"fmt ":
            fmt = data[pos + 8:pos + 8 + size]
        elif chunk_id == b"data":
            body = data[pos + 8:pos + 8 + size]
        pos += 8 + size + (size % 2)
    if fmt is None or len(fmt) < 16 or body is None:
        raise AnalysisError(f"Missing fmt or data chunk: {path}")

    tag, channels, sample_rate, _, block_align, bits = struct.unpack_from("<HHIIHH", fmt)
    is_float = tag == 3
    channel_mask = 0
    if tag == 0xFFFE and len(fmt) >= 40:
        # WAVE_FORMAT_EXTENSIBLE: channel mask, then a GUID starting with the real format tag
        channel_mask, sub_tag = struct.unpack_from("<IH", fmt, 20)
        if sub_tag not in (1, 3):
            raise AnalysisError(f"Extensible sub-format 0x{sub_tag:04X} is not PCM: {path}")
        is_float = sub_tag == 3
    elif tag not in (1, 3, 0xFFFE):
        raise AnalysisError(f"Codec 0x{tag:04X} is not PCM and needs decoding first: {path}")

    width = block_align // channels if channels else 0
    if channels < 1 or sample_rate < 1 or width not in (1, 2, 3, 4) or (is_float and width != 4):
        raise AnalysisError(f"Unsupported sample layout: {path}")
    body = body[:len(body) // (width * channels) * (width * channels)]

    # Held as 32-bit floats: exact for 24-bit audio, at half the memory of 64-bit
    if width == 1:
        samples, scale = np.frombuffer(body, np.uint8).astype(np.float32) - 128, 1 / 128
    elif width == 3:
        # Into the top three bytes of a 32-bit integer, which keeps the sign
        wide = np.zeros((len(body) // 3, 4), np.uint8)
        wide[:, 1:] = np.frombuffer(body, np.uint8).reshape(-1, 3)
        samples, scale = wide.view("<i4").ravel().astype(np.float32), 1 / 2147483648
    else:
        samples = np.frombuffer(body, "<f4" if is_float else ("<i2" if width == 2 else "<i4")).astype(np.float32)
        scale = 1.0 if is_float else (1 / 32768 if width == 2 else 1 / 2147483648)
    samples *= np.float32(scale)  # a power of two, so nothing is rounded
    frames = samples.reshape(-1, channels)
    top = 1.0 if is_float else 1.0 - 2.0 ** (1 - 8 * width)
    return WavFile(frames, sample_rate, bits, is_float, channel_mask,
                   clipped=[count_clipped(frames[:, channel], top) for channel in range(channels)])


def count_clipped(samples: np.ndarray, top: float) -> int:
    """Samples of one channel in flat spots at full scale: two or more in a
    row at the format's highest value (`top`) or its lowest. One alone is
    just a peak that reaches full scale."""
    at_limit = (samples >= top) | (samples <= -1.0)
    if not at_limit.any():
        return 0
    with_next = at_limit[1:] & at_limit[:-1]  # this sample and the one after it
    in_flat_spot = np.zeros_like(at_limit)
    in_flat_spot[1:] |= with_next
    in_flat_spot[:-1] |= with_next
    return int(np.count_nonzero(in_flat_spot))


Details = List[Tuple[str, str]]


def file_details(path: str, wav: WavFile, codec: str, bit_rate: str) -> Details:
    """What the report lists about a file, as (label, value) lines. `codec` and
    `bit_rate` are the decoder's words for a decoded file, empty for PCM."""
    channels = {1: "mono", 2: "stereo"}.get(wav.channels, f"{wav.channels} channels")
    return [("", path),
            ("Codec", codec or ("PCM float" if wav.is_float else "PCM")),
            ("Bit-Rate", bit_rate or f"{wav.bits}-bit"),
            ("Sample Rate", f"{wav.sample_rate / 1000:g} kHz"),
            ("Channels", channels),
            ("Length", f"{wav.duration:.3f}s"),
            ("Size", f"{os.path.getsize(path):,} bytes")]


def level_summary(samples: np.ndarray) -> Tuple[float, float]:
    """Peak and RMS in dBFS."""
    return (20 * math.log10(float(np.max(np.abs(samples))) + 1e-20),
            10 * math.log10(float(np.mean(samples * samples)) + 1e-20))


# ---------------------------------------------------------------------------
# Trim and alignment
# ---------------------------------------------------------------------------

@dataclass
class Pair:
    """An original signal and its converted copy: same range, lined up, same sample rate."""
    original: Audio
    converted: Audio
    time_offset: float  # start of the analysed range in the original file, in seconds
    duration: float  # length of the analysed range, in seconds
    facts: Dict[str, str] = field(default_factory=dict)  # diagnostics for --verbose, in plain ASCII
    warnings: List[str] = field(default_factory=list)


def resample(samples: np.ndarray, rate: int, new_rate: int) -> np.ndarray:
    """Resamples by resizing the spectrum, which leaves everything below the
    lower Nyquist frequency as it was."""
    step = math.gcd(rate, new_rate)
    per_in, per_out = rate // step, new_rate // step  # per_in samples in last as long as per_out out
    # Padded with silence, as an FFT treats audio as a loop, up to a length that converts to a whole
    # number of samples and that an FFT handles quickly. Lengths are in groups of per_in samples.
    lead = -(-2048 // per_in)
    groups = -(-len(samples) // per_in) + 2 * lead
    round_to = 1 << max(0, groups.bit_length() - 5)
    groups = -(-groups // round_to) * round_to
    padded = np.zeros(groups * per_in)
    padded[lead * per_in:lead * per_in + len(samples)] = samples
    spectrum = np.fft.rfft(padded)
    resized = np.zeros(groups * per_out // 2 + 1, dtype=complex)
    kept = min(len(spectrum), len(resized))
    resized[:kept] = spectrum[:kept]
    out = np.fft.irfft(resized, groups * per_out) * (per_out / per_in)
    return out[lead * per_out:lead * per_out + round(len(samples) * new_rate / rate)]


def find_lag(original: np.ndarray, converted: np.ndarray, max_lag_limit: int) -> Optional[Tuple[int, float, float]]:
    """How many samples the converted file is late by (negative = early), from
    cross-correlating the middle of the original against it. Returns (lag,
    correlation, distinctness: how far the best match stands above the other
    offsets, in standard deviations), or None if the files are too short."""
    n = min(len(original), len(converted))
    if n < 4096:
        return None
    seg = 1024
    while seg * 2 <= n // 2 and seg < 131072:
        seg *= 2
    max_lag = min(max_lag_limit, (n - seg) // 2)
    if max_lag < 1:
        return None
    start = (n - seg) // 2
    span = seg + 2 * max_lag
    size = 1 << (span - 1).bit_length()
    count = 2 * max_lag + 1

    a = original[start:start + seg]
    b = converted[start - max_lag:start - max_lag + span]
    # Every offset in one pass: r[k] = sum(a[i] * b[i + k]) = IFFT(conj(A) * B)
    raw = np.fft.irfft(np.conj(np.fft.rfft(a, size)) * np.fft.rfft(b, size), size)[:count]
    b_energy = np.concatenate(([0.0], np.cumsum(b * b)))
    r = raw / np.sqrt(np.dot(a, a) * (b_energy[seg:seg + count] - b_energy[:count]) + 1e-30)

    # Of several near-equal peaks (a repeating sound), take the one closest to no shift
    best = r.max()
    near_best = np.flatnonzero(r >= best - 0.02 * abs(best))
    left = np.concatenate(([-np.inf], r[:-1]))[near_best]
    right = np.concatenate((r[1:], [-np.inf]))[near_best]
    peaks = near_best[(r[near_best] >= left) & (r[near_best] >= right)]
    best_k = int(peaks[np.argmin(np.abs(peaks - max_lag))])
    std = float(r.std())
    return best_k - max_lag, float(r[best_k]), (float(r[best_k] - r.mean()) / std if std > 0 else 0.0)


def prepare(ao: Audio, ac: Audio, trim_begin: float, trim_end: float) -> Pair:
    """Cuts the original to the trim range (seconds; negative = not set), lines
    the converted copy up against it and crops both to the range they share.
    Both are changed in place."""
    if len(ao.samples) == 0:
        raise AnalysisError("The original file contains no audio.")
    if len(ac.samples) == 0:
        raise AnalysisError("The converted file contains no audio.")

    facts: Dict[str, str] = {}
    warnings: List[str] = []
    full_duration = ao.duration
    converted_duration = ac.duration
    if ac.sample_rate != ao.sample_rate:
        # Compared at the original's rate, so both are measured over the same windows
        facts["Resampling"] = f"Converted file brought from {ac.sample_rate} to {ao.sample_rate} Hz for the comparison"
        ac.samples = resample(ac.samples, ac.sample_rate, ao.sample_rate)
        ac.sample_rate = ao.sample_rate

    # 1. Cut the original to the range that was converted
    begin = 0.0 if not trim_begin >= 0 else min(trim_begin, full_duration)
    end = full_duration if not trim_end >= 0 else min(trim_end, full_duration)
    if end <= begin:
        raise AnalysisError(f"The trim range is empty: {begin:.3f} to {end:.3f} s.")
    first = round(begin * ao.sample_rate)
    last = min(len(ao.samples), round(end * ao.sample_rate))
    trimmed = first > 0 or last < len(ao.samples)
    if trimmed:
        ao.samples = ao.samples[first:last]
    offset = first / ao.sample_rate
    trimmed_duration = ao.duration
    which = "trimmed original" if trimmed else "original"

    if trimmed:
        facts["Trim"] = (f"Original cut to {offset:.3f}-{offset + trimmed_duration:.3f} s "
                         f"({trimmed_duration:.3f} s, samples {first}-{last})")
    else:
        facts["Trim"] = "None; the whole original is used"

    # 2. The converted file should now be as long as the cut original. Warnings go in the report, so
    # they say only what to do; the figures behind them are in `facts`.
    mismatch = converted_duration - trimmed_duration
    facts["Length"] = f"Converted is {mismatch * 1000:+.1f} ms against the {which}"
    if abs(mismatch) > LENGTH_TOLERANCE_SECONDS:
        warnings.append("The converted file may be out of date: it does not fit the Sound as it is set up now. "
                        "Convert the Sound again in Wwise, then run this again.")

    # 3. Line the two up sample for sample
    pair = Pair(original=ao, converted=ac, time_offset=offset, duration=0.0, facts=facts, warnings=warnings)
    found = find_lag(ao.samples, ac.samples, int(MAX_LAG_SECONDS * ao.sample_rate))
    if found is None:
        facts["Alignment"] = "Skipped; the files are too short"
        warnings.append("The files are too short to check that they match; they are compared as they are.")
    else:
        lag, correlation, distinctness = found
        where = "in step" if lag == 0 else (f"{abs(lag)} samples ({abs(lag) * 1000 / ao.sample_rate:.2f} ms) "
                                            f"{'late' if lag > 0 else 'early'}")
        facts["Alignment"] = (f"Best match with the converted file {where} "
                              f"(correlation {correlation:.3f}, distinctness {distinctness:.1f})")
        if correlation < MIN_CORRELATION:
            facts["Alignment"] += "; too weak a match, so no shift was applied"
            warnings.append("The converted file does not match the original, so this may be a comparison between "
                            "different parts of the sound. Convert the Sound again in Wwise, then run this again.")
        else:
            if distinctness < MIN_DISTINCTNESS:
                # A steady or repeating sound lines up at many offsets. Not a fault, so not a warning.
                facts["Alignment"] += "; not distinct, so it relies on the trim values"
            if lag > 0:
                ac.samples = ac.samples[lag:]
            elif lag < 0:
                ao.samples = ao.samples[-lag:]
                pair.time_offset += -lag / ao.sample_rate

    # 4. Keep only the range both files cover
    shared = min(ao.duration, ac.duration)
    ao.samples = ao.samples[:round(shared * ao.sample_rate)]
    ac.samples = ac.samples[:round(shared * ac.sample_rate)]
    if len(ao.samples) == 0 or len(ac.samples) == 0:
        raise AnalysisError("Nothing is left to compare after trimming and alignment.")
    pair.duration = shared
    facts["Analysed range"] = (f"{pair.time_offset:.3f}-{pair.time_offset + shared:.3f} s of the original "
                               f"({shared:.3f} s, {len(ao.samples)} samples)")
    return pair


def pair_channels(original: WavFile, converted: WavFile, trim_begin: float) -> Optional[List[int]]:
    """For each channel of the original, the converted channel that carries
    it; None if that cannot be told. Needs equal channel counts.

    Paired by content, not position, as a conversion may reorder channels
    (Wwise does with ambisonics). Of equal matches, a channel keeps its place."""
    count = original.channels
    original_parts, converted_parts = matching_stretches(original, converted, trim_begin)
    matches = []
    for k in range(count):
        for j in range(count):
            found = find_lag(original_parts[k], converted_parts[j], int(MAX_LAG_SECONDS * original.sample_rate))
            if found is None:  # too short to tell
                return None
            matches.append((round(abs(found[1]), 3), k == j, k, j))

    partners: List[Optional[int]] = [None] * count
    for correlation, _, k, j in sorted(matches, reverse=True):  # best first
        if correlation < MIN_CORRELATION:
            break
        if partners[k] is None and j not in partners:
            partners[k] = j
    return None if None in partners else partners


def matching_stretches(original: WavFile, converted: WavFile, trim_begin: float) -> Tuple[List[np.ndarray], List[np.ndarray]]:
    """The same stretch from the middle of every channel of each file, the
    converted ones brought to the original's sample rate."""
    seconds = min(PAIRING_SECONDS, converted.duration)
    start = (converted.duration - seconds) / 2  # the converted file begins where the trim range does

    def stretch(wav: WavFile, begin: float, channel: int) -> np.ndarray:
        first = round(begin * wav.sample_rate)
        return wav.frames[first:first + round(seconds * wav.sample_rate), channel].astype(np.float64)

    converted_parts = [stretch(converted, start, j) for j in range(converted.channels)]
    if converted.sample_rate != original.sample_rate:
        converted_parts = [resample(part, converted.sample_rate, original.sample_rate) for part in converted_parts]
    return [stretch(original, max(0.0, trim_begin) + start, k) for k in range(original.channels)], converted_parts


def find_mixes(original: WavFile, converted: WavFile, trim_begin: float) -> Optional[List[List[float]]]:
    """For a changed channel count: mixes[j][k] is how much of original
    channel k is in converted channel j (1 = all, 0 = none); None if that
    cannot be told. Found from the audio, by fitting each converted channel
    as a weighted sum of the original's channels."""
    original_parts, converted_parts = matching_stretches(original, converted, trim_begin)
    mixes = []
    for part in converted_parts:
        # Lined up on whichever original channel it resembles most
        found = [find_lag(original_part, part, int(MAX_LAG_SECONDS * original.sample_rate)) for original_part in original_parts]
        if None in found:  # too short to tell
            return None
        lag, correlation, _ = max(found, key=lambda match: abs(match[1]))
        if abs(correlation) < MIN_MIX_CORRELATION:
            return None
        sources = [p[max(0, -lag):] for p in original_parts]
        target = part[max(0, lag):]
        length = min(len(target), min(len(source) for source in sources))
        # Fitted only where the converted channel has most of its energy: audio the conversion took
        # off the top would otherwise make every share come out too small.
        frequencies = np.fft.rfftfreq(length, 1 / original.sample_rate)
        target_spectrum = np.fft.rfft(target[:length])
        energy = np.cumsum(np.where(frequencies >= FREQ_MIN, np.abs(target_spectrum) ** 2, 0.0))
        if energy[-1] <= 0:
            return None
        edge = frequencies[min(len(frequencies) - 1, int(np.searchsorted(energy, MIX_FIT_ENERGY * energy[-1])))]
        band = (frequencies >= FREQ_MIN) & (frequencies <= edge)

        def in_band(spectrum: np.ndarray) -> np.ndarray:
            return np.concatenate((spectrum[band].real, spectrum[band].imag))

        matrix = np.stack([in_band(np.fft.rfft(source[:length])) for source in sources], axis=1)
        wanted = in_band(target_spectrum)
        gains = np.linalg.lstsq(matrix, wanted, rcond=None)[0]
        fitted = matrix @ gains
        agreement = float(np.dot(fitted, wanted) / np.sqrt(np.dot(fitted, fitted) * np.dot(wanted, wanted) + 1e-30))
        if agreement < MIN_CORRELATION:
            return None
        # Shares small enough to be codec noise are left out
        largest = float(np.max(np.abs(gains)))
        mixes.append([float(g) if abs(g) >= MIX_SHARE_FLOOR * largest else 0.0 for g in gains])
    return mixes


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def hann_window(fft_size: int) -> np.ndarray:
    """Hann window, scaled so a full-scale sine reads 0 dBFS in the spectrum."""
    window = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(fft_size) / fft_size)
    return window * (2.0 / window.sum())


def window_powers(samples: np.ndarray, starts: np.ndarray, fft_size: int):
    """Yields (index of first window, power spectra) for the windows at `starts`, a block at a time."""
    window = hann_window(fft_size)
    offsets = np.arange(fft_size)
    for first in range(0, len(starts), BLOCK):
        spectra = np.fft.rfft(samples[starts[first:first + BLOCK, None] + offsets] * window, axis=1)
        yield first, spectra.real ** 2 + spectra.imag ** 2


def spectrogram(audio: Audio, columns: int, rows: int, duration: float, fft_size: int, f_max: float) -> np.ndarray:
    """Power per cell, shape (rows, columns). Row 0 is f_max on a log
    frequency axis; each column is one analysis window centred on it."""
    half = fft_size // 2
    bin_hz = audio.sample_rate / fft_size
    n = len(audio.samples)

    # Each row is the mean of the interpolated spectrum across its frequency span, which stays smooth
    # whether a row covers many FFT bins or a fraction of one. c_lo and c_hi are the span's edges in bins.
    y = np.arange(rows)
    c_hi = np.minimum(half, FREQ_MIN * (f_max / FREQ_MIN) ** (1 - y / rows) / bin_hz)
    c_lo = np.minimum(half, FREQ_MIN * (f_max / FREQ_MIN) ** (1 - (y + 1) / rows) / bin_hz)
    k_hi = np.minimum(half - 1, c_hi.astype(np.int64))
    k_lo = np.minimum(half - 1, c_lo.astype(np.int64))
    frac_hi, frac_lo = c_hi - k_hi, c_lo - k_lo
    above_nyquist = c_hi - c_lo < 1e-9
    width = np.maximum(c_hi - c_lo, 1e-9)

    # Padded so the windows at either end can hang over the file's edges
    padded = np.concatenate((np.zeros(half), audio.samples, np.zeros(fft_size)))
    centres = np.minimum(n, ((np.arange(columns) + 0.5) / columns * duration * audio.sample_rate).astype(np.int64))
    grid = np.empty((rows, columns))
    for first, power in window_powers(padded, centres, fft_size):
        # area[:, k] is the integral of the interpolated spectrum from bin 0 to bin k
        steps = (power[:, :-1] + power[:, 1:]) / 2
        area = np.concatenate((np.zeros((len(power), 1)), np.cumsum(steps, axis=1)), axis=1)
        slope_hi = power[:, k_hi + 1] - power[:, k_hi]
        slope_lo = power[:, k_lo + 1] - power[:, k_lo]
        upper = area[:, k_hi] + frac_hi * (2 * power[:, k_hi] + frac_hi * slope_hi) / 2
        lower = area[:, k_lo] + frac_lo * (2 * power[:, k_lo] + frac_lo * slope_lo) / 2
        values = (upper - lower) / width
        values[:, above_nyquist] = 0.0
        grid[:, first:first + len(power)] = values.T
    return grid


def average_spectrum(samples: np.ndarray, fft_size: int) -> np.ndarray:
    """Welch-averaged power spectrum (75% overlap); a short file is zero-padded."""
    if len(samples) < fft_size:
        samples = np.concatenate((samples, np.zeros(fft_size - len(samples))))
    starts = np.arange(0, len(samples) - fft_size + 1, fft_size // 4)
    total = np.zeros(fft_size // 2 + 1)
    for _, power in window_powers(samples, starts, fft_size):
        total += power.sum(axis=0)
    return total / len(starts)


class Spectrum:
    """A power spectrum with exact band averages of its linear interpolation."""

    def __init__(self, power: np.ndarray, bin_hz: float):
        self.power = power.tolist()
        self.bin_hz = bin_hz
        self.nyquist = (len(power) - 1) * bin_hz
        self._area = np.concatenate(([0.0], np.cumsum((power[:-1] + power[1:]) / 2))).tolist()

    def at(self, f: float) -> Optional[float]:
        c = f / self.bin_hz
        if c > len(self.power) - 1:
            return None
        k = min(len(self.power) - 2, int(c))
        return self.power[k] + (c - k) * (self.power[k + 1] - self.power[k])

    def _integral(self, c: float) -> float:
        k = min(len(self.power) - 2, int(c))
        frac = c - k
        p0, p1 = self.power[k], self.power[k + 1]
        return self._area[k] + frac * (p0 + (p0 + frac * (p1 - p0))) / 2

    def band_power(self, f_lo: float, f_hi: float) -> Optional[float]:
        if f_lo >= self.nyquist:
            return None
        c_lo, c_hi = f_lo / self.bin_hz, min(f_hi, self.nyquist) / self.bin_hz
        if c_hi - c_lo < 1e-9:
            return self.at(f_lo)
        return (self._integral(c_hi) - self._integral(c_lo)) / (c_hi - c_lo)


# A cutoff at one end of the spectrum: (frequency 3 dB down, frequency where nothing is left), None if absent
Cutoff = Tuple[Optional[float], Optional[float]]


def find_cutoffs(freqs: List[float], difference: List[Optional[float]], unsmoothed: List[Optional[float]],
                 original_db: List[Optional[float]]) -> Tuple[Cutoff, Cutoff]:
    """(low, high): the cutoffs at the bottom and top of the spectrum.

    The 3 dB point is judged on the smoothed difference against its median,
    so a level change is not taken for a cutoff. "Nothing left" is read off
    the unsmoothed difference, as smoothing carries a steep edge outward.
    Frequencies where the original itself has nothing are left out."""
    floor_db = 10 * math.log10(QUIET_FLOOR)
    usable = [i for i, (d, u, o) in enumerate(zip(difference, unsmoothed, original_db))
              if d is not None and u is not None and o is not None and o > floor_db]
    if len(usable) < 3:
        return (None, None), (None, None)
    typical = statistics.median(difference[i] for i in usable)

    def edge(values: List[Optional[float]], limit: float, indices: List[int]) -> Optional[float]:
        """Walks inward from one end to where the values come back above the limit."""
        if values[indices[0]] >= limit:
            return None
        for outer, inner in zip(indices, indices[1:]):
            if values[inner] >= limit:
                t = (limit - values[outer]) / (values[inner] - values[outer])
                return freqs[outer] * (freqs[inner] / freqs[outer]) ** t
        return None

    def cutoff(indices: List[int]) -> Cutoff:
        down_3db = edge(difference, typical - 3, indices)
        return down_3db, (None if down_3db is None else edge(unsmoothed, typical - GONE_DB, indices))

    return cutoff(usable), cutoff(usable[::-1])


@dataclass
class Analysis:
    window_size: int
    f_max: float
    columns: int
    rows: int
    # Spectrogram grids, set to None once the images have been made from them
    grid_original: Optional[np.ndarray]  # power, shape (rows, columns), row 0 = f_max
    grid_converted: Optional[np.ndarray]
    grid_difference: Optional[np.ndarray]  # dB, converted minus original
    curve_f: List[float]
    curve_original: List[Optional[float]]
    curve_converted: List[Optional[float]]
    curve_difference: List[Optional[float]]
    low_cutoff: Cutoff  # at the bottom of the spectrum
    cutoff: Cutoff  # at the top
    band_hi: float  # top of the band both files fully cover, in Hz
    band_difference: float  # level change across that band in dB, for --verbose
    levels_original: Tuple[float, float]  # peak and RMS in dBFS
    levels_converted: Tuple[float, float]


def analyse(pair: Pair, window_size: int, scale: int) -> Analysis:
    ao, ac = pair.original, pair.converted  # at the same sample rate
    f_max = ao.sample_rate / 2
    columns, rows = BASE_COLUMNS * scale, BASE_ROWS * scale
    grid_o = spectrogram(ao, columns, rows, pair.duration, window_size, f_max)
    grid_c = spectrogram(ac, columns, rows, pair.duration, window_size, f_max)

    spectrum_o = Spectrum(average_spectrum(ao.samples, window_size), ao.sample_rate / window_size)
    spectrum_c = Spectrum(average_spectrum(ac.samples, window_size), ac.sample_rate / window_size)

    # Spectrum curves: one value per point for the levels, 1/12-octave smoothing for the difference
    points = 544
    ratio = f_max / FREQ_MIN
    point_half = ratio ** (0.5 / points)
    smooth = 2 ** (1 / 24)
    curve_f, curve_o, curve_c, curve_d, smooth_original = [], [], [], [], []
    for i in range(points):
        f = FREQ_MIN * ratio ** ((i + 0.5) / points)
        curve_f.append(f)
        curve_o.append(to_db(spectrum_o.band_power(f / point_half, f * point_half)))
        curve_c.append(to_db(spectrum_c.band_power(f / point_half, f * point_half)))
        smooth_o = to_db(spectrum_o.band_power(f / smooth, f * smooth))
        smooth_c = to_db(spectrum_c.band_power(f / smooth, f * smooth))
        smooth_original.append(smooth_o)
        curve_d.append(None if smooth_o is None or smooth_c is None else smooth_c - smooth_o)

    unsmoothed = [None if o is None or c is None else c - o for o, c in zip(curve_o, curve_c)]
    low_cutoff, cutoff = find_cutoffs(curve_f, curve_d, unsmoothed, smooth_original)
    band_hi = f_max if cutoff[0] is None else 0.9 * cutoff[0]
    if band_hi >= 2000:
        band_hi = 1000 * math.floor(band_hi / 1000)
    band_difference = to_db(spectrum_c.band_power(FREQ_MIN, band_hi)) - to_db(spectrum_o.band_power(FREQ_MIN, band_hi))

    # Level change per cell
    grid_d = 10 * np.log10((grid_c + QUIET_FLOOR) / (grid_o + QUIET_FLOOR))

    return Analysis(window_size=window_size, f_max=f_max, columns=columns, rows=rows,
                    grid_original=grid_o, grid_converted=grid_c, grid_difference=grid_d,
                    curve_f=curve_f, curve_original=curve_o, curve_converted=curve_c, curve_difference=curve_d,
                    low_cutoff=low_cutoff, cutoff=cutoff, band_hi=band_hi, band_difference=band_difference,
                    levels_original=level_summary(ao.samples), levels_converted=level_summary(ac.samples))


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

def build_lut(stops) -> List[Tuple[int, int, int]]:
    lut = []
    for i in range(256):
        t = i / 255
        s = 0
        while s < len(stops) - 2 and t > stops[s + 1][0]:
            s += 1
        (t0, c0), (t1, c1) = stops[s], stops[s + 1]
        lut.append(tuple(round(c0[c] + (c1[c] - c0[c]) * (t - t0) / (t1 - t0)) for c in range(3)))
    return lut


INFERNO_LUT = build_lut(INFERNO_STOPS)
DIVERGING_LUT = build_lut(DIVERGING_STOPS)


def png_data_uri(indices: np.ndarray, lut: List[Tuple[int, int, int]]) -> str:
    """A heat map as an embeddable RGB PNG; `indices` is a colour-map index per cell, (rows, columns)."""
    rows, columns = indices.shape
    raw = np.zeros((rows, 1 + 3 * columns), np.uint8)  # each row starts with PNG filter type 0 (none)
    raw[:, 1:] = np.asarray(lut, np.uint8)[indices].reshape(rows, 3 * columns)

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", columns, rows, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw.tobytes(), 6))
           + chunk(b"IEND", b""))
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def level_indices(grid: np.ndarray) -> np.ndarray:
    level = (10 * np.log10(grid + 1e-20) - DB_MIN) / (DB_MAX - DB_MIN)
    return np.rint(255 * np.clip(level, 0, 1)).astype(np.uint8)


def difference_indices(grid: np.ndarray, range_db: float) -> np.ndarray:
    return np.rint((np.clip(grid / range_db, -1, 1) * 0.5 + 0.5) * 255).astype(np.uint8)


def render_images(analysis: Analysis, range_db: float) -> Dict[str, str]:
    """The three spectrogram images, ready to embed in the page."""
    return {
        "original": png_data_uri(level_indices(analysis.grid_original), INFERNO_LUT),
        "converted": png_data_uri(level_indices(analysis.grid_converted), INFERNO_LUT),
        "difference": png_data_uri(difference_indices(analysis.grid_difference, range_db), DIVERGING_LUT),
    }


# ---------------------------------------------------------------------------
# Web page
# ---------------------------------------------------------------------------

PAGE_STYLE = """
:root {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --plot: #121216;
  --text-primary: #ffffff; --text-secondary: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --border: rgba(255, 255, 255, 0.10);
  --original: #3987e5; --converted: #d95926; --warning: #fab219;
}
* { box-sizing: border-box; }
body { margin: 0; padding: 28px 20px 48px; background: var(--page); color: var(--text-primary);
       font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1240px; margin: 0 auto; }
h1 { margin: 0; font-size: 26px; font-weight: 650; }
h2 { margin: 0 0 2px; font-size: 16px; font-weight: 600; }
p { margin: 0; }
.sub { color: var(--muted); }
section { background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
          padding: 18px 20px 14px; margin-top: 16px; }
.warning { border-color: var(--warning); }
.warning h2::before { content: "\\26A0\\FE0E  "; color: var(--warning); }
.warning li { color: var(--text-secondary); }
ul { margin: 6px 0 0; padding-left: 20px; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 6px 12px 6px 0; border-bottom: 1px solid var(--grid); vertical-align: top; }
tr:last-child > * { border-bottom: 0; }
th { color: var(--muted); font-weight: 400; white-space: nowrap; width: 1%; }
td { color: var(--text-secondary); overflow-wrap: anywhere; }
/* Two files side by side: equal columns, so the same line of each sits level */
table.compare { table-layout: fixed; }
table.compare th { width: 50%; }
/* A table that follows another in the same section is set apart by a gap and a rule */
table + table { margin-top: 14px; border-top: 1px solid var(--grid); }
/* Per-channel reports: one column (or, for many channels, one row) per channel, headed by its name */
table.channels tr:first-child th { color: var(--text-primary); font-weight: 600; white-space: normal; width: auto; }
table.channels th:first-child { width: 210px; white-space: normal; }
/* Tabs: one per Sound when a report covers several, and one per channel in a per-channel report. A row of
   tabs stays at the top of the window while what it shows is scrolled; a Sound's channel tabs sit under
   the row of Sound tabs, whose height the page's script keeps in --sound-tabs. */
.tabs { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px; padding: 10px 0; position: sticky; top: 0;
        z-index: 3; background: var(--page); }
.sound > .tabs { top: var(--sound-tabs, 0px); z-index: 2; }
.tabs button { font: inherit; color: var(--text-secondary); background: var(--surface); cursor: pointer;
               border: 1px solid var(--border); border-radius: 8px; padding: 7px 14px; }
.tabs button.on { color: var(--text-primary); border-color: var(--original); }
.tabs ~ .view > section:first-child, .tabs ~ .sound > section:first-child { margin-top: 0; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 22px; color: var(--text-secondary); margin: 6px 0 2px; }
.key { display: inline-block; width: 18px; height: 2px; vertical-align: middle; margin-right: 8px; border-radius: 1px; }
/* Sticks to the bottom of the window while its section is taller than the window, so it stays in view */
.readout { min-height: 21px; color: var(--text-secondary); font-variant-numeric: tabular-nums; margin-top: 2px;
           position: sticky; bottom: 0; padding: 6px 0; background: var(--surface); }
svg.figure { display: block; width: 100%; height: auto; margin-top: 6px; font-size: 12px; }
svg text { fill: var(--text-secondary); }
svg .muted { fill: var(--muted); }
svg .grid { stroke: var(--grid); }
svg .axis { stroke: var(--axis); }
svg .frame { fill: none; stroke: var(--axis); }
svg .plot { fill: var(--plot); }
svg .hit { fill: transparent; pointer-events: all; cursor: crosshair; }
svg .cross { stroke: rgba(255, 255, 255, 0.6); pointer-events: none; visibility: hidden; }
svg .line { fill: none; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
footer { color: var(--muted); margin-top: 18px; }
"""

PAGE_SCRIPT = """
(() => {
  const luts = JSON.parse(document.getElementById('luts').textContent);
  const hz = f => f < 1000 ? f.toFixed(0) + ' Hz' : (f / 1000).toFixed(2) + ' kHz';
  const num = (v, signed) => (v < 0 ? '\\u2212' : signed && v > 0 ? '+' : '') + Math.abs(v).toFixed(1);
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  // A report has one view, or one per channel. Each has its own figures, hover data and readout lines.
  const setUp = root => {
    const curves = JSON.parse(root.querySelector('script.curves').textContent);
    // Heat-map panels by figure kind, so one figure's readout can use another figure's image
    const byKind = {};
    const valueAt = (panel, u, v) => {
      if (!panel || !panel.pixels) return null;
      const p = panel.pixels;
      const x = clamp(Math.floor(u * p.width), 0, p.width - 1), y = clamp(Math.floor(v * p.height), 0, p.height - 1);
      const i = (y * p.width + x) * 4;
      const index = panel.reverse.get(p.data[i] << 16 | p.data[i + 1] << 8 | p.data[i + 2]);
      return index === undefined ? null : panel.vmin + index / 255 * (panel.vmax - panel.vmin);
    };
    // Level change under the pointer, read from the level-change image. Where that image is at the end of its
    // colour scale, the two levels are subtracted instead, unless either is off the bottom of its own scale.
    const changeAt = (u, v) => {
      const panel = (byKind.change || [])[0], levels = byKind.levels || [];
      const value = valueAt(panel, u, v);
      if (value === null) return null;
      if (Math.abs(value) < panel.vmax - 0.05) return num(value, true) + ' dB';
      const original = valueAt(levels[0], u, v), converted = valueAt(levels[1], u, v);
      if (original !== null && converted !== null && Math.min(original, converted) > levels[0].vmin + 0.5)
        return num(converted - original, true) + ' dB';
      return (value > 0 ? '\\u2265 ' : '\\u2264 ') + num(value, true) + ' dB';
    };

    root.querySelectorAll('svg.figure').forEach(svg => {
      const d = svg.dataset;
      // A figure without a readout line of its own uses the one in its section
      const readout = document.getElementById(d.readout) || svg.closest('section').querySelector('.readout');
      const idle = readout ? readout.textContent : '';
      const crossV = svg.querySelector('.cross.v'), crossH = svg.querySelector('.cross.h');
      const left = +d.left, width = +d.width, fMin = +d.fmin, fMax = +d.fmax;

      // Heat maps: read each embedded image back, so hovering can report the value under the pointer
      const reverse = new Map();
      (luts[d.lut] || []).forEach((c, i) => { const key = c[0] << 16 | c[1] << 8 | c[2]; if (!reverse.has(key)) reverse.set(key, i); });
      const panels = [...svg.querySelectorAll('image')].map(node => {
        const panel = { pixels: null, reverse, vmin: +d.vmin, vmax: +d.vmax };
        const img = new Image();
        img.onload = () => {
          const canvas = document.createElement('canvas');
          canvas.width = img.naturalWidth; canvas.height = img.naturalHeight;
          const ctx = canvas.getContext('2d', { willReadFrequently: true });
          ctx.drawImage(img, 0, 0);
          panel.pixels = ctx.getImageData(0, 0, canvas.width, canvas.height);
        };
        img.src = node.getAttribute('href');
        return panel;
      });
      byKind[d.kind] = panels;
      // With nowhere to show values there is nothing to set up, but the other figures must still get theirs
      if (!readout) return;

      svg.querySelectorAll('.hit').forEach(hit => {
        const top = +hit.getAttribute('y'), height = +hit.getAttribute('height');
        hit.addEventListener('pointermove', event => {
          const p = new DOMPoint(event.clientX, event.clientY).matrixTransform(svg.getScreenCTM().inverse());
          const u = clamp((p.x - left) / width, 0, 1), v = clamp((p.y - top) / height, 0, 1);
          crossV.setAttribute('x1', p.x); crossV.setAttribute('x2', p.x); crossV.style.visibility = 'visible';
          let text;
          if (d.kind === 'spectrum') {
            const i = clamp(Math.floor(u * curves.f.length), 0, curves.f.length - 1);
            const o = curves.o[i], c = curves.c[i], diff = curves.d[i];
            text = hz(curves.f[i]) + '  \\u00b7  original ' + (o === null ? 'n/a' : num(o) + ' dBFS')
              + '  \\u00b7  converted ' + (c === null ? 'n/a' : num(c) + ' dBFS')
              + '  \\u00b7  difference ' + (diff === null ? 'n/a' : num(diff, true) + ' dB');
          } else {
            crossH.setAttribute('y1', p.y); crossH.setAttribute('y2', p.y); crossH.style.visibility = 'visible';
            const t = +d.tmin + u * (+d.tmax - +d.tmin);
            text = t.toFixed(3) + ' s  \\u00b7  ' + hz(fMin * Math.pow(fMax / fMin, 1 - v));
            // Every spectrogram reports the same things, whichever one the pointer is over
            ['original', 'converted'].forEach((name, n) => {
              const panel = (byKind.levels || [])[n], value = valueAt(panel, u, v);
              // A level at the bottom of the colour scale may really be lower still
              if (value !== null) text += '  \\u00b7  ' + name + ' ' + (value <= panel.vmin + 0.05 ? '\\u2264 ' : '') + num(value) + ' dBFS';
            });
            const change = changeAt(u, v);
            if (change !== null) text += '  \\u00b7  difference ' + change;
          }
          readout.textContent = text;
        });
        hit.addEventListener('pointerleave', () => {
          crossV.style.visibility = 'hidden';
          if (crossH) crossH.style.visibility = 'hidden';
          readout.textContent = idle;
        });
      });
    });
  };

  // A view is set up when it is first shown, so a report with many Sounds or channels still opens quickly
  const setUpShown = () => document.querySelectorAll('.view').forEach(view => {
    if (!view.dataset.ready && view.getClientRects().length) { view.dataset.ready = '1'; setUp(view); }
  });
  // A row of tabs shows one of the panels beside it: a Sound of the report, or a channel of a Sound
  document.querySelectorAll('.tabs').forEach(row => {
    const tabs = [...row.children];
    const panels = [...row.parentElement.children].filter(panel => panel.classList.contains(row.dataset.panels));
    const show = key => {
      panels.forEach(panel => { panel.hidden = panel.dataset.key !== key; });
      tabs.forEach(tab => tab.classList.toggle('on', tab.dataset.key === key));
    };
    tabs.forEach(tab => tab.addEventListener('click', () => { show(tab.dataset.key); setUpShown(); }));
    show(tabs[0].dataset.key);
  });
  setUpShown();  // once every row of tabs has hidden what it does not show
  // The channel tabs of a Sound stay just under the row of Sound tabs, however many lines that takes
  const soundTabs = document.querySelector('main > .tabs');
  const place = () => document.documentElement.style.setProperty('--sound-tabs', (soundTabs ? soundTabs.offsetHeight : 0) + 'px');
  place();
  window.addEventListener('resize', place);
})();
"""

PLOT_LEFT = 64
# Shared by the spectrum chart and the spectrograms, so they line up; images are made at the shape shown
PLOT_WIDTH = BASE_COLUMNS
PANEL_HEIGHT = BASE_ROWS
SPECTRUM_HEIGHT = 300  # the spectrum chart's level panel
# Leaves room to the right of the plot for a colour bar
FIGURE_WIDTH = PLOT_LEFT + PLOT_WIDTH + 110


def esc(text) -> str:
    return html.escape(str(text))


def minus(value: float, digits: int = 1, signed: bool = False) -> str:
    """Formats a number with a real minus sign."""
    text = f"{abs(value):.{digits}f}"
    if float(text) == 0:
        return text
    return ("\u2212" if value < 0 else "+" if signed else "") + text


def db_tick(value: float, signed: bool = False) -> str:
    """Axis label for a dB value, without trailing zeros."""
    if value == 0:
        return "0"
    return ("−" if value < 0 else "+" if signed else "") + f"{abs(value):g}"


def hz_label(f: float) -> str:
    return f"{f / 1000:g} kHz" if f >= 1000 else f"{f:g} Hz"


def tick_label(f: float) -> str:
    return f"{f / 1000:g}k" if f >= 1000 else f"{f:g}"


def time_ticks(offset: float, duration: float) -> List[float]:
    """Round tick positions in original-file time, so a trimmed range reads as e.g. 1 to 4 s."""
    steps = (0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600)
    step = next((s for s in steps if duration / s <= 10), steps[-1])
    first = math.ceil(offset / step - 1e-6)
    return [i * step for i in range(first, int((offset + duration) / step + 1e-6) + 1)]


def gradient(name: str, stops) -> str:
    """Vertical colour-bar gradient, top = highest value."""
    body = "".join(f'<stop offset="{1 - t:.3f}" stop-color="rgb({c[0]},{c[1]},{c[2]})"/>' for t, c in reversed(stops))
    return f'<linearGradient id="{name}" x1="0" y1="0" x2="0" y2="1">{body}</linearGradient>'


def heat_figure(kind: str, panels: List[Tuple[str, str]], panel_height: int, pair: Pair, analysis: Analysis,
                bar_gradient: str, bar_stops, bar_labels: List[str], bar_title: str,
                value_range: Tuple[float, float], lut_name: str, readout_id: str) -> str:
    """Stacked heat maps, each with its own axes, sharing a colour bar."""
    width = PLOT_WIDTH
    top0, gap = 24, 78  # the gap holds a time axis and the next panel's caption
    tops = [top0 + i * (panel_height + gap) for i in range(len(panels))]
    bottom = tops[-1] + panel_height
    height = bottom + 52
    right = PLOT_LEFT + width
    ratio = analysis.f_max / FREQ_MIN
    out = [f'<svg class="figure" viewBox="0 0 {FIGURE_WIDTH} {height}" role="img" aria-label="{esc(bar_title)} heat map" '
           f'data-kind="{kind}" data-readout="{readout_id}" data-left="{PLOT_LEFT}" data-width="{width}" '
           f'data-fmin="{FREQ_MIN}" data-fmax="{analysis.f_max}" data-tmin="{pair.time_offset}" '
           f'data-tmax="{pair.time_offset + pair.duration}" data-vmin="{value_range[0]}" data-vmax="{value_range[1]}" '
           f'data-lut="{lut_name}"><defs>{gradient(bar_gradient, bar_stops)}</defs>']
    for (caption, uri), top in zip(panels, tops):
        out.append(f'<text x="{PLOT_LEFT}" y="{top - 8}">{esc(caption)}</text>')
        out.append(f'<rect class="plot" x="{PLOT_LEFT}" y="{top}" width="{width}" height="{panel_height}"/>')
        out.append(f'<image href="{uri}" x="{PLOT_LEFT}" y="{top}" width="{width}" height="{panel_height}" preserveAspectRatio="none"/>')
        out.append(f'<rect class="frame" x="{PLOT_LEFT - 0.5}" y="{top - 0.5}" width="{width + 1}" height="{panel_height + 1}"/>')
        for f in FREQ_TICKS:
            if f > analysis.f_max:
                continue
            y = top + panel_height * (1 - math.log(f / FREQ_MIN) / math.log(ratio))
            out.append(f'<line class="axis" x1="{PLOT_LEFT - 6}" y1="{y:.1f}" x2="{PLOT_LEFT - 1}" y2="{y:.1f}"/>')
            out.append(f'<text x="{PLOT_LEFT - 10}" y="{y + 4:.1f}" text-anchor="end">{tick_label(f)}</text>')
        middle = top + panel_height / 2
        out.append(f'<text class="muted" transform="translate(14 {middle}) rotate(-90)" text-anchor="middle">Frequency (Hz)</text>')
        for t in time_ticks(pair.time_offset, pair.duration):
            x = PLOT_LEFT + (t - pair.time_offset) / pair.duration * width
            out.append(f'<line class="axis" x1="{x:.1f}" y1="{top + panel_height}" x2="{x:.1f}" y2="{top + panel_height + 5}"/>')
            out.append(f'<text x="{x:.1f}" y="{top + panel_height + 20}" text-anchor="middle">{t:g}</text>')
        out.append(f'<text class="muted" x="{PLOT_LEFT + width / 2}" y="{top + panel_height + 42}" text-anchor="middle">Time (s)</text>')

    bar_x, bar_height = right + 30, bottom - top0
    out.append(f'<rect x="{bar_x}" y="{top0}" width="16" height="{bar_height}" fill="url(#{bar_gradient})"/>')
    out.append(f'<rect class="frame" x="{bar_x - 0.5}" y="{top0 - 0.5}" width="17" height="{bar_height + 1}"/>')
    out.append(f'<text class="muted" x="{bar_x + 8}" y="{top0 - 8}" text-anchor="middle">{esc(bar_title)}</text>')
    for i, label in enumerate(bar_labels):
        y = top0 + bar_height * i / (len(bar_labels) - 1)
        out.append(f'<line class="axis" x1="{bar_x + 16}" y1="{y:.1f}" x2="{bar_x + 21}" y2="{y:.1f}"/>')
        out.append(f'<text x="{bar_x + 25}" y="{y + 4:.1f}">{label}</text>')

    out.append(f'<line class="cross v" y1="{top0}" y2="{bottom}"/><line class="cross h" x1="{PLOT_LEFT}" x2="{right}"/>')
    for top in tops:
        out.append(f'<rect class="hit" x="{PLOT_LEFT}" y="{top}" width="{width}" height="{panel_height}"/>')
    out.append("</svg>")
    return "".join(out)


def spectrum_figure(analysis: Analysis, readout_id: str, key: str) -> str:
    """Average spectrum of both files, with the smoothed difference in a panel
    below. `key` keeps the figure's ids unique in the page."""
    width, main_top, main_height, gap, diff_height, diff_range = PLOT_WIDTH, 24, SPECTRUM_HEIGHT, 46, 120, 3.0
    diff_top = main_top + main_height + gap
    main_bottom, diff_bottom = main_top + main_height, diff_top + diff_height
    height = diff_bottom + 52
    right = PLOT_LEFT + width
    ratio = analysis.f_max / FREQ_MIN

    levels = [v for v in analysis.curve_original + analysis.curve_converted if v is not None]
    db_top = 20 * math.ceil((max(levels) + 5) / 20)
    db_bottom = min(db_top - 60, max(db_top - 160, 20 * math.floor((min(levels) - 5) / 20)))

    def x_of(f: float) -> float:
        return PLOT_LEFT + width * math.log(f / FREQ_MIN) / math.log(ratio)

    def y_level(db: float) -> float:
        return main_top + (db_top - max(db, db_bottom - 20)) / (db_top - db_bottom) * main_height

    def y_diff(db: float) -> float:
        return diff_top + (diff_range - max(-3 * diff_range, min(3 * diff_range, db))) / (2 * diff_range) * diff_height

    def path(values, y_of) -> str:
        return " ".join(f"{x_of(f):.1f},{y_of(v):.1f}" for f, v in zip(analysis.curve_f, values) if v is not None)

    out = [f'<svg class="figure" viewBox="0 0 {FIGURE_WIDTH} {height}" role="img" aria-label="Average spectrum of the original '
           f'and converted files" data-kind="spectrum" data-readout="{readout_id}" data-left="{PLOT_LEFT}" '
           f'data-width="{width}" data-fmin="{FREQ_MIN}" data-fmax="{analysis.f_max}">'
           f'<defs><clipPath id="clip-main{key}"><rect x="{PLOT_LEFT}" y="{main_top}" width="{width}" height="{main_height}"/></clipPath>'
           f'<clipPath id="clip-diff{key}"><rect x="{PLOT_LEFT}" y="{diff_top}" width="{width}" height="{diff_height}"/></clipPath></defs>']
    out.append(f'<text class="muted" x="{PLOT_LEFT}" y="{diff_top - 8}">Converted minus original (dB), 1/12-octave smoothed</text>')
    for f in FREQ_TICKS:
        if f > analysis.f_max:
            continue
        x = x_of(f)
        if f > FREQ_MIN:
            out.append(f'<line class="grid" x1="{x:.1f}" y1="{main_top}" x2="{x:.1f}" y2="{main_bottom}"/>')
            out.append(f'<line class="grid" x1="{x:.1f}" y1="{diff_top}" x2="{x:.1f}" y2="{diff_bottom}"/>')
        out.append(f'<text x="{x:.1f}" y="{diff_bottom + 20}" text-anchor="middle">{tick_label(f)}</text>')
    db = db_top
    while db >= db_bottom:
        y = y_level(db)
        if db > db_bottom:
            out.append(f'<line class="grid" x1="{PLOT_LEFT}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}"/>')
        out.append(f'<text x="{PLOT_LEFT - 10}" y="{y + 4:.1f}" text-anchor="end">{minus(db, 0)}</text>')
        db -= 20
    for step in range(int(diff_range), -int(diff_range) - 1, -1):
        y = y_diff(step)
        if step > -diff_range:  # the bottom edge is the panel's baseline, drawn below
            out.append(f'<line class="{"axis" if step == 0 else "grid"}" x1="{PLOT_LEFT}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}"/>')
        out.append(f'<text x="{PLOT_LEFT - 10}" y="{y + 4:.1f}" text-anchor="end">{minus(step, 0, signed=True)}</text>')
    out.append(f'<line class="axis" x1="{PLOT_LEFT}" y1="{main_bottom}" x2="{right}" y2="{main_bottom}"/>')
    out.append(f'<line class="axis" x1="{PLOT_LEFT}" y1="{diff_bottom}" x2="{right}" y2="{diff_bottom}"/>')
    out.append(f'<text class="muted" x="{PLOT_LEFT + width / 2}" y="{diff_bottom + 42}" text-anchor="middle">Frequency (Hz)</text>')

    original = path(analysis.curve_original, y_level)
    known = [f for f, v in zip(analysis.curve_f, analysis.curve_original) if v is not None]
    out.append(f'<g clip-path="url(#clip-main{key})">'
               f'<polygon points="{original} {x_of(known[-1]):.1f},{main_bottom} {x_of(known[0]):.1f},{main_bottom}" '
               f'fill="var(--original)" fill-opacity="0.1"/>'
               f'<polyline class="line" points="{original}" stroke="var(--original)"/>'
               f'<polyline class="line" points="{path(analysis.curve_converted, y_level)}" stroke="var(--converted)"/></g>')
    out.append(f'<g clip-path="url(#clip-diff{key})"><polyline class="line" points="{path(analysis.curve_difference, y_diff)}" '
               f'stroke="var(--text-secondary)"/></g>')

    out.append(f'<line class="cross v" y1="{main_top}" y2="{diff_bottom}"/>')
    out.append(f'<rect class="hit" x="{PLOT_LEFT}" y="{main_top}" width="{width}" height="{main_height}"/>')
    out.append(f'<rect class="hit" x="{PLOT_LEFT}" y="{diff_top}" width="{width}" height="{diff_height}"/>')
    out.append("</svg>")
    return "".join(out)


# (label, value) rows; a value is text, or a list of (label, value) lines
Rows = List[Tuple[str, object]]


def row_lines(value: object) -> List[str]:
    """The lines a row's value is shown as."""
    if isinstance(value, str):
        return [value]
    return [f"{label}: {text}" if label else text for label, text in value]


def table(rows: Rows) -> str:
    return "<table>" + "".join(f"<tr><th>{esc(k)}</th><td>{'<br>'.join(esc(line) for line in row_lines(v))}</td></tr>"
                               for k, v in rows) + "</table>"


def side_by_side(rows: Rows) -> str:
    """The rows as columns headed by their labels, so the same line of each value sits level."""
    head = "".join(f"<th>{esc(label)}</th>" for label, _ in rows)
    body = "".join("<tr>" + "".join(f"<td>{esc(line)}</td>" for line in lines) + "</tr>"
                   for lines in zip(*(row_lines(value) for _, value in rows)))
    return f'<table class="compare"><tr>{head}</tr>{body}</table>'


def cutoff_label(cutoff: Cutoff, beyond: str) -> str:
    """A cutoff as shown, such as "4.5 kHz (nothing above 4.8 kHz)"; `beyond`
    is "above" or "below". Rounded to 100 Hz from 1 kHz, 10 Hz from 100 Hz, else 1 Hz."""
    def rounded(f: float) -> str:
        step = 100 if f >= 1000 else 10 if f >= 100 else 1
        return hz_label(round(f / step) * step)

    down_3db, gone = cutoff
    if down_3db is None:
        return "None"
    return rounded(down_3db) + ("" if gone is None else f" (nothing {beyond} {rounded(gone)})")


def clipping_label(original: int, converted: int, compact: bool = False) -> str:
    """Whether the converted audio clips, from counts of samples in flat spots
    at full scale. `compact` is the short wording for the channel table."""
    def count(samples: int) -> str:
        return "none" if samples == 0 else f"{samples:,} sample{'' if samples == 1 else 's'}"

    if compact:
        return ("None" if not converted else count(converted)) + (f" (original: {count(original)})" if original else "")
    if converted:
        return f"Converted file clips: {count(converted)} at full scale (original: {count(original)})"
    return "None" if not original else f"None in the converted file (original: {count(original)} at full scale)"


@dataclass
class View:
    """One comparison shown in the report: every channel mixed to mono, or one channel on its own."""
    name: str  # the channel's name; empty for a single view
    place: str  # where the channel sits in each file, or which original channels it was made from
    pair: Pair
    analysis: Analysis
    clipped: Tuple[int, int]  # clipped samples in the original and the converted file
    images: Dict[str, str]  # the spectrogram images, ready to embed


def size_row(item: "Item") -> Tuple[str, str]:
    original_size, converted_size = os.path.getsize(item.original), os.path.getsize(item.converted)
    # Both figures come from one rounded percentage, so they add up to 100
    percent = round(100 * converted_size / max(1, original_size), 1)
    change = f"{100 - percent:.1f}% reduction" if percent <= 100 else f"{percent - 100:.1f}% larger"
    return "Converted size", f"{percent:.1f}% of original file ({change})"


def key_rows(view: View, compact: bool = False) -> Rows:
    """The key numbers of one view, shown by both the report and the log.
    `compact` is the short wording for the channel table."""
    def levels(original: float, converted: float) -> str:
        # The difference is of the rounded figures, so it agrees with the numbers beside it
        original, converted = round(original, 1), round(converted, 1)
        difference = minus(converted - original, signed=True)
        if compact:
            return f"{minus(original)} → {minus(converted)} dBFS ({difference} dB)"
        return f"original {minus(original)} dBFS, converted {minus(converted)} dBFS, difference {difference} dB"

    analysis = view.analysis
    return [("Peak level", levels(analysis.levels_original[0], analysis.levels_converted[0])),
            ("RMS level", levels(analysis.levels_original[1], analysis.levels_converted[1])),
            ("Clipping", clipping_label(view.clipped[0], view.clipped[1], compact)),
            ("Low-frequency cutoff", cutoff_label(analysis.low_cutoff, "below")),
            ("High-frequency cutoff", cutoff_label(analysis.cutoff, "above"))]


def channel_table(views: List[View], position_label: str) -> str:
    """Every channel's key numbers in one table: a column per channel, or a
    row each when there are too many. `position_label` heads the line saying
    where each channel sits; empty leaves that line out."""
    labels = ([position_label] if position_label else []) + [label for label, _ in key_rows(views[0], compact=True)]
    cells = [([view.place] if position_label else []) + [str(value) for _, value in key_rows(view, compact=True)]
             for view in views]
    names = [view.name for view in views]
    if len(views) <= MAX_CHANNEL_COLUMNS:
        lines = [[""] + names] + [[label] + [channel[row] for channel in cells] for row, label in enumerate(labels)]
    else:
        lines = [["Channel"] + labels] + [[name] + channel for name, channel in zip(names, cells)]
    head, body = lines[0], lines[1:]
    return ('<table class="channels"><tr>' + "".join(f"<th>{esc(text)}</th>" for text in head) + "</tr>"
            + "".join(f"<tr><th>{esc(line[0])}</th>" + "".join(f"<td>{esc(text)}</td>" for text in line[1:]) + "</tr>"
                      for line in body) + "</table>")


def view_sections(view: View, key: str, range_db: float) -> str:
    """One view's Spectrum and Spectrograms sections, then the data their
    hover readouts use. `key` keeps the ids unique in the page."""
    pair, analysis, images = view.pair, view.analysis, view.images
    difference_labels = [f"\u2265 {db_tick(range_db, True)}", db_tick(range_db / 2, True), "0",
                         db_tick(-range_db / 2, True), f"\u2264 {db_tick(-range_db, True)}"]
    level_labels = [db_tick(v) for v in range(int(DB_MAX), int(DB_MIN) - 1, -20)]
    curves = {"f": [round(v, 1) for v in analysis.curve_f],
              "o": [None if v is None else round(v, 2) for v in analysis.curve_original],
              "c": [None if v is None else round(v, 2) for v in analysis.curve_converted],
              "d": [None if v is None else round(v, 2) for v in analysis.curve_difference]}
    return f"""<section>
<h2>Spectrum</h2>
<div class="legend"><span><span class="key" style="background:var(--original)"></span>Original</span>
<span><span class="key" style="background:var(--converted)"></span>Converted</span>
<span><span class="key" style="background:var(--text-secondary)"></span>Converted minus original</span></div>
{spectrum_figure(analysis, f"readout-spectrum{key}", key)}
<p class="readout" id="readout-spectrum{key}">Move the pointer over the chart to read values.</p>
</section>
<section>
<h2>Spectrograms</h2>
{heat_figure("levels", [("Original", images["original"]), ("Converted", images["converted"])], PANEL_HEIGHT, pair, analysis,
             f"bar-level{key}", INFERNO_STOPS, level_labels, "dBFS", (DB_MIN, DB_MAX), "inferno", f"readout-spectrograms{key}")}
{heat_figure("change", [("Converted minus Original. Red is louder, blue is quieter, grey is unchanged.", images["difference"])], PANEL_HEIGHT, pair, analysis,
             f"bar-change{key}", DIVERGING_STOPS, difference_labels, "dB", (-range_db, range_db), "diverging", f"readout-spectrograms{key}")}
<p class="readout" id="readout-spectrograms{key}">Move the pointer over a spectrogram to read the levels and their difference.</p>
</section>
<script type="application/json" class="curves">{json.dumps(curves, separators=(",", ":"))}</script>"""


@dataclass
class SoundReport:
    """Everything the report shows about one Sound."""
    name: str
    warnings: List[str]
    information: str  # the inside of the Conversion Information section
    views: List[View]  # one, or one per channel
    notes: str  # sentences for the footer, each starting with a space
    digest: str  # the conversion in one line


def tab_row(panels: str, names: List[str]) -> str:
    """A row of tabs, one per name; each shows the sibling of class `panels`
    whose data-key is the tab's place in the row."""
    return (f'<div class="tabs" data-panels="{panels}">'
            + "".join(f'<button type="button" data-key="{key}">{esc(name)}</button>' for key, name in enumerate(names))
            + "</div>")


def sound_block(sound: SoundReport, title: str, key: str, range_db: float) -> str:
    """One Sound's part of the page: warnings, summary, charts and footer, with
    a tab per view if there are several. `key` keeps the ids unique in the page."""
    views = sound.views
    warning_box = ""
    if sound.warnings:
        warning_box = ('<section class="warning"><h2>Check before trusting this comparison</h2><ul>'
                       + "".join(f"<li>{esc(w)}</li>" for w in sound.warnings) + "</ul></section>")
    several = len(views) > 1
    tabs = tab_row("view", [view.name for view in views]) if several else ""
    shown = "".join(f'<div class="view" data-key="{index}">'
                    f'{view_sections(view, f"{key}-{index}" if several else key, range_db)}</div>'
                    for index, view in enumerate(views))
    analysis = views[0].analysis
    cells = f"{analysis.columns} \u00d7 {analysis.rows}"
    window_note = f"Analysis window: {analysis.window_size:,} samples (Hann)"

    return f"""{warning_box}
<section>
<h2>Conversion Information</h2>
{sound.information}
</section>
{tabs}
{shown}
<footer>Generated {datetime.now():%d %B %Y at %H:%M} by {esc(title)}.{sound.notes} {window_note};
{cells} cells per image. The images are embedded in this page; no separate image files are kept.</footer>"""


def build_page(title: str, sounds: List[SoundReport], range_db: float) -> str:
    """The whole report; `title` is the tool's name as the menu entry shows it.
    Several Sounds get a tab each."""
    if len(sounds) == 1:
        subject, tabs = sounds[0].name, ""
        key = [""]
    else:
        subject, tabs = f"{len(sounds)} Sounds", tab_row("sound", [sound.name for sound in sounds])
        key = [f"-{index}" for index in range(len(sounds))]
    blocks = "".join(f'<div class="sound" data-key="{index}">{sound_block(sound, title, key[index], range_db)}</div>'
                     for index, sound in enumerate(sounds))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}: {esc(subject)}</title>
<style>{PAGE_STYLE}</style>
</head>
<body>
<main>
<header>
<h1>{esc(title)}: {esc(subject)}</h1>
<p class="sub">Wwise Conversion analysis: what changed between the original file and the converted media</p>
</header>
{tabs}
{blocks}
</main>
<script type="application/json" id="luts">{json.dumps({"inferno": INFERNO_LUT, "diverging": DIVERGING_LUT}, separators=(",", ":"))}</script>
<script>{PAGE_SCRIPT}</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Wwise and vgmstream
# ---------------------------------------------------------------------------

@dataclass
class Item:
    """What is compared: a Sound's active source in Wwise, or two files given directly."""
    name: str
    original: str  # path of the original file
    converted: str  # path of the converted file; it may not exist yet
    trim_begin: float = -1.0  # seconds from the start of the original; negative = not set
    trim_end: float = -1.0
    fade_in: float = 0.0  # seconds
    fade_out: float = 0.0
    wwise_path: str = ""  # the Sound's path in the project; empty for files given directly
    sound_id: str = ""
    language: str = ""
    platform: str = ""
    channels_setting: str = ""  # the Channels option of the Sound's conversion settings; empty if not known
    notes: List[str] = field(default_factory=list)  # lines to print about how the item was obtained


SOUND_FIELDS = ["id", "name", "type", "path", "activeSource"]
SOURCE_FIELDS = ["type", "originalFilePath", "convertedFilePath", "audioSource:trimValues", "audioSourceLanguage",
                 "FadeInDuration", "FadeOutDuration"]


def read_wwise_item(client, target: Optional[str]) -> Item:
    """The Sound's active source files and trim, for the platform selected in Wwise."""
    # Platforms may differ in settings, source and converted file, so the selected one is named
    project = client.call("ak.wwise.core.getProjectInfo", {})
    platform = next((p["name"] for p in project["platforms"] if p["id"] == project["currentPlatformId"]), "")
    on_platform = {"platform": platform} if platform else {}
    if target:
        key = "id" if target.startswith("{") else "path"
        found = client.call("ak.wwise.core.object.get", {"from": {key: [target]}},
                            options={"return": SOUND_FIELDS, **on_platform})["return"]
        if not found:
            raise AnalysisError(f"No object found in Wwise for {target}.")
    else:
        found = client.call("ak.wwise.ui.getSelectedObjects", {}, options={"return": SOUND_FIELDS})["objects"]
        if len(found) != 1:
            raise AnalysisError(f"Select exactly one Sound in Wwise, or pass its ID or path ({len(found)} objects are selected).")
    sound = found[0]
    if sound["type"] != "Sound":
        raise AnalysisError(f"'{sound['name']}' is a {sound['type']}; a Sound is required.")
    active = sound.get("activeSource") or {}
    if not active.get("id"):
        raise AnalysisError(f"'{sound['name']}' has no audio source.")

    source = client.call("ak.wwise.core.object.get", {"from": {"id": [active["id"]]}},
                         options={"return": SOURCE_FIELDS, **on_platform})["return"][0]
    if not source.get("originalFilePath"):
        raise AnalysisError(f"The source of '{sound['name']}' is a {source.get('type')} with no original file to compare.")
    trim = source.get("audioSource:trimValues") or {}
    return Item(name=sound["name"], original=source["originalFilePath"], converted=source.get("convertedFilePath", ""),
                trim_begin=float(trim.get("trimBegin", -1)), trim_end=float(trim.get("trimEnd", -1)),
                fade_in=float(source.get("FadeInDuration") or 0), fade_out=float(source.get("FadeOutDuration") or 0),
                wwise_path=sound["path"], sound_id=sound["id"],
                language=(source.get("audioSourceLanguage") or {}).get("name", ""), platform=platform,
                channels_setting=read_channels_setting(client, sound["id"], platform))


# The Channels option of Wwise's conversion settings, by the number Wwise stores for it
CHANNELS_SETTINGS = {0: "Mono", 1: "Stereo", 2: "Mono Drop", 3: "Stereo Drop", 4: "As Input"}


def read_channels_setting(client, sound_id: str, platform: str) -> str:
    """The Channels option of the Sound's conversion settings on a platform; empty if Wwise does not say."""
    try:
        sound = client.call("ak.wwise.core.object.get", {"from": {"id": [sound_id]}},
                            options={"return": ["@@Conversion"]})["return"][0]
        conversion = (sound.get("@@Conversion") or {}).get("id")  # the settings in effect, inherited or not
        if not conversion:
            return ""
        options = {"return": ["@Channels"]}
        if platform:
            options["platform"] = platform
        settings = client.call("ak.wwise.core.object.get", {"from": {"id": [conversion]}}, options=options)["return"][0]
        return CHANNELS_SETTINGS.get(settings.get("@Channels"), "")
    except Exception:
        # Extra detail only, and an older Wwise may not answer; never a reason to fail
        return ""


def resolve_from_wwise(target: Optional[str], convert_if_missing: bool, waapi_url: Optional[str]) -> Item:
    """Reads the Sound from Wwise (`target` is its GUID or path; None = the
    selected Sound) and, if asked, has Wwise convert it when unconverted."""
    # Imported here so file mode works without waapi-client
    try:
        from waapi import WaapiClient, CannotConnectToWaapiException, WaapiRequestFailed
    except ImportError:
        raise AnalysisError("waapi-client is required. Install it with:  py -m pip install waapi-client")
    try:
        client = WaapiClient(url=waapi_url, allow_exception=True)  # no URL = the default address
    except CannotConnectToWaapiException:
        raise AnalysisError("Could not connect to Wwise. Check that Wwise is running with a project open and that "
                            "the Wwise Authoring API is enabled in its User Preferences. If Wwise uses a WAMP "
                            "port other than the default, pass --waapi-url.")
    try:
        item = read_wwise_item(client, target)
        if not os.path.exists(item.converted) and convert_if_missing:
            # Writes to the project's cache only
            item.notes.append(f"Converted in Wwise for {item.platform} ({item.language})")
            result = client.call("ak.wwise.core.audio.convert",
                                 {"objects": [item.sound_id], "platforms": [item.platform], "languages": [item.language]})
            for entry in (result or {}).get("errors", []):
                item.notes.append(f"Wwise {entry.get('severity')}: {entry.get('message')}")
        return item
    except WaapiRequestFailed as e:
        detail = getattr(e.args[0], "kwargs", {}).get("message") if e.args else None
        raise AnalysisError(f"Wwise refused the request{f' for {target}' if target else ''}: {detail or e}")
    finally:
        client.disconnect()


def find_vgmstream(given: Optional[str]) -> str:
    """Path of vgmstream-cli: the one given, else beside this script, else on the PATH."""
    if given:
        if not os.path.isfile(given):
            raise AnalysisError(f"vgmstream-cli not found at {given} (from --vgmstream).")
        return given
    name = "vgmstream-cli.exe" if os.name == "nt" else "vgmstream-cli"
    for candidate in (SCRIPT_DIR / "vgmstream-win64" / name, SCRIPT_DIR / name):
        if candidate.is_file():
            return str(candidate)
    on_path = shutil.which("vgmstream-cli")
    if on_path:
        return on_path
    raise AnalysisError(f"vgmstream-cli is needed to decode the converted file but was not found. Download it from "
                        f"https://vgmstream.org and put its folder, named vgmstream-win64, in {SCRIPT_DIR}; "
                        "or add it to the PATH; or pass --vgmstream.")


def decode_with_vgmstream(vgmstream: str, source: str, scratch: str) -> Tuple[str, str, str]:
    """Decodes a .wem to a WAV in the scratch folder. Returns (path, codec, bit rate)."""
    decoded = os.path.join(scratch, "converted_decoded.wav")
    try:
        # -i: decode once, ignoring loop points. stdin is closed off, as Wwise starts this script
        # without one and inheriting that fails.
        result = subprocess.run([vgmstream, "-i", "-o", decoded, source], capture_output=True, text=True,
                                errors="replace", stdin=subprocess.DEVNULL, timeout=DECODE_TIMEOUT_SECONDS,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise AnalysisError(f"vgmstream did not finish decoding {source} within {DECODE_TIMEOUT_SECONDS} seconds.")
    except OSError as e:
        raise AnalysisError(f"vgmstream could not be started ({vgmstream}): {e}")
    if result.returncode != 0 or not os.path.exists(decoded):
        detail = (result.stdout.strip() or result.stderr.strip()).splitlines()
        raise AnalysisError(f"vgmstream could not decode {source} (exit code {result.returncode})"
                            + (f": {detail[-1]}" if detail else "."))
    info = dict(line.split(": ", 1) for line in result.stdout.splitlines() if ": " in line)
    encoding = info.get("encoding", "unknown codec").replace("Custom ", "").replace("Little Endian ", "")
    return decoded, encoding, info.get("bitrate", "unknown bitrate")


def wem_is_ambisonic(path: str) -> bool:
    """Whether a .wem describes its channels as ambisonic. Where a WAV keeps
    its channel mask, a .wem keeps Wwise's channel configuration: the count
    in bits 0-7 and the kind in bits 8-11 (2 = ambisonic)."""
    try:
        with open(path, "rb") as file:
            data = file.read(4096)  # the fmt chunk is at the start of the file
    except OSError:
        return False
    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        return False
    pos = 12
    while pos + 8 <= len(data):
        chunk_id, size = data[pos:pos + 4], struct.unpack_from("<I", data, pos + 4)[0]
        if chunk_id == b"fmt ":
            if size < 24 or pos + 8 + 24 > len(data):
                return False
            channels, configuration = struct.unpack_from("<H", data, pos + 10)[0], struct.unpack_from("<I", data, pos + 28)[0]
            return configuration & 0xFF == channels and configuration >> 8 & 0xF == 2
        pos += 8 + size + (size % 2)
    return False


def channel_names(original: WavFile, partners: List[int], ambisonic: bool) -> List[str]:
    """The report's name for each channel of the original; `partners` gives
    the converted channel that carries each. Ambisonic channels go by letter,
    taken from their place in the converted file, whose order (Wwise's) is known."""
    count = original.channels
    if ambisonic:
        return [AMBISONIC_LETTERS[j] if j < len(AMBISONIC_LETTERS) else f"ACN {j}" for j in partners]
    if count <= 2:
        return ["Mono"] if count == 1 else ["Left", "Right"]
    stated = [speaker for bit, speaker in enumerate(SPEAKERS) if original.channel_mask >> bit & 1]
    return stated if len(stated) == count else [f"Channel {k + 1}" for k in range(count)]


def ordinal(number: int) -> str:
    """1st, 2nd, 3rd, 4th ..."""
    return f"{number}{'th' if 10 <= number % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(number % 10, 'th')}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=f"{TOOL_NAME}: shows what Wwise's conversion did to a Sound, "
                                                 "as one web page.")
    parser.add_argument("objects", nargs="*",
                        help="GUID(s) or path(s) of the Sound(s) to analyse; defaults to the Sound selected in Wwise")
    parser.add_argument("--output-dir", metavar="FOLDER",
                        help=f"keep the report in this folder, named after its Sound. By default the report is "
                             f"{REPORT_PATH.name} in the temp folder, written over on every run")
    parser.add_argument("--window-size", type=int, default=WINDOW_SIZE, choices=(1024, 2048, 4096, 8192, 16384, 32768),
                        help="analysis window in samples; larger = finer frequency detail, coarser timing")
    parser.add_argument("--difference-range", type=float, default=6.0,
                        help="colour range of the level-change image, in dB either side of no change")
    parser.add_argument("--convert-if-missing", action="store_true",
                        help="ask Wwise to convert the Sound when its converted file does not exist yet")
    parser.add_argument("--vgmstream", metavar="PATH",
                        help="vgmstream-cli to decode with; by default the one beside this script, else the one on the PATH")
    parser.add_argument("--waapi-url", metavar="URL",
                        help="address of the Wwise Authoring API, if not the default ws://127.0.0.1:8080/waapi")
    parser.add_argument("--per-channel", action="store_true",
                        help="analyse each channel on its own instead of mixing them to mono; the report gets a "
                             "tab per channel")
    parser.add_argument("--scale", type=int, choices=(1, 2, 3, 4),
                        help=f"spectrogram resolution: {BASE_COLUMNS} x {BASE_ROWS} cells multiplied by this in each "
                             f"direction. Default 2, or 1 for a report on more than {MAX_SOUNDS_AT_FULL_RESOLUTION} "
                             f"Sounds or a per-channel report of more than {MAX_CHANNEL_COLUMNS} channels")
    parser.add_argument("--no-open", action="store_true", help="do not open the page in the default browser")
    parser.add_argument("--verbose", action="store_true",
                        help="also print the trim, alignment and timing details, which the report leaves out, and "
                             "print everything as it happens instead of at the end")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    files = parser.add_argument_group("file mode: compare two files directly, without Wwise")
    files.add_argument("--original", metavar="FILE")
    files.add_argument("--converted", metavar="FILE", help="a .wav is read as it is; anything else is decoded first")
    files.add_argument("--trim-begin", type=float, default=-1.0, metavar="SECONDS",
                       help="where the converted range starts in the original")
    files.add_argument("--trim-end", type=float, default=-1.0, metavar="SECONDS",
                       help="where the converted range ends in the original")
    files.add_argument("--name", help="name to give the report; defaults to the original file's name")
    return parser.parse_args()


def write_report(page: str, output_dir: Optional[str], sounds: List[SoundReport], suffix: str) -> Path:
    """Writes the report and returns its path: by default the one page in the
    temp folder, written over on every run; with `output_dir`, a file there
    named after its Sound(s), `suffix` telling the kinds of report apart."""
    path = REPORT_PATH
    if output_dir:
        name = sounds[0].name + (f" and {len(sounds) - 1} more" if len(sounds) > 1 else "")
        safe = "".join("_" if ch in '\\/:*?"<>|' or ord(ch) < 32 else ch for ch in name).strip(" .") or "Sound"
        path = Path(output_dir) / f"{safe}_ConversionAnalysis{suffix}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written in place, which Windows allows while a browser has the page open (replacing the file is
    # refused then). The second try covers a scanner or indexer holding it for a moment.
    try:
        path.write_text(page, encoding="utf-8")
    except OSError:
        time.sleep(0.5)
        path.write_text(page, encoding="utf-8")
    return path


def make_view(args: argparse.Namespace, item: Item, scale: int, name: str, place: str,
              original: Audio, converted: Audio, clipped: Tuple[int, int]) -> View:
    """Lines one original signal up with its converted copy and analyses the pair."""
    pair = prepare(original, converted, item.trim_begin, item.trim_end)
    analysis = analyse(pair, args.window_size, scale)
    images = render_images(analysis, args.difference_range)
    # The audio and grids are let go of now the figures are drawn, to keep memory down
    pair.original.samples = pair.converted.samples = np.empty(0)
    analysis.grid_original = analysis.grid_converted = analysis.grid_difference = None
    return View(name, place, pair, analysis, clipped, images)


def channel_views(args: argparse.Namespace, item: Item, scale: int, original: WavFile,
                  converted: WavFile) -> Tuple[List[View], str, List[str]]:
    """One view per channel. Returns the views, the label for the line saying
    where each channel sits (empty if none moved) and any pairing warnings."""
    count = original.channels
    warnings = []
    partners = pair_channels(original, converted, item.trim_begin)
    if partners is None:
        partners = list(range(count))
        warnings.append("The channels of the two files could not be paired by what they contain, so they are "
                        "compared in file order.")
    ambisonic = wem_is_ambisonic(item.converted)
    names = channel_names(original, partners, ambisonic)
    # Ambisonic channels are listed in Wwise's order, others in the original file's
    order = sorted(range(count), key=partners.__getitem__) if ambisonic else range(count)
    views = []
    for k in order:
        j = partners[k]
        place = (f"{ordinal(k + 1)} in both files" if k == j else
                 f"{ordinal(k + 1)} in the original, {ordinal(j + 1)} in the converted file")
        views.append(make_view(args, item, scale, names[k], place, one_channel(original, k), one_channel(converted, j),
                               (original.clipped[k], converted.clipped[j])))
    position_label = ""
    if partners != list(range(count)):
        position_label = "Position in the file" + (" (Wwise reorders channels when converting ambisonics)"
                                                   if ambisonic else "")
    return views, position_label, warnings


def mixed_views(args: argparse.Namespace, item: Item, scale: int, original: WavFile,
                converted: WavFile) -> Optional[Tuple[List[View], Rows]]:
    """For a changed channel count: one view per converted channel, each
    compared with the mix of original channels it was made from. Returns the
    views and extra summary lines; None if the mixes cannot be told."""
    mixes = find_mixes(original, converted, item.trim_begin)
    if mixes is None:
        return None
    sources = channel_names(original, list(range(original.channels)), False)
    names = channel_names(converted, list(range(converted.channels)), wem_is_ambisonic(item.converted))
    views = []
    for j, shares in enumerate(mixes):
        used = [k for k, share in enumerate(shares) if share]
        made_from = " + ".join(sources[k] if abs(shares[k] - 1) < 0.02 else f"{minus(shares[k], 2)} × {sources[k]}"
                               for k in used)
        mix = np.zeros(len(original.frames))
        for k in used:
            mix += original.frames[:, k].astype(np.float64) * shares[k]
        views.append(make_view(args, item, scale, names[j], made_from, Audio(mix, original.sample_rate),
                               one_channel(converted, j), (sum(original.clipped[k] for k in used), converted.clipped[j])))
    rows: Rows = []
    if item.channels_setting:
        rows.append(("Channels setting in Wwise", item.channels_setting))
    left_out = [sources[k] for k in range(original.channels) if not any(shares[k] for shares in mixes)]
    if left_out:
        rows.append(("Original channels left out", ", ".join(left_out)))
    return views, rows


def analyse_sound(args: argparse.Namespace, target: Optional[str], title: str, full_resolution: bool,
                  say: Callable[[str], None]) -> SoundReport:
    """Analyses one Sound (`target` is its GUID or path; None = the selected
    Sound) or, in file mode, the two files given. What the report shows is
    also handed to `say`, a line at a time, for the script's output. `title`
    is the tool's name as the menu entry shows it; `full_resolution` is False
    when the report covers too many Sounds for full-size spectrograms."""
    started = time.perf_counter()
    if args.original or args.converted:
        if not (args.original and args.converted):
            raise AnalysisError("File mode needs both --original and --converted.")
        item = Item(name=args.name or Path(args.original).stem, original=str(Path(args.original).resolve()),
                    converted=str(Path(args.converted).resolve()), trim_begin=args.trim_begin, trim_end=args.trim_end)
    else:
        item = resolve_from_wwise(target, args.convert_if_missing, args.waapi_url)
    say(f"\n=== {title}: {item.name} ===")
    for note in item.notes:
        say(note)
    if not os.path.isfile(item.original):
        raise AnalysisError(f"Original file not found: {item.original}")
    if not item.converted or not os.path.isfile(item.converted):
        raise AnalysisError(f"Converted file not found: {item.converted or '(Wwise gave no path)'}. "
                            "Convert the Sound in Wwise (or pass --convert-if-missing), then run this again.")

    # The decoded audio exists only in this folder, which is removed on the way out
    with tempfile.TemporaryDirectory(prefix=f"{TOOL_NAME}_") as scratch:
        codec, bit_rate, decoded, decoder = "", "", item.converted, ""
        if Path(item.converted).suffix.lower() != ".wav":
            decoded, codec, bit_rate = decode_with_vgmstream(find_vgmstream(args.vgmstream), item.converted, scratch)
            decoder = "vgmstream"
        original, converted = read_wav(item.original), read_wav(decoded)

    files: Rows = [("Original", file_details(item.original, original, "", "")),
                   ("Converted", file_details(item.converted, converted, codec, bit_rate))]
    size = size_row(item)
    warnings: List[str] = []
    notes = f" Converted file decoded with {decoder}." if decoder else ""
    if converted.sample_rate != original.sample_rate:
        notes += (f" Converted file resampled from {converted.sample_rate / 1000:g} kHz to "
                  f"{original.sample_rate / 1000:g} kHz for the comparison.")

    # A per-channel report follows the converted file's channels: with as many as the original, each is
    # compared with the original channel it carries; otherwise with the mix it was made from.
    per_channel = args.per_channel and max(original.channels, converted.channels) > 1
    scale = args.scale or (2 if full_resolution and not (per_channel and converted.channels > MAX_CHANNEL_COLUMNS) else 1)
    mixed = None
    if per_channel and converted.channels != original.channels:
        mixed = mixed_views(args, item, scale, original, converted)
        if mixed is None:
            per_channel = False

            def channels(count: int) -> str:
                return f"{count} channel{'' if count == 1 else 's'}"
            warnings.append(f"The original has {channels(original.channels)} and the converted file "
                            f"{channels(converted.channels)}, and what the converted ones were made from could not "
                            "be worked out. All channels are mixed to mono instead.")
    extra: Rows = []  # summary lines beyond the file details and the size
    # Shown last in the summary; not known for files compared without Wwise
    platform: Rows = [("Platform", item.platform)] if item.platform else []
    place_label, place_prefix = "", ""  # what a channel's `place` says, in the table and in the output
    if mixed:
        views, extra = mixed
        place_label, place_prefix = "Made from (channels of the original)", "made from "
        information = side_by_side(files) + table([size] + extra) + channel_table(views, place_label)
        notes += (" Each channel of the converted file is compared with the mix of the original's channels that it "
                  "was made from, as worked out from the audio.")
    elif per_channel:
        views, place_label, pairing_warnings = channel_views(args, item, scale, original, converted)
        warnings += pairing_warnings
        information = side_by_side(files) + table([size]) + channel_table(views, place_label)
        notes += " Each channel is analysed on its own; the two files' channels are paired by what they contain."
    else:
        views = [make_view(args, item, scale, "", "", mono_mix(original), mono_mix(converted),
                           (sum(original.clipped), sum(converted.clipped)))]
        information = side_by_side(files) + table([size] + key_rows(views[0]) + platform)
        if max(original.channels, converted.channels) > 1:
            notes += " Each file's channels are mixed to mono for the analysis."
    if (mixed or per_channel) and platform:
        information += table(platform)  # under the table of channels, which holds the cutoffs here
    # A warning every channel gives is shown once; one that only some give says which
    shared = [w for w in views[0].pair.warnings if all(w in view.pair.warnings for view in views)]
    warnings += shared + [f"{view.name}: {w}" for view in views for w in view.pair.warnings if w not in shared]

    # The output shows what the report shows, in plain characters, as Wwise's log garbles the others
    def plain(text: str) -> str:
        return text.replace("−", "-").replace("×", "x")

    def show(rows: Rows, indent: str = "") -> None:
        for label, value in rows:
            first, *rest = row_lines(value)
            say(plain("\n".join([f"{indent}{label}: {first}"] + [f"{indent}  {line}" for line in rest])))

    show(files + [size] + extra)
    if args.verbose and item.wwise_path:
        say(f"Object: {item.wwise_path}")
    for view in views:
        indent = ""
        if view.name:  # one block per channel
            say(plain(f"{view.name}:" + (f" {place_prefix}{view.place}" if place_label else "")))
            indent = "  "
        show(key_rows(view), indent)
        if args.verbose:
            analysis = view.analysis
            for label, value in view.pair.facts.items():
                say(f"{indent}{label}: {value}")
            for name, (down_3db, gone) in (("Low", analysis.low_cutoff), ("High", analysis.cutoff)):
                say(f"{indent}{name}-frequency cutoff (3 dB below the typical difference): "
                      + ("none" if down_3db is None else f"{down_3db:.1f} Hz")
                      + ("" if gone is None else f"; nothing left ({GONE_DB:g} dB below it) from {gone:.1f} Hz"))
            say(f"{indent}Mean level change {FREQ_MIN:.0f}-{analysis.band_hi:.0f} Hz: {analysis.band_difference:+.2f} dB")
    show(platform)
    for warning in warnings:
        say(f"WARNING: {warning}")
    if args.verbose:
        if item.fade_in > 0 or item.fade_out > 0:
            say(f"Fades: in {item.fade_in:g} s, out {item.fade_out:g} s. They are part of the converted file, "
                "so they show as level differences at the edges")
        say(f"Took {time.perf_counter() - started:.1f} s (window {args.window_size} samples; "
            f"{views[0].analysis.columns} x {views[0].analysis.rows} cells; {len(views)} "
            f"view{'' if len(views) == 1 else 's'})")
    # The Sound in one line, for when the output has to be kept short
    made = dict(files[1][1])
    digest = f"{made['Codec']}, {made['Bit-Rate']}, {made['Sample Rate']}, {made['Channels']}; {size[1]}"
    return SoundReport(item.name, warnings, information, views, notes, digest)


def default_browser() -> Optional[str]:
    """Path of the program Windows uses for web links, or None. Looked up
    directly, as .html files are often set to open in an editor."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        size = ctypes.c_ulong(1024)
        path = ctypes.create_unicode_buffer(size.value)
        # AssocQueryStringW(no flags, ASSOCSTR_EXECUTABLE, association, verb, out, size); 0 means found
        found = ctypes.windll.shlwapi.AssocQueryStringW(0, 2, "http", "open", path, ctypes.byref(size)) == 0
    except (ImportError, AttributeError, OSError):
        return None
    return path.value if found and os.path.isfile(path.value) else None


def open_report(page_path: Path) -> str:
    """Opens the report and returns a line saying what it was opened with."""
    browser = default_browser()
    if browser:
        try:
            # Wwise starts this script without a stdin, and inheriting that fails
            subprocess.Popen([browser, page_path.as_uri()], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return "Opened in web browser"
        except OSError:
            pass  # e.g. a browser installed as a Windows app, which only the shell can start
    if webbrowser.open(page_path.as_uri()):
        return "Opened with the program registered for .html files"
    return "WARNING: no browser could be started. Open the report from the path above."


# What an unread output pipe takes before its writer has to wait, unless the pipe says otherwise,
# and how far under that size to stay
DEFAULT_PIPE_BYTES = 4096
PIPE_MARGIN_BYTES = 128
# How long to wait at the end for the output to be taken
OUTPUT_WAIT_SECONDS = 5.0


def pipe_room(stream) -> Optional[int]:
    """How many bytes a stream takes, unread, before its writer has to wait;
    None if there is no limit (the stream is a file)."""
    try:
        descriptor = stream.fileno()
        mode = os.fstat(descriptor).st_mode
    except (AttributeError, OSError, ValueError):
        return DEFAULT_PIPE_BYTES - PIPE_MARGIN_BYTES
    if stat.S_ISREG(mode):
        return None
    capacity = DEFAULT_PIPE_BYTES
    if os.name == "nt":
        try:
            import ctypes
            import msvcrt
            reported = ctypes.c_ulong(0)
            # GetNamedPipeInfo(pipe, flags, out buffer size, in buffer size, instances); any pipe answers
            if ctypes.windll.kernel32.GetNamedPipeInfo(ctypes.c_void_p(msvcrt.get_osfhandle(descriptor)), None,
                                                       ctypes.byref(reported), None, None) and reported.value:
                capacity = reported.value
        except (ImportError, AttributeError, OSError):
            pass
    return max(256, capacity - PIPE_MARGIN_BYTES)


class Output:
    """What the script prints.

    Wwise reads a command's output only once it has ended, through a pipe
    that holds a few kilobytes; a script that writes more waits for ever. So
    unless it is read as it goes (a console, or --verbose), the output is
    held until the end and cut to what the pipe can hold: everything if that
    fits, otherwise each block's short form."""

    def __init__(self, as_it_goes: bool):
        self.as_it_goes = as_it_goes
        self.blocks: List[Tuple[List[str], List[str]]] = []  # each block's lines, in full and in brief

    def add(self, full: List[str], brief: Optional[List[str]] = None) -> None:
        """Takes a block of lines; `brief` is shown instead when there is not room for everything."""
        if not self.as_it_goes:
            self.blocks.append((full, full if brief is None else brief))
        elif full:
            print("\n".join(full))

    def finish(self) -> bool:
        """Writes whatever was held back. Returns False if it was not taken."""
        if self.as_it_goes:
            return True
        room = pipe_room(sys.stdout)
        encoding = sys.stdout.encoding or "utf-8"

        def size(lines: List[str]) -> int:
            # A line end is written as two bytes on Windows
            return sum(len(line.encode(encoding, "replace")) + 2 * (line.count("\n") + 1) for line in lines)

        lines = [line for full, _ in self.blocks for line in full]
        if room is not None and size(lines) > room:
            lines = [line for _, brief in self.blocks for line in brief]
            if size(lines) > room:
                # Even a line per Sound is too much: keep the closing lines and as many of the first as fit
                note, closing = "... the rest is left out here; the report has everything", lines[-2:]
                kept: List[str] = []
                for line in lines[:-2]:
                    if size(kept + [line, note] + closing) > room:
                        break
                    kept.append(line)
                lines = kept + [note] + closing

        def write() -> None:
            sys.stdout.write("\n".join(lines) + "\n")
            sys.stdout.flush()

        writer = threading.Thread(target=write, daemon=True)
        writer.start()
        writer.join(OUTPUT_WAIT_SECONDS)
        return not writer.is_alive()


def run(args: argparse.Namespace, output: Output) -> int:
    """Analyses everything asked for and writes the report. Returns the exit code."""
    title = TOOL_NAME + (" (per-channel)" if args.per_channel else "")  # as the menu entry shows it
    targets = args.objects or [None]
    # For several Sounds the output says only that the tool ran, that the report opened and what
    # went wrong; the figures are in the report. --verbose still prints everything.
    quiet = len(targets) > 1 and not args.verbose
    failed = False
    sounds: List[SoundReport] = []
    if quiet:
        output.add(["", f"=== {title}: {len(targets)} Sounds ===", "Ran multi selection"])
    # One Sound failing does not stop the others; the report covers those that worked
    for target in targets:
        lines: List[str] = []
        problems: List[str] = []
        sound = None
        try:
            sound = analyse_sound(args, target, title, len(targets) <= MAX_SOUNDS_AT_FULL_RESOLUTION, lines.append)
            sounds.append(sound)
        except AnalysisError as e:
            problems.append(f"ERROR: {e}")
        except OSError as e:
            # A file that cannot be read: locked, no permission
            problems.append(f"ERROR: {e}")
        except Exception:
            # Not expected; the traceback is printed so it can be reported
            problems.append(f"ERROR: unexpected problem{f' with {target}' if target else ''}; details follow.")
            problems.append(traceback.format_exc().rstrip())
        failed = failed or bool(problems)
        if quiet:
            output.add(problems)
        else:
            # In brief: a line on the Sound and its warnings, or all of what went wrong
            output.add(lines + problems, lines + problems if sound is None else
                       [f"{sound.name}: {sound.digest}"] + [f"  WARNING: {warning}" for warning in sound.warnings])
    if not sounds:
        return 1

    # One report for the whole selection
    try:
        page_path = write_report(build_page(title, sounds, args.difference_range), args.output_dir, sounds,
                                 "_PerChannel" if args.per_channel else "")
    except OSError as e:
        # No permission, disk full, or another program has the page locked
        output.add([f"ERROR: the report could not be written. {e}"])
        return 1
    if quiet:
        # Where the report is matters only if it was not opened
        output.add([f"Report: {page_path}"] if args.no_open else [open_report(page_path)])
    else:
        output.add([f"Report: {page_path}"] + ([] if args.no_open else [open_report(page_path)]))
    return 1 if failed else 0


def main() -> int:
    # Wwise's pipe may use an encoding that cannot show every character of a Sound's name
    sys.stdout.reconfigure(errors="replace", line_buffering=True)
    args = parse_args()
    output = Output(as_it_goes=args.verbose or sys.stdout.isatty())
    code = run(args, output)
    if not output.finish():
        # The output is not being taken. The report is written and open, so just end.
        os._exit(code)
    return code


if __name__ == "__main__":
    sys.exit(main())
