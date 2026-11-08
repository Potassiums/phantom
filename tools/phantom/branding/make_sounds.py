"""Synthesize Phantom's interface sound effects: a soft, cozy set (48 kHz, 16-bit mono WAV).

Every sound is a small recipe built from a few warm voices: felt-mallet taps (damped sine modes
with a rounded attack), muted bubble pops (a sine whose pitch glides as it fades), breaths of
filtered air, and a small wooden room. Everything is low-passed and starts without a hard edge,
and each sound is set to a loudness relative to the plain click (`LEVELS`), so the set stays
balanced when one recipe changes. The output is deterministic.

Needs numpy. Usage: python make_sounds.py [output_dir] [--report]
(`--report` prints each sound's loudness and peak without writing anything).
"""
import sys
import wave
from pathlib import Path

import numpy as np

RATE = 48000
REPO = Path(__file__).resolve().parents[3]  # tools/phantom/branding -> repository root
DEFAULT_OUT = REPO / "release" / "datafiles" / "sounds"

# Loudness of the plain click: A-weighted RMS of its loudest 50 ms, in dBFS. Every other sound is
# set relative to it in `LEVELS`; Phantom's own volume preference scales the whole set on top.
REFERENCE_DB = -26.0


# ---------------------------------------------------------------------------------------------
# Building blocks.

def _time(dur):
    return np.arange(int(RATE * dur)) / RATE


def _osc(freq, t):
    """Sine following `freq` (a number or a per-sample array), phase-continuous."""
    freq = np.broadcast_to(np.asarray(freq, dtype=float), t.shape)
    return np.sin(2 * np.pi * np.cumsum(freq) / RATE)


def envelope(t, attack, tau):
    """Rounded (raised-cosine) attack, then an exponential decay with time constant `tau`.
    The rounded start is what keeps every sound free of a hard click."""
    rise = 0.5 - 0.5 * np.cos(np.pi * np.clip(t / max(attack, 1e-6), 0.0, 1.0))
    return rise * np.exp(-np.maximum(t - attack, 0.0) / tau)


def mix(*parts):
    """Sum (sound, offset_seconds) pairs into one buffer."""
    n = max(int(off * RATE) + len(s) for s, off in parts)
    out = np.zeros(n)
    for s, off in parts:
        i = int(off * RATE)
        out[i:i + len(s)] += s
    return out


def _biquad(x, b0, b1, b2, a1, a2):
    y = np.empty(len(x))
    x1 = x2 = y1 = y2 = 0.0
    for i, v in enumerate(x.tolist()):
        o = b0 * v + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        x2, x1, y2, y1 = x1, v, y1, o
        y[i] = o
    return y


def lowpass(x, cutoff, q=0.6):
    """12 dB/octave low-pass (gentle Q, no resonant peak): rounds off the top end."""
    w = 2 * np.pi * cutoff / RATE
    alpha = np.sin(w) / (2 * q)
    c = np.cos(w)
    a0 = 1 + alpha
    return _biquad(x, (1 - c) / 2 / a0, (1 - c) / a0, (1 - c) / 2 / a0, -2 * c / a0, (1 - alpha) / a0)


def highpass(x, cutoff, q=0.6):
    w = 2 * np.pi * cutoff / RATE
    alpha = np.sin(w) / (2 * q)
    c = np.cos(w)
    a0 = 1 + alpha
    return _biquad(x, (1 + c) / 2 / a0, -(1 + c) / a0, (1 + c) / 2 / a0, -2 * c / a0, (1 - alpha) / a0)


def room(x, *, wet=0.18, size=1.0, damp=2400, tail=0.22):
    """A small, soft wooden room: six feedback combs at unrelated lengths (so it never rings
    metallic), darkened and kept out of the low end."""
    n = len(x) + int(tail * RATE)
    dry = np.zeros(n)
    dry[:len(x)] = x
    out = np.zeros(n)
    for ms, fb in ((11.3, 0.50), (13.7, 0.48), (17.9, 0.46), (19.1, 0.44), (23.3, 0.42), (29.7, 0.40)):
        d = int(ms * size * RATE / 1000)
        buf = dry.copy()
        for start in range(d, n, d):  # buf[i] = x[i] + fb * buf[i - d], one delay length at a time.
            end = min(start + d, n)
            buf[start:end] += fb * buf[start - d:end - d]
        out += buf - dry
    out = highpass(lowpass(out / 6, damp), 220)
    return dry + wet * out


def _noise(dur, seed):
    return np.random.default_rng(seed).standard_normal(int(RATE * dur))


