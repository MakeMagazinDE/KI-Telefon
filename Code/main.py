import queue
import threading
import time
import pyaudio
import RPi.GPIO as GPIO
GPIO.setmode(GPIO.BCM)
import sounddevice as sd
import numpy as np
import os

from roles import choose_role, role as role_list
from openai_ws import connect_to_openai
from bell import ring_until_answer
from handset import setup, is_handset_lifted
from pathlib import Path

# Projekt Parameter
PROJECT_DIR = Path(__file__).resolve().parent
GREETING_WAV_PATH = PROJECT_DIR / "greeting.wav"

# Audio-Parameter
CHUNK_SIZE = 2048
RATE = 24000
FORMAT = pyaudio.paInt16

# Globale Audio-Puffer & Flags
audio_buffer = bytearray()
mic_queue = queue.Queue()
stop_event = threading.Event()

# Wählscheiben-Parameter
PULSE_PIN = 26
GPIO.setup(PULSE_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
FREQ_425HZ = 425
FREQ_DURATION = 5
NUM_FREIZEICHEN = 3
FREIZEICHEN_DELAY = 0.5
AUTOCALL_DELAY = 30  # Sekunden bis zum nächsten Klingeln, wenn niemand abnimmt

# Mikrofon-Callback
def mic_callback(in_data, frame_count, time_info, status):
    mic_queue.put(in_data)
    return (None, pyaudio.paContinue)

# Lautsprecher-Callback
REENGAGE_DELAY_MS = 500
mic_on_at = 0

def speaker_callback(in_data, frame_count, time_info, status):
    global audio_buffer, mic_on_at
    bytes_needed = frame_count * 2
    if len(audio_buffer) >= bytes_needed:
        chunk = bytes(audio_buffer[:bytes_needed])
        audio_buffer[:] = audio_buffer[bytes_needed:]
        mic_on_at = time.time() + REENGAGE_DELAY_MS / 1000
    else:
        chunk = bytes(audio_buffer) + b'\x00' * (bytes_needed - len(audio_buffer))
        audio_buffer.clear()
    return (chunk, pyaudio.paContinue)

def monitor_handset(stop_event):
    time.sleep(0.5)
    while not stop_event.is_set():
        if not is_handset_lifted():  # Hörer wurde aufgelegt
            print("Hörer aufgelegt – beende Gespräch...")
            stop_event.set()
            break
        time.sleep(0.05)
        
# --- Non-blocking Freizeichen (425 Hz) ---
dial_tone_stream = None

def start_dial_tone():
    global dial_tone_stream
    if dial_tone_stream is not None:
        return
    fs = RATE
    freq = FREQ_425HZ
    step = 2 * np.pi * freq / fs
    phase = {'phi': 0.0}
    def callback(outdata, frames, time_info, status):
        phi0 = phase['phi']
        t = phi0 + step * np.arange(frames, dtype=np.float32)
        samples = (0.1 * np.sin(t)).astype(np.float32)
        outdata[:] = samples.reshape(-1, 1)
        phase['phi'] = (phi0 + frames * step) % (2 * np.pi)
    dial_tone_stream = sd.OutputStream(
        samplerate=fs,
        channels=1,
        dtype='float32',
        callback=callback
    )
    dial_tone_stream.start()
    print("Freizeichen gestartet.")

def stop_dial_tone():
    global dial_tone_stream
    if dial_tone_stream is not None:
        try:
            dial_tone_stream.stop()
            dial_tone_stream.close()
        finally:
            dial_tone_stream = None
            print("Freizeichen gestoppt.")

def play_425hz(duration=FREQ_DURATION):
    fs = 24000
    t = np.linspace(0, duration, int(fs*duration), endpoint=False)
    signal = 0.1 * np.sin(2 * np.pi * FREQ_425HZ * t)
    sd.play(signal, samplerate=fs)
    sd.wait()

def read_rotary_wheel(timeout=1.5):
    pulse_count = 0
    last_pulse_time = [0.0]
    first_seen = [False]
    MIN_PULSE_SEPARATION = 0.05
    def pulse_callback(channel):
        nonlocal pulse_count
        now = time.time()
        if now - last_pulse_time[0] > MIN_PULSE_SEPARATION:
            pulse_count += 1
            last_pulse_time[0] = now
            if not first_seen[0]:
                first_seen[0] = True
                stop_dial_tone()
                print("Wählscheibe aktiv – Impulse werden gezählt.")
            print(f"Impuls erkannt! Gesamt: {pulse_count}")
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(PULSE_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    try:
        GPIO.remove_event_detect(PULSE_PIN)
    except Exception:
        pass
    GPIO.add_event_detect(PULSE_PIN, GPIO.FALLING, callback=pulse_callback)
    while True:
        if first_seen[0] and (time.time() - last_pulse_time[0]) > timeout:
            break
        time.sleep(0.005)
    GPIO.remove_event_detect(PULSE_PIN)
    if pulse_count == 0:
        print("Keine Wahl erkannt.")
        return None
    digit = 0 if pulse_count == 11 else pulse_count - 1
    print(f"Gewählte Ziffer: {digit}")
    return digit

def play_freitone(repeats=3, tone_dur=1.0, pause_dur=4.0, freq=425):
    fs = RATE
    for i in range(repeats):
        print(f"Freiton {i+1}/{repeats}: Ton...")
        t = np.linspace(0, tone_dur, int(fs * tone_dur), endpoint=False)
        signal = 0.1 * np.sin(2 * np.pi * freq * t)
        sd.play(signal, samplerate=fs)
        sd.wait()
        if i < repeats - 1:        
            print("Pause...")
            time.sleep(pause_dur)
    print("Freiton beendet – KI übernimmt nun.")

# Angepasst
def wait_for_role_selection():
    print("Hörer abheben, um Rolle auszuwählen...")
    while not is_handset_lifted():
        time.sleep(0.05)
    print("Hörer abgehoben – Freizeichen aktiv. Bitte wählen...")
    start_dial_tone()
    role_number = read_rotary_wheel(timeout=1.5)
    print(f"Gewählte Nummer: {role_number}")
    if role_number == 0:
        print("Shutdown-Sequenz wird ausgeführt...")
        stop_dial_tone()
        play_425hz(1)
        os.system("sudo shutdown now")
        return None
    print("Teilnehmer wird jetzt angerufen!")
    play_freitone()
    return role_number

# Angepasst
def run_conversation(selected_role=None, greeting=False):
    global audio_buffer, mic_queue, stop_event
    stop_event.clear()
    audio_buffer.clear()
    while not mic_queue.empty():
        mic_queue.get()
    p = pyaudio.PyAudio()
    mic_stream = p.open(
        format=FORMAT,
        channels=1,
        rate=RATE,
        input=True,
        stream_callback=mic_callback,
        frames_per_buffer=CHUNK_SIZE
    )
    speaker_stream = p.open(
        format=FORMAT,
        channels=1,
        rate=RATE,
        output=True,
        stream_callback=speaker_callback,
        frames_per_buffer=CHUNK_SIZE
    )
    if selected_role is None:
        role = [choose_role()]
    else:
        if 1 <= selected_role <= len(role_list):
            role = [role_list[selected_role - 1]]
        else:
            print("Ungültige Nummer – wähle zufällig.")
            role = [choose_role()]
    gespraechspartner = [None]
    print(f"Rolle: {role[0]['name']}")
    print(f"Stil: {role[0]['gpt_style']}")
    try:
        mic_stream.start_stream()
        speaker_stream.start_stream()
        monitor_thread = threading.Thread(target=monitor_handset, args=(stop_event,))
        monitor_thread.start()
        connect_to_openai(
            mic_queue,
            audio_buffer,
            stop_event,
            role,
            gespraechspartner,
            greeting=str(GREETING_WAV_PATH) if greeting else None
        )
        monitor_thread.join()
    except KeyboardInterrupt:
        print('Beenden...')
        stop_event.set()
    finally:
        mic_stream.stop_stream()
        mic_stream.close()
        speaker_stream.stop_stream()
        speaker_stream.close()
        p.terminate()
        print('Audio gestoppt – Gespräch beendet.')

def main():
    setup()
    print("Erstes Klingeln...")
    if ring_until_answer(5):
        print("Abgehoben – KI verbunden (eingehend).")
        run_conversation(greeting=False)
    else:
        print("Niemand hat abgehoben – wechsle in Wartephase.")
    next_ring_at = time.time() + AUTOCALL_DELAY
    while True:
        if is_handset_lifted():
            print("Hörer in Wartezeit abgehoben – ausgehender Anruf via Dialer.")
            role_number = wait_for_role_selection()
            if role_number is not None:
                run_conversation(selected_role=role_number, greeting=True)
            next_ring_at = time.time() + AUTOCALL_DELAY
            continue
        now = time.time()
        if now >= next_ring_at:
            print("Wartezeit abgelaufen – starte erneutes Klingeln.")
            if ring_until_answer(5):
                print("Abgehoben – KI verbunden (eingehend).")
                run_conversation(greeting=False)
            else:
                print("Wieder nicht abgehoben – Wartezeit startet neu.")
            next_ring_at = time.time() + AUTOCALL_DELAY
        time.sleep(0.1)

if __name__ == "__main__":
    main()
