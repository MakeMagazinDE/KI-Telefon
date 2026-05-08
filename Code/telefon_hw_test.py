#!/usr/bin/env python3
"""
Telefon-Hardwaretest für das Make KI-Telefon / Telekom 611-2 Umbau.

Tests:
  1) Hörer / Lautsprecher: 425-Hz-Ton und optional WAV-Datei abspielen
  2) Mikrofon: Pegelanzeige + kurze Aufnahme mit Playback
  3) Gabel: Abheben/Auflegen live anzeigen
  4) Wählscheibe: Impulse zählen und gewählte Ziffer anzeigen

Pins im Projekt:
  Gabel:      GPIO 5  gegen GND, Pull-Up aktiv, LOW = abgehoben
  Wählscheibe GPIO 26 gegen GND, Pull-Up aktiv, fallende Flanke = Impuls

Benötigt:
  sudo apt install python3-rpi.gpio python3-sounddevice python3-numpy
  # oder im vorhandenen Projekt-venv: pip install sounddevice numpy RPi.GPIO
"""

from __future__ import annotations

import argparse
import math
import os
import queue
import sys
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import sounddevice as sd

try:
    import RPi.GPIO as GPIO
except Exception as exc:  # auf Nicht-Raspberry-Systemen abbrechen
    GPIO = None
    GPIO_IMPORT_ERROR = exc
else:
    GPIO_IMPORT_ERROR = None


@dataclass
class Pins:
    handset: int = 5
    rotary: int = 26


@dataclass
class AudioCfg:
    samplerate: int = 24000
    channels: int = 1
    tone_freq: float = 425.0
    tone_volume: float = 0.15


PINS = Pins()
AUDIO = AudioCfg()


def require_gpio() -> None:
    if GPIO is None:
        raise RuntimeError(f"RPi.GPIO konnte nicht importiert werden: {GPIO_IMPORT_ERROR}")
    if GPIO.getmode() is None:
        GPIO.setmode(GPIO.BCM)


