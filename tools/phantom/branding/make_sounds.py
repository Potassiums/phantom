"""Synthesize Phantom's interface sound effects (48 kHz, 16-bit mono WAV).

Pure standard library so it runs anywhere. Tweak the recipes below and re-run.

Usage: python make_sounds.py [output_dir]
"""
import math
import random
import struct
import sys
import wave
from pathlib import Path

RATE = 48000
REPO = Path(__file__).resolve().parents[3]  # tools/phantom/branding -> repository root
DEFAULT_OUT = REPO / "release" / "datafiles" / "sounds"


def silence(seconds):
    return [0.0] * int(RATE * seconds)


def mix_into(dst, src, offset_s=0.0):
    off = int(offset_s * RATE)
    need = off + len(src)
    if len(dst) < need:
        dst.extend([0.0] * (need - len(dst)))
    for i, v in enumerate(src):
        dst[off + i] += v


def envelope(i, attack_s, tau_s):
    """Linear attack, then exponential decay with time constant tau."""
    t = i / RATE
    if t < attack_s:
        return t / attack_s
    return math.exp(-(t - attack_s) / tau_s)


def tone(freq, dur, *, amp=1.0, attack=0.002, tau=0.03, glide_to=None, partials=((1, 1.0),)):
    """Sine-based tone with optional exponential pitch glide and extra partials."""
    n = int(RATE * dur)
    out = []
    phase = [0.0] * len(partials)
    for i in range(n):
        f = freq if glide_to is None else freq * (glide_to / freq) ** (i / n)
        s = 0.0
        for k, (mult, gain) in enumerate(partials):
            phase[k] += 2 * math.pi * f * mult / RATE
            s += gain * math.sin(phase[k])
        out.append(amp * s * envelope(i, attack, tau))
    return out


def noise_burst(dur, *, amp=1.0, tau=0.002, highpass=0.85, seed=7):
    rng = random.Random(seed)
    n = int(RATE * dur)
    out, prev_in, prev_out = [], 0.0, 0.0
    for i in range(n):
        x = rng.uniform(-1, 1)
        # One-pole high-pass keeps only the crisp top end.
        y = highpass * (prev_out + x - prev_in)
        prev_in, prev_out = x, y
        out.append(amp * y * envelope(i, 0.0003, tau))
    return out


def finish(samples, peak):
    """Normalize to `peak`, add 4 ms fade-out, soft-clip for safety."""
    m = max(1e-9, max(abs(s) for s in samples))
    samples = [s / m * peak for s in samples]
    fade = int(0.004 * RATE)
    for i in range(min(fade, len(samples))):
        samples[-1 - i] *= i / fade
    return [math.tanh(s * 1.2) / math.tanh(1.2) for s in samples]


def write(path, samples):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, s)) * 32767)) for s in samples))


def lowpass(samples, cutoff_hz):
    """One-pole low-pass: rounds off the top end for a warmer, softer sound."""
    a = 1.0 - math.exp(-2 * math.pi * cutoff_hz / RATE)
    out, y = [], 0.0
    for x in samples:
        y += a * (x - y)
        out.append(y)
    return out


def room(samples, *, mix=0.22, tail_s=0.18):
    """Tiny cozy room: a few feedback comb delays, like a small wooden house."""
    out = samples + [0.0] * int(tail_s * RATE)
    wet = [0.0] * len(out)
    for delay_ms, fb in ((23.0, 0.45), (31.0, 0.4), (41.0, 0.35)):
        d = int(delay_ms / 1000 * RATE)
        buf = [0.0] * len(out)
        for i in range(len(out)):
            buf[i] = (out[i] if i < len(samples) else 0.0) + (fb * buf[i - d] if i >= d else 0.0)
            wet[i] += buf[i] / 3
    wet = lowpass(wet, 3000)
    return [d + mix * w for d, w in zip(out, wet)]


# Cozy instrument voices (inspired by the soft, rounded sound design of life-sim games).
MARIMBA = ((1, 1.0), (3.93, 0.18))
KALIMBA = ((1, 1.0), (2.0, 0.12), (5.4, 0.05))


def marimba(freq, dur=0.22, *, amp=1.0, tau=0.07):
    body = tone(freq, dur, amp=amp, attack=0.003, tau=tau, partials=MARIMBA)
    # The upper partial of a marimba bar dies away much faster than the body.
    knock = tone(freq * 3.93, min(dur, 0.04), amp=amp * 0.12, attack=0.001, tau=0.008)
    mix_into(body, knock)
    return body


def kalimba(freq, dur=0.3, *, amp=1.0, tau=0.09):
    s = tone(freq, dur, amp=amp, attack=0.002, tau=tau, partials=KALIMBA)
    mix_into(s, tone(freq * 7.1, 0.02, amp=amp * 0.06, attack=0.0005, tau=0.004))  # Tine "tink".
    return s