# ---------------------------------------------------------------------------------------------
# Voices.

# Mode tables: (frequency ratio, gain, decay scale). Upper modes die away faster than the body.
FELT = ((1.0, 1.0, 1.0), (1.59, 0.22, 0.5), (2.14, 0.07, 0.3))           # Felt on a small box.
WOOD = ((1.0, 1.0, 1.0), (2.76, 0.16, 0.35), (5.40, 0.04, 0.15))         # Little wooden bar.
MARIMBA = ((1.0, 1.0, 1.0), (3.93, 0.10, 0.25), (9.2, 0.015, 0.08))
KALIMBA = ((1.0, 1.0, 1.0), (2.0, 0.08, 0.6), (5.4, 0.035, 0.2))


def tap(freq, dur=0.1, *, modes=FELT, attack=0.002, tau=0.02, drop=0.0, amp=1.0):
    """A soft mallet tap. `drop` bends the pitch down a little right at the start, the 'thock'
    of something soft being struck."""
    t = _time(dur)
    bend = 1.0 + drop * np.exp(-t / 0.006)
    out = np.zeros_like(t)
    for ratio, gain, scale in modes:
        out += gain * _osc(freq * ratio * bend, t) * envelope(t, attack, tau * scale)
    return amp * out


def bubble(f_from, f_to, dur, *, attack=0.004, tau=0.03, amp=1.0):
    """A muted bubble pop: a round sine gliding (quickly at first) from `f_from` to `f_to`."""
    t = _time(dur)
    f = f_from * (f_to / f_from) ** (1.0 - np.exp(-t / (dur / 3)))
    return amp * (_osc(f, t) + 0.05 * _osc(2 * f, t)) * envelope(t, attack, tau)


def air(dur, *, cutoff=1400, attack=0.02, tau=0.03, seed=1, amp=1.0):
    """A breath of soft, dark air."""
    n = highpass(lowpass(_noise(dur, seed), cutoff), 180)
    return amp * n / (n.std() + 1e-12) * 0.3 * envelope(_time(dur), attack, tau)


def grain(dur, *, cutoff=2400, tau=0.002, seed=1, amp=1.0):
    """A tiny puff of muffled noise: the felt or paper texture under a tap."""
    n = lowpass(highpass(_noise(dur, seed), 500), cutoff)
    return amp * n / (n.std() + 1e-12) * 0.3 * envelope(_time(dur), 0.0006, tau)


# Notes (a warm, mostly pentatonic register).
B3, C4, D4, E4, G4, A4 = 246.94, 261.63, 293.66, 329.63, 392.0, 440.0
B4, C5, D5, E5, G5, A5 = 493.88, 523.25, 587.33, 659.26, 783.99, 880.0


# ---------------------------------------------------------------------------------------------
# Recipes. Each returns (samples, cutoff, room wet); `make` finishes and levels them.

def click():
    """Soft felt tap on a small wooden box."""
    s = mix((tap(560, 0.07, attack=0.0015, tau=0.016, drop=0.12), 0),
            (grain(0.012, cutoff=2200, amp=0.12, seed=3), 0))
    return s, 2600, 0.08


def toggle(rising):
    """A muted pop with a little switch body: gliding up when turned on, down when turned off."""
    a, b = (430, 640) if rising else (600, 400)
    s = mix((tap(a, 0.05, attack=0.0015, tau=0.01, amp=0.45), 0),
            (bubble(a, b, 0.08, attack=0.004, tau=0.022), 0.012))
    return s, 2400, 0.1


def menu_open():
    """Soft airy 'fwip' up, as the menu unfolds."""
    s = mix((air(0.07, cutoff=1500, attack=0.015, tau=0.02, seed=4, amp=0.7), 0),
            (bubble(320, 560, 0.09, attack=0.008, tau=0.026), 0.006))
    return s, 2200, 0.1


def confirm():
    """One soft, rounded 'bloop': a felt pluck on a little wooden box that bends up a touch as it
    sounds. Cute and settled, no chime."""
    t = _time(0.22)
    f = G4 * (1.0 - 0.09 * np.exp(-t / 0.03))  # Starts a little flat and rises into the note.
    body = _osc(f, t) * envelope(t, 0.005, 0.05)
    round_ = 0.1 * _osc(2 * f, t) * envelope(t, 0.003, 0.014)  # The octave, gone almost at once.
    s = mix((body + round_, 0), (grain(0.01, cutoff=1400, amp=0.05, seed=9), 0))
    return s, 1600, 0.1


