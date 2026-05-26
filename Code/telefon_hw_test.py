#!/usr/bin/env python3
"""
Telephone hardware test for the KI-Telefon / Telekom 611-2 conversion.

Tests:
  1) Handset / Speaker: Play 425 Hz tone
  2) Microphone: Level meter + short recording with playback
  3) Hook switch: Show live pickup/hangup state
  4) Rotary dial: Count pulses and display dialed digit
  5) Bell: Drive H-bridge briefly with 25 Hz AC

Project Pins:
  Hook switch: GPIO 5  to GND, Pull-Up active, LOW = lifted
  Rotary dial: GPIO 26 to GND, Pull-Up active, falling edge = pulse
  Bell:        IN1 GPIO 17, IN2 GPIO 27, ENA GPIO 22

Requirements:
  sudo apt install python3-rpi.gpio python3-sounddevice python3-numpy
  # or inside the existing project venv: pip install sounddevice numpy RPi.GPIO
"""

from __future__ import annotations

import argparse
import math
import queue
import sys
import time
import numpy as np
import sounddevice as sd
from dataclasses import dataclass
from typing import Optional
from config import MOTORDRIVER

try:
    import RPi.GPIO as GPIO
except Exception as exc:  # Abort on non-Raspberry systems
    GPIO = None
    GPIO_IMPORT_ERROR = exc
else:
    GPIO_IMPORT_ERROR = None


@dataclass
class Pins:
    handset: int = 5
    rotary: int = 26
    bell_in1: int = 17
    bell_in2: int = 27
    bell_ena: int = 22


@dataclass
class AudioCfg:
    samplerate: int = 24000
    channels: int = 1
    tone_freq: float = 425.0
    tone_volume: float = 0.15


@dataclass
class BellCfg:
    frequency: float = 25.0
    seconds: float = 1.5
    pause: float = 1.0
    rings: int = 1


PINS = Pins()
AUDIO = AudioCfg()
BELL = BellCfg()


def require_gpio() -> None:
    """Ensure RPi.GPIO is available and initialized."""
    if GPIO is None:
        raise RuntimeError(f"RPi.GPIO konnte nicht importiert werden: {GPIO_IMPORT_ERROR}")
    if GPIO.getmode() is None:
        GPIO.setmode(GPIO.BCM)