def bubble(f_from, f_to, dur, *, amp=1.0, tau=0.035):
    return tone(f_from, dur, amp=amp, attack=0.004, tau=tau, glide_to=f_to, partials=((1, 1.0), (2, 0.08)))


def wood_block(freq, *, amp=1.0):
    s = tone(freq, 0.05, amp=amp, attack=0.0008, tau=0.012, partials=((1, 1.0), (2.7, 0.3)))
    mix_into(s, lowpass(noise_burst(0.01, amp=amp * 0.25, tau=0.002, highpass=0.5, seed=11), 2500))
    return s


def cozy(samples, *, peak, cutoff=4200, reverb=0.22):
    return finish(room(lowpass(samples, cutoff), mix=reverb), peak)


# Note frequencies (pentatonic-friendly picks).
C5, D5, E5, G5, A5 = 523.25, 587.33, 659.26, 783.99, 880.0
C6, D6, E6, G6 = 1046.5, 1174.66, 1318.51, 1567.98


def grab_start():
    """Soft upward 'boop' as a vertex/object is picked up."""
    return cozy(bubble(380, 620, 0.09, tau=0.03), peak=0.16, cutoff=2600)


def transform_confirm():
    """Two soft rising bubbles: 'pop-pop', it settles into place (same family as grab)."""
    s = bubble(420, 560, 0.06, tau=0.022)
    mix_into(s, bubble(560, 760, 0.08, amp=0.8, tau=0.028), 0.055)
    return cozy(s, peak=0.15, cutoff=2400)


def transform_cancel():
    """One gentle bubble sinking back to where it was."""
    return cozy(bubble(560, 360, 0.11, tau=0.04), peak=0.14, cutoff=2200)


def undo():
    """Two soft steps back down."""
    s = bubble(700, 520, 0.06, tau=0.022)
    mix_into(s, bubble(560, 400, 0.08, amp=0.85, tau=0.028), 0.06)
    return cozy(s, peak=0.13, cutoff=2400, reverb=0.18)


def redo():
    """Two soft steps back up."""
    s = bubble(400, 560, 0.06, tau=0.022)
    mix_into(s, bubble(520, 700, 0.08, amp=0.85, tau=0.028), 0.06)
    return cozy(s, peak=0.13, cutoff=2400, reverb=0.18)


def mode_switch():
    """Soft airy 'fwoop' into a kalimba note: swapping tools, like opening a pocket."""
    air = lowpass(noise_burst(0.09, amp=0.3, tau=0.04, highpass=0.5, seed=41), 1800)
    air = [v * min(1.0, i / (0.03 * RATE)) for i, v in enumerate(air)]  # Breathe in.
    s = air
    mix_into(s, bubble(330, 520, 0.1, amp=0.7, tau=0.035))
    mix_into(s, kalimba(E5, 0.28, amp=0.55, tau=0.08), 0.07)
    return cozy(s, peak=0.15, cutoff=2600, reverb=0.2)


def type_tap():
    """Little rounded key 'pip' - pitch is varied at playback time."""
    s = tone(1050, 0.03, attack=0.001, tau=0.007, partials=((1, 1.0), (2, 0.1)))
    mix_into(s, lowpass(noise_burst(0.006, amp=0.2, tau=0.0015, highpass=0.4, seed=5), 3000))
    return cozy(s, peak=0.13, cutoff=3800, reverb=0.08)


def type_delete():
    s = tone(620, 0.04, attack=0.001, tau=0.009, glide_to=520, partials=((1, 1.0), (2, 0.1)))
    return cozy(s, peak=0.12, cutoff=3000, reverb=0.08)


def type_enter():
    s = marimba(G5, 0.12, tau=0.03)
    mix_into(s, marimba(D6, 0.16, tau=0.04), 0.035)
    return cozy(s, peak=0.15, reverb=0.15)


def outliner_select():
    """Soft wooden 'tok'."""
    return cozy(wood_block(740), peak=0.15, cutoff=3200, reverb=0.12)


def loop_cut():
    """A soft 'zip' along the loop, ending in a little chime."""
    zip_ = noise_burst(0.07, amp=0.35, tau=0.03, highpass=0.7, seed=21)
    zip_ = [v * (i / len(zip_)) for i, v in enumerate(zip_)]  # Swell into the chime.
    s = lowpass(zip_, 2200)
    mix_into(s, bubble(500, 900, 0.07, amp=0.5, tau=0.03))
    mix_into(s, kalimba(A5, 0.3, tau=0.09), 0.06)
    mix_into(s, kalimba(E6, 0.3, amp=0.5, tau=0.08), 0.1)
    return cozy(s, peak=0.19)