def error():
    """A gentle 'uh-oh': two low wooden notes stepping down, the second softly wobbling."""
    second = tap(B3, 0.3, modes=WOOD, attack=0.003, tau=0.08) + tap(B3 * 1.012, 0.3, modes=WOOD,
                                                                     attack=0.003, tau=0.08)
    s = mix((tap(D4, 0.16, modes=WOOD, attack=0.003, tau=0.05), 0), (0.55 * second, 0.11))
    return s, 1800, 0.16


def grab_start():
    """Soft upward 'boop' as something is picked up."""
    return bubble(300, 480, 0.1, attack=0.005, tau=0.028), 2000, 0.12


def transform_confirm():
    """Two soft rising pops, 'pop-pop': it settles into place (same family as grab)."""
    s = mix((bubble(360, 480, 0.07, tau=0.02), 0), (bubble(480, 640, 0.09, tau=0.026, amp=0.8), 0.055))
    return s, 2200, 0.14


def transform_cancel():
    """One gentle pop sinking back to where it was (also the dialogs' cancel)."""
    return bubble(520, 340, 0.12, attack=0.005, tau=0.036), 2000, 0.14


def undo():
    """Two soft steps back down."""
    s = mix((bubble(560, 450, 0.07, tau=0.02), 0), (bubble(450, 350, 0.09, tau=0.026, amp=0.85), 0.06))
    return s, 2100, 0.14


def redo():
    """Two soft steps back up."""
    s = mix((bubble(350, 450, 0.07, tau=0.02), 0), (bubble(450, 560, 0.09, tau=0.026, amp=0.85), 0.06))
    return s, 2100, 0.14


def type_tap():
    """Muted keycap 'thock' (pitch is varied at playback time)."""
    s = mix((tap(340, 0.05, attack=0.001, tau=0.01, drop=0.18), 0),
            (grain(0.008, cutoff=1800, amp=0.1, seed=5), 0))
    return s, 2000, 0.04


def type_delete():
    """The same key, a little lower and duller."""
    s = mix((tap(270, 0.055, attack=0.0012, tau=0.011, drop=0.22), 0),
            (grain(0.008, cutoff=1400, amp=0.08, seed=6), 0))
    return s, 1700, 0.04


def type_enter():
    """A soft keycap tap into two little marimba notes."""
    s = mix((tap(300, 0.05, attack=0.001, tau=0.01, drop=0.18, amp=0.5), 0),
            (tap(G4, 0.18, modes=MARIMBA, attack=0.002, tau=0.04, amp=0.8), 0.015),
            (tap(D5, 0.22, modes=MARIMBA, attack=0.002, tau=0.055, amp=0.7), 0.055))
    return s, 2600, 0.14


def outliner_select():
    """Soft wooden 'tok'."""
    return tap(620, 0.09, modes=WOOD, attack=0.0015, tau=0.02), 2600, 0.1


def loop_cut():
    """A soft swish along the loop, landing on two kalimba notes."""
    swish = air(0.09, cutoff=1500, attack=0.06, tau=0.02, seed=21, amp=0.8)
    s = mix((swish, 0),
            (bubble(380, 640, 0.08, attack=0.01, tau=0.03, amp=0.4), 0.02),
            (tap(A4, 0.32, modes=KALIMBA, attack=0.002, tau=0.09), 0.065),
            (tap(E5, 0.32, modes=KALIMBA, attack=0.002, tau=0.08, amp=0.5), 0.105))
    return s, 2600, 0.18


def slide_tick():
    """Tiny wooden tick; pitch follows the slide position at playback time."""
    return tap(720, 0.04, modes=WOOD, attack=0.001, tau=0.007), 2400, 0.0


def measure_tick():
    """The softest 'tk' (measuring, and scrubbing the history timeline): it can play many times a
    second, so it is short, dry, low and quiet, the same pitch every step so it reads as a count."""
    return tap(500, 0.035, attack=0.0012, tau=0.0055, drop=0.08), 1800, 0.0


def loop_add():
    """Soft rising 'plip' for each loop added while loop cutting."""
    return bubble(420, 580, 0.07, attack=0.003, tau=0.018), 2000, 0.05


def loop_remove():
    """The same 'plip' sinking back, for each loop taken away."""
    return bubble(560, 420, 0.07, attack=0.003, tau=0.018), 1900, 0.05


def knife_cut():
    """Soft paper-scissors 'snip-snip' with a felt thunk of the blade."""
    s = mix((grain(0.03, cutoff=2600, tau=0.006, seed=31), 0),
            (grain(0.035, cutoff=2300, tau=0.008, seed=37, amp=0.8), 0.05),
            (tap(200, 0.06, attack=0.002, tau=0.014, amp=0.35), 0.05))
    return s, 2800, 0.08