def setup_gpio(setup_bell: bool = False) -> None:
    """Configure basic input/output pins."""
    require_gpio()
    GPIO.setup(PINS.handset, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(PINS.rotary, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    if setup_bell:
        GPIO.setup(PINS.bell_in1, GPIO.OUT)
        GPIO.setup(PINS.bell_in2, GPIO.OUT)
        if MOTORDRIVER == "L298N":
            GPIO.setup(PINS.bell_ena, GPIO.OUT)
        bell_off(disable_enable=True)


def handset_lifted() -> bool:
    """Check if handset is currently lifted."""
    setup_gpio()
    return GPIO.input(PINS.handset) == GPIO.LOW


def bell_off(disable_enable: bool = False) -> None:
    """Stop the bell ringing safely."""
    require_gpio()
    GPIO.output(PINS.bell_in1, GPIO.LOW)
    GPIO.output(PINS.bell_in2, GPIO.LOW)
    if disable_enable and MOTORDRIVER == "L298N":
        GPIO.output(PINS.bell_ena, GPIO.LOW)


def test_bell(rings: int = 1, seconds: float = 1.5, frequency: float = 25.0, pause: float = 1.0) -> None:
    """
    Drives the bell bipolarly via H-bridge like bell.py.
    Aborts as soon as the handset is lifted to mimic real logic.
    """
    setup_gpio(setup_bell=True)
    half_period = 1.0 / (frequency * 2.0)
    print(f"\nKlingeltest: IN1=GPIO {PINS.bell_in1}, IN2=GPIO {PINS.bell_in2}, ENA=GPIO {PINS.bell_ena}")
    print(f"{rings}x {seconds:.1f}s mit {frequency:.1f} Hz. Abheben beendet den Test. Strg+C bricht ab.")

    try:
        if MOTORDRIVER == "L298N":
            GPIO.output(PINS.bell_ena, GPIO.HIGH)
        for ring_no in range(1, rings + 1):
            print(f"Klingelrunde {ring_no}/{rings} ...")
            end = time.time() + seconds
            while time.time() < end:
                if handset_lifted():
                    print("Hörer abgehoben – Klingeltest beendet.")
                    return
                GPIO.output(PINS.bell_in1, GPIO.HIGH)
                GPIO.output(PINS.bell_in2, GPIO.LOW)
                time.sleep(half_period)
                if handset_lifted():
                    print("Hörer abgehoben – Klingeltest beendet.")
                    return
                GPIO.output(PINS.bell_in1, GPIO.LOW)
                GPIO.output(PINS.bell_in2, GPIO.HIGH)
                time.sleep(half_period)
            bell_off(disable_enable=False)
            if ring_no < rings:
                time.sleep(pause)
        print("Klingeltest beendet.")
    except KeyboardInterrupt:
        print("\nKlingeltest abgebrochen.")
    finally:
        bell_off(disable_enable=True)


def digit_from_pulses(pulses: int) -> Optional[int]:
    """
    Standard rotary dial mapping matching main.py logic:
    1..9 pulses -> 1..9, 10 pulses -> 0
    """
    if pulses <= 0:
        return None
    if 1 <= pulses <= 9:
        return pulses
    if pulses == 10:
        return 0
    return None


def print_audio_devices() -> None:
    """Display system audio devices."""
    print("\nVerfügbare Audiogeräte:")
    print(sd.query_devices())
    print("\nDefault-Geräte:", sd.default.device)


def play_tone(duration: float = 2.0, freq: Optional[float] = None) -> None:
    """Play a pure sine wave to test the speaker/handset."""
    freq = freq or AUDIO.tone_freq
    sr = AUDIO.samplerate
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    signal = (AUDIO.tone_volume * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    print(f"Spiele {freq:.0f} Hz für {duration:.1f} s über das Default-Ausgabegerät ...")
    sd.play(signal, samplerate=sr)
    sd.wait()


def test_output() -> None:
    """Execute output test sequence."""
    print_audio_devices()
    play_tone(2.0)
    print("Wenn du den Ton im Hörer gehört hast, ist die Audio-Ausgabe grundsätzlich OK.")


def meter_bar(rms: float, peak: float, width: int = 40) -> str:
    """Generate a visual terminal VU meter."""
    # Roughly display normalized values for 16-bit-float in dBFS
    db = 20 * math.log10(max(rms, 1e-8))
    filled = max(0, min(width, int((db + 60) / 60 * width)))
    return "#" * filled + "." * (width - filled) + f"  RMS {db:6.1f} dBFS  Peak {peak:5.3f}"


def test_microphone(seconds: float = 5.0, playback: bool = True) -> None:
    """Record audio and display live VU meter, followed by optional playback."""
    print_audio_devices()
    print("\nMikrofon-Pegeltest. Sprich in den Hörer. Abbruch mit Strg+C.")
    print("Danach wird optional eine kurze Aufnahme wiedergegeben.\n")

    q: queue.Queue[np.ndarray] = queue.Queue()

    def cb(indata, frames, time_info, status):
        if status:
            print(status, file=sys.stderr)
        q.put(indata.copy())

    recorded = []
    end = time.time() + seconds
    with sd.InputStream(samplerate=AUDIO.samplerate, channels=AUDIO.channels, dtype="float32", callback=cb):
        while time.time() < end:
            try:
                chunk = q.get(timeout=0.5)
            except queue.Empty:
                continue
            recorded.append(chunk)
            rms = float(np.sqrt(np.mean(np.square(chunk))))
            peak = float(np.max(np.abs(chunk)))
            print("\r" + meter_bar(rms, peak), end="", flush=True)
    print("\nAufnahme beendet.")

    if recorded and playback:
        data = np.concatenate(recorded, axis=0)
        print("Spiele Aufnahme zur Kontrolle über den Hörer zurück ...")
        sd.play(data, samplerate=AUDIO.samplerate)
        sd.wait()


def test_handset() -> None:
    """Continuously poll hook switch and print state changes."""
    setup_gpio()
    print(f"\nGabeltest auf GPIO {PINS.handset}. LOW = abgehoben, HIGH = aufgelegt.")
    print("Bitte Hörer mehrmals abheben/auflegen. Abbruch mit Strg+C.\n")
    last = None
    try:
        while True:
            raw = GPIO.input(PINS.handset)
            state = "ABGEHOBEN" if raw == GPIO.LOW else "aufgelegt"
            if state != last:
                print(f"{time.strftime('%H:%M:%S')}  GPIO={raw}  Hörer: {state}")
                last = state
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\nGabeltest beendet.")


def test_rotary(timeout: float = 1.2, min_sep: float = 0.035) -> None:
    """Monitor rotary dial pulses and decode dialed digits."""
    setup_gpio()
    print(f"\nWählscheibentest auf GPIO {PINS.rotary}.")
    print("Wähle nacheinander 1,2,3,4,5,6,7,8,9,0. Abbruch mit Strg+C.\n")

    try:
        try:
            GPIO.remove_event_detect(PINS.rotary)
        except Exception:
            pass

        pulse_count = 0
        last_pulse = 0.0
        first_pulse = 0.0
        sequence = ""

        def on_pulse(channel):
            nonlocal pulse_count, last_pulse, first_pulse
            now = time.time()
            if now - last_pulse >= min_sep:
                if pulse_count == 0:
                    first_pulse = now
                pulse_count += 1
                last_pulse = now
                print(f"  Impuls {pulse_count}")

        GPIO.add_event_detect(PINS.rotary, GPIO.FALLING, callback=on_pulse, bouncetime=1)

        while True:
            if pulse_count and (time.time() - last_pulse) > timeout:
                pulses = pulse_count
                elapsed = last_pulse - first_pulse if pulses > 1 else 0.0
                digit = digit_from_pulses(pulses)
                if digit is None:
                    print(f"=> {pulses} Impulse in {elapsed:.2f}s: ungültig/nicht zugeordnet")
                else:
                    sequence += str(digit)
                    print(f"=> {pulses} Impulse in {elapsed:.2f}s: gewählt = {digit}     Folge: {sequence}")
                pulse_count = 0
                last_pulse = 0.0
                first_pulse = 0.0
            time.sleep(0.01)
    except KeyboardInterrupt:
        print("\nWählscheibentest beendet.")
    finally:
        try:
            GPIO.remove_event_detect(PINS.rotary)
        except Exception:
            pass


def menu(args) -> None:
    """Display interactive CLI menu."""
    while True:
        print("""
Telefon-Hardwaretest
====================
1  Hörer/Lautsprecher testen
2  Mikrofon testen
3  Gabel testen
4  Wählscheibe testen
5  Klingel testen
6  Audiogeräte anzeigen
q  Beenden
""".strip())
        choice = input("Auswahl: ").strip().lower()
        if choice == "1":
            test_output()
        elif choice == "2":
            test_microphone(seconds=args.record_seconds, playback=True)
        elif choice == "3":
            test_handset()
        elif choice == "4":
            test_rotary(timeout=args.rotary_timeout, min_sep=args.min_pulse_separation)
        elif choice == "5":
            test_bell(rings=args.bell_rings, seconds=args.bell_seconds, frequency=args.bell_frequency, pause=args.bell_pause)
        elif choice == "6":
            print_audio_devices()
        elif choice in {"q", "quit", "exit"}:
            break
        else:
            print("Unbekannte Auswahl.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Hardware-Testscript für KI-Telefon")
    parser.add_argument("--handset-pin", type=int, default=PINS.handset)
    parser.add_argument("--rotary-pin", type=int, default=PINS.rotary)
    parser.add_argument("--bell-in1-pin", type=int, default=PINS.bell_in1)
    parser.add_argument("--bell-in2-pin", type=int, default=PINS.bell_in2)
    parser.add_argument("--bell-ena-pin", type=int, default=PINS.bell_ena)
    parser.add_argument("--samplerate", type=int, default=AUDIO.samplerate)
    parser.add_argument("--record-seconds", type=float, default=5.0)
    parser.add_argument("--rotary-timeout", type=float, default=1.2)
    parser.add_argument("--min-pulse-separation", type=float, default=0.035)
    parser.add_argument("--bell-rings", type=int, default=BELL.rings)
    parser.add_argument("--bell-seconds", type=float, default=BELL.seconds)
    parser.add_argument("--bell-frequency", type=float, default=BELL.frequency)
    parser.add_argument("--bell-pause", type=float, default=BELL.pause)
    parser.add_argument("--test", choices=["output", "mic", "handset", "rotary", "bell", "devices"], help="Einzeltest ohne Menü")
    args = parser.parse_args()

    PINS.handset = args.handset_pin
    PINS.rotary = args.rotary_pin
    PINS.bell_in1 = args.bell_in1_pin
    PINS.bell_in2 = args.bell_in2_pin
    PINS.bell_ena = args.bell_ena_pin
    AUDIO.samplerate = args.samplerate
    BELL.rings = args.bell_rings
    BELL.seconds = args.bell_seconds
    BELL.frequency = args.bell_frequency
    BELL.pause = args.bell_pause

    try:
        if args.test == "output":
            test_output()
        elif args.test == "mic":
            test_microphone(seconds=args.record_seconds, playback=True)
        elif args.test == "handset":
            test_handset()
        elif args.test == "rotary":
            test_rotary(timeout=args.rotary_timeout, min_sep=args.min_pulse_separation)
        elif args.test == "bell":
            test_bell(rings=args.bell_rings, seconds=args.bell_seconds, frequency=args.bell_frequency, pause=args.bell_pause)
        elif args.test == "devices":
            print_audio_devices()
        else:
            menu(args)
    finally:
        if GPIO is not None:
            GPIO.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
  
