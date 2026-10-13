"""Beat and tempo analysis.

Deterministic DSP, no model and no extra dependency: the audio is already
decoded during ingestion, so onset detection runs on data we have.

The pipeline is the standard one:

    PCM -> spectral flux -> adaptive-threshold onsets -> tempo by
    inter-onset histogram -> phase fit -> a regular beat grid

The grid matters more than the raw onsets. Onsets are where energy actually
jumps, which is noisy; a grid is what an editor cuts against, and it stays
sensible through a drum fill or a bar of silence.
"""
from __future__ import annotations

import cmath
import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..config import settings

#: Analysis sample rate. Beats live well below 4 kHz, and downsampling keeps a
#: ten-minute track a couple of seconds of work.
SAMPLE_RATE = 11025
#: ~11.6 ms per frame, fine enough to place a beat inside a video frame.
HOP = 128
WINDOW = 1024

#: Tempo range considered. Below this is usually a half-time artefact, above it
#: is usually a double-time one.
MIN_BPM = 60.0
MAX_BPM = 200.0


@dataclass
class BeatGrid:
    bpm: float = 0.0
    #: Seconds of the first beat; the grid is offset + n * (60 / bpm).
    offset: float = 0.0
    confidence: float = 0.0
    beats: list[float] = field(default_factory=list)
    downbeats: list[float] = field(default_factory=list)
    onsets: list[float] = field(default_factory=list)
    #: Loudest moments, which is where a drop or an impact usually sits.
    accents: list[float] = field(default_factory=list)
    beats_per_bar: int = 4

    def to_dict(self) -> dict:
        return {
            "bpm": round(self.bpm, 2),
            "offset": round(self.offset, 4),
            "confidence": round(self.confidence, 3),
            "beats_per_bar": self.beats_per_bar,
            "beats": [round(b, 4) for b in self.beats],
            "downbeats": [round(b, 4) for b in self.downbeats],
            "onsets": [round(o, 4) for o in self.onsets],
            "accents": [round(a, 4) for a in self.accents],
        }


