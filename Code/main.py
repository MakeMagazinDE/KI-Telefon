import queue
import sys

# Setup Logging side-effects (redirects stdout/stderr to logfile)
from logging_setup import setup_logging
setup_logging()

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

# Debug toggle for console output
DEBUG = False


def debug_print(*args, **kwargs):
    """Print only if DEBUG is True."""
    if DEBUG:
        print(*args, **kwargs)


# Audio parameters
CHUNK_SIZE = 2048
RATE = 24000
FORMAT = pyaudio.paInt16

# Global audio buffers & flags
audio_buffer = bytearray()
audio_lock = threading.Lock()
mic_queue = queue.Queue()
stop_event = threading.Event()
hardware_muted = False  # Instant acoustic cut-off flag

# Hangup handling
HANDSET_HANGUP_DEBOUNCE_S = 0.5  # 500ms debounce for agile hook detection

# Rotary dial parameters
PULSE_PIN = 26
GPIO.setup(PULSE_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
FREQ_425HZ = 425
AUTOCALL_DELAY = 30  # Seconds until next call if nobody picks up


def mic_callback(in_data, frame_count, time_info, status):
    """Forward microphone audio continuously."""
    # Instant hardware mute if hook is pressed
    if hardware_muted:
        in_data = b'\x00' * len(in_data)
        
    mic_queue.put(in_data)
    return (None, pyaudio.paContinue)


def speaker_callback(in_data, frame_count, time_info, status):
    """Play audio received from OpenAI."""
    global audio_buffer
    bytes_needed = frame_count * 2

    with audio_lock:
        available = len(audio_buffer)

        if available >= bytes_needed:
            chunk = bytes(audio_buffer[:bytes_needed])
            audio_buffer[:] = audio_buffer[bytes_needed:]
        elif available > 0:
            chunk = bytes(audio_buffer) + b'\x00' * (bytes_needed - available)
            audio_buffer.clear()
        else:
            chunk = b'\x00' * bytes_needed

    # Instant hardware mute: replace valid audio chunk with silence
    if hardware_muted:
        chunk = b'\x00' * bytes_needed

    return (chunk, pyaudio.paContinue)


def monitor_handset(stop_event):
    """Monitor hook switch and end the active call after a stable hangup."""
    global hardware_muted
    time.sleep(0.5)
    down_since = None

    while not stop_event.is_set():
        now = time.time()
        lifted = is_handset_lifted()

        if not lifted:
            hardware_muted = True  # Instantly mute speaker and mic
            
            if down_since is None:
                down_since = now
                debug_print("Hörerstatus: aufgelegt erkannt – starte Debounce.")
            elif now - down_since >= HANDSET_HANGUP_DEBOUNCE_S:
                time.sleep(0.1)  # Brief final confirmation check
                if is_handset_lifted():
                    debug_print(f"Hörerstatus wieder abgehoben nach {time.time() - down_since:.2f}s – ignoriere Drop.")
                    down_since = None
                    continue
                print(f"Hörer aufgelegt seit {time.time() - down_since:.2f}s – beende Gespräch...")
                stop_event.set()
                break
        else:
            hardware_muted = False  # Instantly restore audio
            
            if down_since is not None:
                debug_print(f"Hörerstatus wieder abgehoben nach {now - down_since:.2f}s – ignoriere kurzen Drop.")
                down_since = None

        time.sleep(0.05)


# --- Non-blocking dial tone (425 Hz) ---
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
    debug_print("Freizeichen gestartet.")


def stop_dial_tone():
    global dial_tone_stream
    if dial_tone_stream is not None:
        try:
            dial_tone_stream.stop()
            dial_tone_stream.close()
        finally:
            dial_tone_stream = None
            debug_print("Freizeichen gestoppt.")


def play_shutdown_signal():
    """Acoustic confirmation for shutdown sequence."""
    fs = RATE
    sequence = [
        (880, 0.18),
        (0, 0.08),
        (660, 0.18),
        (0, 0.08),
        (440, 0.35),
    ]
    signal_parts = []
    for freq, duration in sequence:
        samples = int(fs * duration)
        if freq == 0:
            signal_parts.append(np.zeros(samples, dtype=np.float32))
        else:
            t = np.linspace(0, duration, samples, endpoint=False)
            signal_parts.append((0.18 * np.sin(2 * np.pi * freq * t)).astype(np.float32))
    sd.play(np.concatenate(signal_parts), samplerate=fs)
    sd.wait()


def play_busy_signal(duration=4.0):
    """Play a standard busy signal (425 Hz, fast toggling)."""
    fs = RATE
    t_on = np.linspace(0, 0.5, int(fs * 0.5), endpoint=False)
    t_off = np.zeros(int(fs * 0.5), dtype=np.float32)
    signal_on = (0.1 * np.sin(2 * np.pi * FREQ_425HZ * t_on)).astype(np.float32)

    signal_parts = []
    for _ in range(int(duration)):
        signal_parts.append(signal_on)
        signal_parts.append(t_off)

    if signal_parts:
        sd.play(np.concatenate(signal_parts), samplerate=fs)
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
                debug_print("Wählscheibe aktiv – Impulse werden gezählt.")
            debug_print(f"Impuls erkannt! Gesamt: {pulse_count}")

    # Prevent duplicate setup errors by relying on the global initialization
    try:
        GPIO.remove_event_detect(PULSE_PIN)
    except Exception:
        pass

    GPIO.add_event_detect(PULSE_PIN, GPIO.FALLING, callback=pulse_callback)
    try:
        while True:
            if not is_handset_lifted():
                print("Hörer aufgelegt – Wählen abgebrochen.")
                stop_dial_tone()
                return None

            if first_seen[0] and (time.time() - last_pulse_time[0]) > timeout:
                break
            time.sleep(0.005)
    finally:
        try:
            GPIO.remove_event_detect(PULSE_PIN)
        except Exception:
            pass

    if pulse_count == 10:
        digit = 0
    elif 1 <= pulse_count <= 9:
        digit = pulse_count
    else:
        print(f"Ungültige Impulszahl: {pulse_count}")
        return None

    print(f"Gewählte Ziffer: {digit}")
    return digit


def play_freitone(repeats=3, tone_dur=1.0, pause_dur=4.0, freq=425):
    """Plays ringback tone, but aborts immediately if handset is placed on hook."""
    fs = RATE
    for i in range(repeats):
        if not is_handset_lifted():
            return False

        debug_print(f"Freiton {i+1}/{repeats}: Ton...")
        t = np.linspace(0, tone_dur, int(fs * tone_dur), endpoint=False)
        signal = 0.1 * np.sin(2 * np.pi * freq * t)
        sd.play(signal, samplerate=fs)

        # Non-blocking wait for tone to finish
        start_tone = time.time()
        while time.time() - start_tone < tone_dur:
            if not is_handset_lifted():
                sd.stop()
                return False
            time.sleep(0.05)

        if i < repeats - 1:
            debug_print("Pause...")
            # Non-blocking wait during the silence between rings
            start_pause = time.time()
            while time.time() - start_pause < pause_dur:
                if not is_handset_lifted():
                    return False
                time.sleep(0.05)

    debug_print("Freiton beendet – KI übernimmt nun.")
    return True


def wait_for_role_selection():
    print("Hörer abheben, um Rolle auszuwählen...")
    while not is_handset_lifted():
        time.sleep(0.05)

    print("Hörer abgehoben – Freizeichen aktiv. Bitte wählen...")
    start_dial_tone()
    role_number = read_rotary_wheel(timeout=1.5)

    if role_number is None:
        print("Keine Wahl / Wahl abgebrochen.")
        stop_dial_tone()
        return None

    print(f"Gewählte Nummer: {role_number}")

    if role_number == 0:
        print("Shutdown-Sequenz wird ausgeführt...")
        stop_dial_tone()
        play_shutdown_signal()
        os.system("sudo shutdown now")
        sys.exit(0) # Terminate Python to prevent unintended loops during shutdown

    if not is_handset_lifted():
        print("Hörer aufgelegt – Anruf abgebrochen.")
        stop_dial_tone()
        return None

    print("Teilnehmer wird jetzt angerufen!")
    stop_dial_tone()

    # Cancel if play_freitone returns False (handset hung up during rings)
    if not play_freitone():
        print("Hörer während Freiton aufgelegt – Anruf abgebrochen.")
        return None

    return role_number


def run_conversation(selected_role=None, is_outgoing=False):
    global audio_buffer, mic_queue, stop_event, hardware_muted
    stop_event.clear()
    hardware_muted = False

    with audio_lock:
        audio_buffer.clear()

    while not mic_queue.empty():
        mic_queue.get()

    p = None
    mic_stream = None
    speaker_stream = None
    connection_failed = False

    try:
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
        debug_print(f"Stil: {role[0]['gpt_style']}")

        mic_stream.start_stream()
        speaker_stream.start_stream()
        monitor_thread = threading.Thread(target=monitor_handset, args=(stop_event,), daemon=True)
        monitor_thread.start()
        
        connect_to_openai(
            mic_queue,
            audio_buffer,
            stop_event,
            role,
            gespraechspartner,
            send_initial_prompt=is_outgoing,
            audio_lock=audio_lock,
        )
        monitor_thread.join(timeout=2)
        
    except KeyboardInterrupt:
        print('Beenden...')
        stop_event.set()
    except Exception as e:
        connection_failed = True
        print(f"Gesprächsfehler: {e}")
        stop_event.set()
    finally:
        if mic_stream is not None:
            try:
                mic_stream.stop_stream()
                mic_stream.close()
            except Exception:
                pass
        if speaker_stream is not None:
            try:
                speaker_stream.stop_stream()
                speaker_stream.close()
            except Exception:
                pass
        if p is not None:
            try:
                p.terminate()
            except Exception:
                pass
                
        print('Audio gestoppt – Gespräch beendet.')

        # If the session ended or disconnected unexpectedly, but the handset is still off-hook
        if (stop_event.is_set() or connection_failed) and is_handset_lifted():
            print("Verbindung getrennt. Spiele Besetztzeichen...")
            play_busy_signal()
            print("Warte auf physisches Auflegen durch den Benutzer...")
            # Enforce disconnection line block until handset is back on hook
            while is_handset_lifted():
                time.sleep(0.1)


def main():
    setup()

    try:
        if not is_handset_lifted():
            print("Hörer aufgelegt – erstes Klingeln...")
            if ring_until_answer(5):
                print("Abgehoben – KI verbunden (eingehend).")
                run_conversation(is_outgoing=False)
            else:
                print("Niemand hat abgehoben – wechsle in Wartephase.")
        else:
            print("Hörer bereits abgehoben – kein erstes Klingeln.")

        next_ring_at = time.time() + AUTOCALL_DELAY
        last_wait_log_at = 0

        while True:
            handset_lifted = is_handset_lifted()

            if handset_lifted:
                print("Hörer in Wartezeit abgehoben – ausgehender Anruf via Dialer.")
                role_number = wait_for_role_selection()
                if role_number is not None:
                    run_conversation(selected_role=role_number, is_outgoing=True)
                next_ring_at = time.time() + AUTOCALL_DELAY
                continue

            now = time.time()

            if now >= next_ring_at:
                print("Wartezeit abgelaufen – starte erneutes Klingeln.")
                if ring_until_answer(5):
                    print("Abgehoben – KI verbunden (eingehend).")
                    run_conversation(is_outgoing=False)
                else:
                    print("Wieder nicht abgehoben – Wartezeit startet neu.")
                next_ring_at = time.time() + AUTOCALL_DELAY
            else:
                if now - last_wait_log_at >= 5:
                    remaining = max(0, int(next_ring_at - now))
                    debug_print(f"Hörer aufgelegt – warte noch {remaining}s bis zum nächsten Klingeln.")
                    last_wait_log_at = now

                time.sleep(0.1)
                
    except KeyboardInterrupt:
        print("\nProgramm durch Benutzer beendet.")
    finally:
        print("Räume GPIO-Pins auf...")
        GPIO.cleanup()


if __name__ == "__main__":
    main()
    