def slide_tick():
    """Tiny wooden tick; pitch follows the slide position at playback time."""
    return cozy(wood_block(1200, amp=0.8), peak=0.09, cutoff=3500, reverb=0.05)


def measure_tick():
    """Muted little 'tk' of a measuring wheel - same pitch every step, so it reads as a steady count."""
    s = wood_block(880, amp=0.7)
    mix_into(s, tone(440, 0.04, amp=0.25, attack=0.001, tau=0.01))  # Soft body under the tick.
    return cozy(s, peak=0.07, cutoff=2400, reverb=0.06)


def loop_add():
    """Soft rising "plip" for each loop added while loop cutting."""
    return cozy(bubble(480, 660, 0.07, tau=0.022), peak=0.11, cutoff=2600, reverb=0.08)


def loop_remove():
    """The same "plip" sinking back, for each loop taken away."""
    return cozy(bubble(660, 470, 0.07, tau=0.022), peak=0.10, cutoff=2400, reverb=0.08)


def knife_cut():
    """Soft paper-scissors 'snip'."""
    a = lowpass(noise_burst(0.025, amp=1.0, tau=0.006, highpass=0.8, seed=31), 5000)
    b = lowpass(noise_burst(0.03, amp=0.8, tau=0.008, highpass=0.8, seed=37), 4200)
    s = a
    mix_into(s, b, 0.045)
    mix_into(s, tone(260, 0.05, amp=0.25, attack=0.002, tau=0.015), 0.045)  # Soft 'thunk' of the blade.
    return cozy(s, peak=0.15, cutoff=4500, reverb=0.1)


BELL = ((1, 1.0), (2, 0.25), (3, 0.06))
REED = ((1, 1.0), (3, 0.22), (5, 0.08))
BUZZ = ((1, 1.0), (3, 1 / 3), (5, 1 / 5), (7, 1 / 7))


def click():
    s = tone(2400, 0.035, glide_to=1700, tau=0.006, attack=0.0005)
    mix_into(s, noise_burst(0.006, amp=0.5))
    return finish(s, 0.32)


def toggle(rising):
    lo, hi = 880.0, 1320.0
    a, b = (lo, hi) if rising else (hi, lo)
    s = tone(a, 0.045, tau=0.015, partials=REED)
    mix_into(s, tone(b, 0.06, tau=0.02, partials=REED), 0.035)
    return finish(s, 0.26 if rising else 0.22)


def menu_open():
    s = tone(520, 0.075, glide_to=1150, tau=0.025, attack=0.004)
    mix_into(s, noise_burst(0.02, amp=0.15, tau=0.008, highpass=0.6, seed=3))
    return finish(s, 0.14)


def confirm():
    s = tone(1318.5, 0.16, tau=0.05, partials=BELL)        # E6
    mix_into(s, tone(1975.5, 0.2, tau=0.07, partials=BELL), 0.065)  # B6
    return finish(s, 0.3)


def cancel():
    s = tone(880.0, 0.12, tau=0.04, partials=REED)          # A5
    mix_into(s, tone(659.3, 0.16, tau=0.05, partials=REED), 0.065)  # E5
    return finish(s, 0.26)


def error():
    s = tone(196.0, 0.09, tau=0.06, attack=0.003, partials=BUZZ)
    mix_into(s, tone(185.0, 0.12, tau=0.07, attack=0.003, partials=BUZZ), 0.12)
    return finish(s, 0.3)


SOUNDS = {
    "click.wav": click,
    "toggle_on.wav": lambda: toggle(True),
    "toggle_off.wav": lambda: toggle(False),
    "menu_open.wav": menu_open,
    "confirm.wav": confirm,
    "cancel.wav": cancel,
    "error.wav": error,
    "grab_start.wav": grab_start,
    "transform_confirm.wav": transform_confirm,
    "transform_cancel.wav": transform_cancel,
    "type.wav": type_tap,
    "type_delete.wav": type_delete,
    "type_enter.wav": type_enter,
    "outliner_select.wav": outliner_select,
    "loop_cut.wav": loop_cut,
    "slide_tick.wav": slide_tick,
    "knife_cut.wav": knife_cut,
    "undo.wav": undo,
    "redo.wav": redo,
    "mode_switch.wav": mode_switch,
    "measure_tick.wav": measure_tick,
    "loop_add.wav": loop_add,
    "loop_remove.wav": loop_remove,
}


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    out.mkdir(parents=True, exist_ok=True)
    for name, fn in SOUNDS.items():
        samples = fn()
        write(out / name, samples)
        print(f"{name:16s} {len(samples) / RATE * 1000:5.0f} ms  peak {max(abs(x) for x in samples):.2f}")


if __name__ == "__main__":
    main()