def setup_gpio() -> None:
    require_gpio()
    GPIO.setup(PINS.handset, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(PINS.rotary, GPIO.IN, pull_up_down=GPIO.PUD_UP)


def handset_lifted() -> bool:
    setup_gpio()
    return GPIO.input(PINS.handset) == GPIO.LOW


def digit_from_pulses(pulses: int, mode: str = "project") -> Optional[int]:
    """
    mode='project': kompatibel zum main.py:
        2 Impulse -> 1, ..., 10 Impulse -> 9, 11 Impulse -> 0
    mode='classic': klassische Wählscheibe:
        1 Impuls -> 1, ..., 9 Impulse -> 9, 10 Impulse -> 0
    """
    if pulses <= 0:
        return None
    if mode == "classic":
        return 0 if pulses == 10 else pulses if 1 <= pulses <= 9 else None
    if mode == "project":
        return 0 if pulses == 11 else pulses - 1 if 2 <= pulses <= 10 else None
    raise ValueError("mode muss 'project' oder 'classic' sein")


def print_audio_devices() -> None:
    print("\nVerfügbare Audiogeräte:")
    print(sd.query_devices())
    print("\nDefault-Geräte:", sd.default.device)


def play_tone(duration: float = 2.0, freq: Optional[float] = None) -> None:
    freq = freq or AUDIO.tone_freq
    sr = AUDIO.samplerate
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    signal = (AUDIO.tone_volume * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    print(f"Spiele {freq:.0f} Hz für {duration:.1f} s über das Default-Ausgabegerät ...")
    sd.play(signal, samplerate=sr)
    sd.wait()


def play_wav(path: Path) -> None:
    if not path.exists():
        print(f"WAV-Datei nicht gefunden: {path}")
        return
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        sr = wf.getframerate()
        width = wf.getsampwidth()
        frames = wf.readframes(wf.getnframes())
    dtype_map = {1: np.uint8, 2: np.int16, 4: np.int32}
    if width not in dtype_map:
        print(f"Nicht unterstützte Sample-Breite: {width} Byte")
        return
    data = np.frombuffer(frames, dtype=dtype_map[width])
    if channels > 1:
        data = data.reshape(-1, channels)
    print(f"Spiele WAV: {path.name} ({sr} Hz, {channels} Kanal/Kanäle) ...")
    sd.play(data, samplerate=sr)
    sd.wait()


def test_output(wav: Optional[Path]) -> None:
    print_audio_devices()
    play_tone(2.0)
    if wav:
        play_wav(wav)
    print("Wenn du den Ton im Hörer gehört hast, ist die Audio-Ausgabe grundsätzlich OK.")


def meter_bar(rms: float, peak: float, width: int = 40) -> str:
    # Für 16-bit-float normalisierte Werte grob in dBFS darstellen
    db = 20 * math.log10(max(rms, 1e-8))
    filled = max(0, min(width, int((db + 60) / 60 * width)))
    return "#" * filled + "." * (width - filled) + f"  RMS {db:6.1f} dBFS  Peak {peak:5.3f}"


def test_microphone(seconds: float = 5.0, playback: bool = True) -> None:
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


def test_rotary(timeout: float = 1.2, min_sep: float = 0.035, mode: str = "project") -> None:
    setup_gpio()
    print(f"\nWählscheibentest auf GPIO {PINS.rotary}. Modus: {mode}")
    print("Wähle nacheinander 1,2,3,4,5,6,7,8,9,0. Abbruch mit Strg+C.\n")
    print("Hinweis: Wenn alle Zahlen um 1 verschoben sind, starte mit --rotary-mode classic.")

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
                digit = digit_from_pulses(pulses, mode=mode)
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
    wav = Path(args.wav).expanduser() if args.wav else None
    while True:
        print("""
Telefon-Hardwaretest
====================
1  Hörer/Lautsprecher testen
2  Mikrofon testen
3  Gabel testen
4  Wählscheibe testen
5  Audiogeräte anzeigen
q  Beenden
""".strip())
        choice = input("Auswahl: ").strip().lower()
        if choice == "1":
            test_output(wav)
        elif choice == "2":
            test_microphone(seconds=args.record_seconds, playback=True)
        elif choice == "3":
            test_handset()
        elif choice == "4":
            test_rotary(timeout=args.rotary_timeout, min_sep=args.min_pulse_separation, mode=args.rotary_mode)
        elif choice == "5":
            print_audio_devices()
        elif choice in {"q", "quit", "exit"}:
            break
        else:
            print("Unbekannte Auswahl.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Hardware-Testscript für KI-Telefon")
    parser.add_argument("--handset-pin", type=int, default=PINS.handset)
    parser.add_argument("--rotary-pin", type=int, default=PINS.rotary)
    parser.add_argument("--samplerate", type=int, default=AUDIO.samplerate)
    parser.add_argument("--wav", default="greeting.wav", help="optionale WAV-Datei für Ausgabetest")
    parser.add_argument("--record-seconds", type=float, default=5.0)
    parser.add_argument("--rotary-timeout", type=float, default=1.2)
    parser.add_argument("--min-pulse-separation", type=float, default=0.035)
    parser.add_argument("--rotary-mode", choices=["project", "classic"], default="project")
    parser.add_argument("--test", choices=["output", "mic", "handset", "rotary", "devices"], help="Einzeltest ohne Menü")
    args = parser.parse_args()

    PINS.handset = args.handset_pin
    PINS.rotary = args.rotary_pin
    AUDIO.samplerate = args.samplerate

    # Relative WAV-Datei neben dem Script suchen
    if args.wav and not os.path.isabs(args.wav):
        candidate = Path.cwd() / args.wav
        if not candidate.exists():
            candidate = Path(__file__).resolve().parent / args.wav
        args.wav = str(candidate)

    try:
        if args.test == "output":
            test_output(Path(args.wav) if args.wav else None)
        elif args.test == "mic":
            test_microphone(seconds=args.record_seconds, playback=True)
        elif args.test == "handset":
            test_handset()
        elif args.test == "rotary":
            test_rotary(timeout=args.rotary_timeout, min_sep=args.min_pulse_separation, mode=args.rotary_mode)
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