def mode_switch():
    """Soft airy 'fwoop' into a kalimba note: swapping modes, like opening a pocket."""
    s = mix((air(0.1, cutoff=1300, attack=0.035, tau=0.03, seed=41, amp=0.7), 0),
            (bubble(300, 470, 0.1, attack=0.01, tau=0.035, amp=0.6), 0.01),
            (tap(E5, 0.3, modes=KALIMBA, attack=0.002, tau=0.085, amp=0.8), 0.07))
    return s, 2400, 0.18


def focus_chime():
    """Focus Mode opening: a little shop-door bell. One small brass bell (its partials a little
    off the harmonic series, each in a close pair that shimmers), struck three times as its
    clapper bounces, each strike softer. One pitch, no chord."""
    dur = 1.1
    t = _time(dur)
    f0 = 1568.0  # G6: a small bell.
    # (ratio, gain, decay): a small bell's partials; the upper ones die away fast.
    partials = ((1.0, 1.0, 0.30), (2.32, 0.38, 0.12), (4.25, 0.14, 0.05), (6.8, 0.05, 0.025))
    bell = np.zeros_like(t)
    for ratio, gain, tau in partials:
        for detune, share in ((1.0, 0.62), (1.0021, 0.38)):  # The pair beats: the shimmer.
            bell += share * gain * _osc(f0 * ratio * detune, t) * envelope(t, 0.0012, tau)
    strike = grain(0.006, cutoff=6000, tau=0.0012, seed=88, amp=0.05)
    s = mix((bell + np.pad(strike, (0, len(bell) - len(strike))), 0),
            (0.55 * bell[:int(0.9 * RATE)], 0.105),
            (0.28 * bell[:int(0.8 * RATE)], 0.185))
    return s, 7000, 0.2


def save_reminder():
    """The save reminder: a quiet, lower little bell (the Focus Mode bell's sound) tapped twice on
    the same pitch, like a gentle nudge. No second note."""
    t = _time(0.9)
    f0 = 1046.5  # C6.
    partials = ((1.0, 1.0, 0.26), (2.32, 0.3, 0.1), (4.25, 0.1, 0.04))
    bell = np.zeros_like(t)
    for ratio, gain, tau in partials:
        for detune, share in ((1.0, 0.62), (1.0021, 0.38)):
            bell += share * gain * _osc(f0 * ratio * detune, t) * envelope(t, 0.002, tau)
    s = mix((bell, 0), (0.6 * bell[:int(0.75 * RATE)], 0.16))
    return s, 5000, 0.18


def paper_crumple():
    """Soft paper crumple for deleting a file (the start picker): little muffled creases in two
    squeezes of the hand (the second smaller) settling over a faint rustle."""
    rng = np.random.default_rng(1028)
    dur = 0.46
    t = _time(dur)

    def bump(center, width):
        return np.exp(-0.5 * ((t - center) / width) ** 2)

    density = bump(0.075, 0.035) + 0.7 * bump(0.215, 0.04) + 0.22 * bump(0.33, 0.045)
    density /= density.max()
    s = np.zeros(len(t))
    for i in np.nonzero(rng.random(len(t)) < 900 * density / RATE)[0]:
        g = grain(rng.uniform(0.002, 0.007), cutoff=rng.choice((1600, 2600)), tau=rng.uniform(0.0008, 0.002),
                  seed=int(rng.integers(1 << 30)), amp=min(rng.lognormal(0.0, 0.3), 1.6) * (0.4 + 0.6 * density[i]))
        s[i:i + len(g)] += g[:len(s) - i]
    rustle = lowpass(highpass(_noise(dur, 1029), 400), 1800)
    s += rustle / rustle.std() * 0.02 * (0.2 + density)
    s *= np.minimum(1.0, (len(t) - np.arange(len(t))) / (0.06 * RATE)) ** 2  # Fade out.
    return s, 3000, 0.06