def _decode_mono(path: Path) -> list[float]:
    """Decode to mono float samples at the analysis rate."""
    argv = [
        settings.ffmpeg_path, "-hide_banner", "-v", "error", "-nostdin",
        "-i", str(path), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-f", "s16le", "-acodec", "pcm_s16le", "-",
    ]
    proc = subprocess.run(argv, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        return []
    raw = proc.stdout
    return [
        int.from_bytes(raw[i:i + 2], "little", signed=True) / 32768.0
        for i in range(0, len(raw) - 1, 2)
    ]


def _hann(size: int) -> list[float]:
    return [0.5 - 0.5 * math.cos(2 * math.pi * i / (size - 1)) for i in range(size)]


def _fft(values: list[complex]) -> list[complex]:
    """Iterative radix-2 FFT. Input length must be a power of two."""
    n = len(values)
    if n <= 1:
        return values

    # Bit-reversal permutation.
    out = list(values)
    bits = n.bit_length() - 1
    for i in range(n):
        j = int(f"{i:0{bits}b}"[::-1], 2)
        if j > i:
            out[i], out[j] = out[j], out[i]

    size = 2
    while size <= n:
        step = cmath.exp(-2j * math.pi / size)
        half = size // 2
        for start in range(0, n, size):
            twiddle = 1 + 0j
            for k in range(half):
                a = out[start + k]
                b = out[start + k + half] * twiddle
                out[start + k] = a + b
                out[start + k + half] = a - b
                twiddle *= step
        size *= 2
    return out


def spectral_flux(samples: list[float]) -> list[float]:
    """Per-frame increase in spectral energy: the onset detection function.

    Only rises count. A note starting is a rise; the same note decaying is a
    fall, and counting falls would put a beat at the end of every note.
    """
    if len(samples) < WINDOW:
        return []
    window = _hann(WINDOW)
    flux: list[float] = []
    previous: list[float] = [0.0] * (WINDOW // 2)

    for start in range(0, len(samples) - WINDOW, HOP):
        frame = [samples[start + i] * window[i] for i in range(WINDOW)]
        spectrum = _fft([complex(v, 0.0) for v in frame])
        magnitude = [abs(spectrum[i]) for i in range(WINDOW // 2)]
        rise = sum(
            m - p for m, p in zip(magnitude, previous) if m > p
        )
        flux.append(rise)
        previous = magnitude
    return flux


def pick_onsets(flux: list[float], sensitivity: float = 1.4) -> list[int]:
    """Frames where the flux clears a local moving threshold.

    An absolute threshold would find nothing in a quiet passage and everything
    in a loud one, so the threshold follows the local mean.
    """
    if not flux:
        return []
    span = 20                      # ~230 ms either side
    minimum_gap = 4                # ~46 ms, below any real note spacing
    picked: list[int] = []
    last = -minimum_gap

    for i, value in enumerate(flux):
        lo = max(0, i - span)
        hi = min(len(flux), i + span + 1)
        local = flux[lo:hi]
        threshold = (sum(local) / len(local)) * sensitivity
        if value <= threshold or value <= 1e-9:
            continue
        if value < max(local):     # keep only the local peak
            continue
        if i - last < minimum_gap:
            continue
        picked.append(i)
        last = i
    return picked


def estimate_tempo(onset_times: list[float]) -> tuple[float, float]:
    """Tempo from the spacing between onsets, as (bpm, confidence).

    Every pair of nearby onsets votes for the tempo its spacing implies.
    Musical spacings agree with each other -- eighths reinforce quarters --
    so the true tempo accumulates votes while noise spreads out.
    """
    if len(onset_times) < 4:
        return 0.0, 0.0

    votes: dict[int, float] = {}
    for i, a in enumerate(onset_times):
        for b in onset_times[i + 1:i + 9]:
            gap = b - a
            if gap <= 0.2 or gap > 2.0:
                continue
            bpm = 60.0 / gap
            # Fold into range: a half-time and a double-time reading of the
            # same music should vote together.
            while bpm < MIN_BPM:
                bpm *= 2
            while bpm > MAX_BPM:
                bpm /= 2
            bucket = int(round(bpm))
            # Neighbouring buckets get partial credit so a slightly loose
            # performance still converges.
            for offset, weight in ((-1, 0.5), (0, 1.0), (1, 0.5)):
                votes[bucket + offset] = votes.get(bucket + offset, 0.0) + weight

    if not votes:
        return 0.0, 0.0

    # Onsets are never perfectly regular, so the true tempo smears across
    # neighbouring buckets while its half-time stays concentrated -- doubling
    # the span averages the jitter away. Smoothing puts the fundamental back
    # together before anything is compared.
    smoothed: dict[int, float] = {}
    for bpm in range(int(MIN_BPM), int(MAX_BPM) + 1):
        total = 0.0
        for offset in range(-4, 5):
            weight = math.exp(-0.5 * (offset / 2.0) ** 2)
            total += votes.get(bpm + offset, 0.0) * weight
        if total > 0:
            smoothed[bpm] = total
    if not smoothed:
        return 0.0, 0.0

    # Shortlist the strongest peaks, then judge each against the octaves it
    # could be confused with. Votes alone cannot separate 120 from 60; how
    # well a regular grid lands on the onsets can.
    ranked = sorted(smoothed, key=lambda k: smoothed[k], reverse=True)[:6]
    candidates: set[int] = set()
    for bpm in ranked:
        for factor in (0.5, 1.0, 2.0, 3.0):
            scaled = int(round(bpm * factor))
            if MIN_BPM <= scaled <= MAX_BPM:
                candidates.add(scaled)

    best_bpm = 0.0
    best_score = -1.0
    for bpm in candidates:
        score = grid_fit(onset_times, float(bpm)) * tempo_prior(float(bpm))
        if score > best_score:
            best_score = score
            best_bpm = float(bpm)

    return best_bpm, max(0.0, min(1.0, best_score))


def tempo_prior(bpm: float) -> float:
    """How readily a tempo is felt as *the* pulse.

    Listeners cluster around 120 BPM and the preference falls away in octaves
    either side, so this breaks ties that the evidence alone cannot.
    """
    return math.exp(-0.5 * (math.log2(bpm / 120.0) / 0.9) ** 2)


def grid_fit(onset_times: list[float], bpm: float) -> float:
    """How well a regular grid at this tempo explains the onsets, 0..1.

    Two errors have to be caught, and they need opposite measures:

    - Too slow. A grid at half the real tempo hits every other onset and
      leaves the rest stranded between beats, so *coverage* falls.
    - Too fast. A grid at double the real tempo hits every onset, but half its
      beats land on silence, so *support* falls.

    Coverage alone picks 150 over 75; support alone picks 60 over 120. Their
    product is the only thing that gets both right.
    """
    if bpm <= 0 or len(onset_times) < 2:
        return 0.0
    period = 60.0 / bpm
    offset = fit_phase(onset_times, bpm, 0.0)

    def closeness(time: float) -> float:
        phase = ((time - offset) % period) / period
        distance = min(phase, 1.0 - phase) * 2.0   # 0 on a beat, 1 between
        return max(0.0, 1.0 - distance * 3.0)

    coverage = sum(closeness(t) for t in onset_times) / len(onset_times)

    first, last = onset_times[0], onset_times[-1]
    beats: list[float] = []
    time = offset + math.floor((first - offset) / period) * period
    while time <= last + period / 2:
        if time >= first - period / 2:
            beats.append(time)
        time += period
    if not beats:
        return 0.0

    tolerance = period * 0.25
    hit = sum(
        1 for beat in beats
        if any(abs(beat - onset) <= tolerance for onset in onset_times)
    )
    support = hit / len(beats)

    return coverage * support


def fit_phase(onset_times: list[float], bpm: float, duration: float) -> float:
    """Where the grid starts: the offset that lands nearest the onsets."""
    if bpm <= 0 or not onset_times:
        return 0.0
    period = 60.0 / bpm
    steps = 48
    best_offset = 0.0
    best_score = -1.0
    for step in range(steps):
        offset = period * step / steps
        score = 0.0
        for time in onset_times:
            phase = ((time - offset) % period) / period
            distance = min(phase, 1.0 - phase)      # 0 on the beat, 0.5 off it
            score += 1.0 - distance * 2.0
        if score > best_score:
            best_score = score
            best_offset = offset
    return best_offset


def analyze_beats(path: Path, duration: float) -> BeatGrid:
    """Full tempo and beat analysis for one audio-bearing file."""
    samples = _decode_mono(path)
    if len(samples) < WINDOW * 4:
        return BeatGrid()

    flux = spectral_flux(samples)
    if not flux:
        return BeatGrid()

    frame_seconds = HOP / SAMPLE_RATE
    onset_frames = pick_onsets(flux)
    onsets = [f * frame_seconds for f in onset_frames]

    bpm, confidence = estimate_tempo(onsets)
    if bpm <= 0:
        return BeatGrid(onsets=onsets)

    offset = fit_phase(onsets, bpm, duration)
    period = 60.0 / bpm
    total = duration or (len(samples) / SAMPLE_RATE)

    beats: list[float] = []
    time = offset
    while time < total:
        if time >= 0:
            beats.append(time)
        time += period

    # Bar lines: assume 4/4 and start on the strongest of the first four beats.
    strengths = [flux[min(len(flux) - 1, int(b / frame_seconds))] for b in beats[:4]]
    first_downbeat = strengths.index(max(strengths)) if strengths else 0
    downbeats = beats[first_downbeat::4]

    # Accents: the loudest onsets, which is where a drop or impact sits.
    ranked = sorted(onset_frames, key=lambda f: flux[f], reverse=True)
    accents = sorted(f * frame_seconds for f in ranked[:max(4, len(ranked) // 12)])

    return BeatGrid(
        bpm=bpm, offset=offset, confidence=confidence, beats=beats,
        downbeats=downbeats, onsets=onsets, accents=accents,
    )


def nearest_beat(grid: dict, time: float, tolerance: float = 0.25) -> float | None:
    """Snap a time to the nearest beat, or None if none is close enough."""
    beats = grid.get("beats") or []
    if not beats:
        return None
    best = min(beats, key=lambda b: abs(b - time))
    return best if abs(best - time) <= tolerance else None