def swoosh():
    """A quick, subtle swoosh for deleting something from the scene: soft air through a gentle
    band-pass that sweeps down as it passes, fading in fast and out slowly."""
    dur = 0.22
    t = _time(dur)
    x = _noise(dur, 4096)
    fc = 1800.0 * (380.0 / 1800.0) ** (t / dur)
    # State-variable filter with a moving center; low damping keeps it airy, never whistling.
    tune = 2 * np.sin(np.pi * fc / RATE)
    damping = 1.1
    low = band = 0.0
    out = np.empty(len(x))
    for i, (v, k) in enumerate(zip(x.tolist(), tune.tolist())):
        low += k * band
        band += k * (v - low - damping * band)
        out[i] = band + 0.3 * low
    swell = np.where(t < 0.035, 0.5 - 0.5 * np.cos(np.pi * t / 0.035), np.exp(-(t - 0.035) / 0.06))
    s = out / out.std() * swell
    s *= np.minimum(1.0, (len(t) - np.arange(len(t))) / (0.05 * RATE)) ** 2
    return s, 2000, 0.06


# Loudness of each sound relative to the plain click, in dB. Frequent sounds sit lower; the
# measure/scrub tick (many per second) and slide tick sit lowest.
SOUNDS = {
    "click.wav": (click, 0.0),
    "toggle_on.wav": (lambda: toggle(True), 0.0),
    "toggle_off.wav": (lambda: toggle(False), -1.0),
    "menu_open.wav": (menu_open, -3.0),
    "confirm.wav": (confirm, 0.0),
    "error.wav": (error, 1.5),
    "grab_start.wav": (grab_start, -3.0),
    "transform_confirm.wav": (transform_confirm, -1.5),
    "transform_cancel.wav": (transform_cancel, -2.5),
    "type.wav": (type_tap, -6.0),
    "type_delete.wav": (type_delete, -7.0),
    "type_enter.wav": (type_enter, -3.0),
    "outliner_select.wav": (outliner_select, -3.0),
    "loop_cut.wav": (loop_cut, 0.0),
    "slide_tick.wav": (slide_tick, -10.0),
    "knife_cut.wav": (knife_cut, -4.0),
    "undo.wav": (undo, -3.0),
    "redo.wav": (redo, -3.0),
    "mode_switch.wav": (mode_switch, -1.0),
    "focus_on.wav": (focus_chime, -3.0),
    "save_reminder.wav": (save_reminder, -7.0),
    "measure_tick.wav": (measure_tick, -13.0),
    "loop_add.wav": (loop_add, -6.0),
    "loop_remove.wav": (loop_remove, -7.0),
    "delete.wav": (paper_crumple, -3.0),
    "delete_object.wav": (swoosh, -6.0),
}


# ---------------------------------------------------------------------------------------------
# Leveling and output.

def _a_weighting(f):
    f2 = f * f
    ra = (12194.0 ** 2 * f2 * f2) / ((f2 + 20.6 ** 2) * np.sqrt((f2 + 107.7 ** 2) * (f2 + 737.9 ** 2)) *
                                     (f2 + 12194.0 ** 2))
    return ra / 0.7943  # 0 dB at 1 kHz.


def loudness(x):
    """A-weighted RMS of the loudest 50 ms, in dBFS."""
    n = len(x) + RATE // 10
    y = np.fft.irfft(np.fft.rfft(x, n) * _a_weighting(np.fft.rfftfreq(n, 1 / RATE)), n)
    win = int(0.05 * RATE)
    power = np.convolve(y * y, np.ones(win) / win, mode="valid")
    return 10 * np.log10(power.max() + 1e-20)


def make(recipe, level_db):
    """Low-pass, room, level, trim the silent tail and fade it out."""
    s, cutoff, wet = recipe()
    s = lowpass(s, cutoff)
    if wet > 0:
        s = room(s, wet=wet)
    s = highpass(s, 70)
    s *= 10 ** ((REFERENCE_DB + level_db - loudness(s)) / 20)
    audible = np.nonzero(np.abs(s) > np.abs(s).max() * 10 ** (-60 / 20))[0]
    s = s[:audible[-1] + 1]
    fade = min(len(s), int(0.008 * RATE))
    s[len(s) - fade:] *= np.linspace(1, 0, fade) ** 2
    return s


def write(path, samples):
    pcm = np.round(np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    report = "--report" in sys.argv
    out = Path(args[0]) if args else DEFAULT_OUT
    if not report:
        out.mkdir(parents=True, exist_ok=True)
    for name, (recipe, level_db) in SOUNDS.items():
        s = make(recipe, level_db)
        peak = np.abs(s).max()
        if peak > 0.6:
            raise SystemExit(f"{name}: peak {peak:.2f} leaves too little headroom, lower its level")
        if not report:
            write(out / name, s)
        print(f"{name:22s} {len(s) / RATE * 1000:5.0f} ms  loudness {loudness(s):6.1f} dB  peak {peak:.3f}")


if __name__ == "__main__":
    main()
