import base64
import json
import socket
import ssl
import threading
import time
import traceback
import websocket
import socks
import wave

from config import OPENAI_API_KEY
from gespraechspartner import personen_info

# Proxy / IPv4 wie im Originalprojekt beibehalten
socket.socket = socks.socksocket

# Aktuelle Realtime-GA API, Stand 2026-05
# Doku: wss://api.openai.com/v1/realtime?model=gpt-realtime-2
REALTIME_MODEL = "gpt-realtime-2"
WS_URL = f"wss://api.openai.com/v1/realtime?model={REALTIME_MODEL}"
API_KEY = OPENAI_API_KEY

REENGAGE_DELAY_MS = 500
DEBUG_EVENTS = True


def create_connection_with_ipv4(*args, **kwargs):
    """WebSocket-Verbindung erzwingen über IPv4."""
    original_getaddrinfo = socket.getaddrinfo

    def getaddrinfo_ipv4(host, port, family=socket.AF_INET, *args):
        return original_getaddrinfo(host, port, socket.AF_INET, *args)

    socket.getaddrinfo = getaddrinfo_ipv4
    try:
        return websocket.create_connection(*args, **kwargs)
    finally:
        socket.getaddrinfo = original_getaddrinfo


def ws_is_closed(ws):
    """Kompatibel mit websocket-client: prüft, ob der Socket noch verbunden ist."""
    try:
        return not bool(ws.connected)
    except Exception:
        return True


def send_json(ws, payload, label="event"):
    if ws_is_closed(ws):
        raise websocket.WebSocketConnectionClosedException(f"WebSocket bereits geschlossen vor {label}")
    ws.send(json.dumps(payload))
    if DEBUG_EVENTS and payload.get("type") not in ("input_audio_buffer.append",):
        print(f"OPENAI TX {label}: {json.dumps(payload, ensure_ascii=False)[:1200]}")


def send_mic_audio_to_websocket(ws, mic_queue, stop_event):
    """Mikrofondaten an OpenAI WebSocket senden."""
    try:
        while not stop_event.is_set():
            if ws_is_closed(ws):
                print("WebSocket geschlossen – Mikrofon-Sende-Thread stoppt.")
                stop_event.set()
                break

            try:
                mic_chunk = mic_queue.get(timeout=0.1)
            except Exception:
                continue

            encoded_chunk = base64.b64encode(mic_chunk).decode("ascii")
            message = {
                "type": "input_audio_buffer.append",
                "audio": encoded_chunk,
            }
            try:
                send_json(ws, message, "mic_audio")
            except Exception as e:
                print(f"Fehler beim Senden von Mikrofon-Audio: {e}")
                stop_event.set()
                break
    except Exception as e:
        print(f"Mikrofon-Thread-Fehler: {e}")
        traceback.print_exc()
    finally:
        print("Mikrofon-Sende-Thread beendet")


def receive_audio_from_websocket(ws, audio_buffer, stop_event, gespraechspartner_ref, role_ref):
    """Audio und Events vom OpenAI WebSocket empfangen."""
    try:
        while not stop_event.is_set():
            try:
                message = ws.recv()
            except websocket.WebSocketConnectionClosedException as e:
                print(f"OpenAI WebSocket geschlossen: {e}")
                stop_event.set()
                break
            except Exception as e:
                print(f"Fehler beim Empfangen vom OpenAI WebSocket: {e}")
                stop_event.set()
                break

            if not message:
                print("Leere Nachricht / WebSocket beendet")
                stop_event.set()
                break

            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                print(f"Ungültige JSON-Nachricht empfangen: {message[:500]}")
                continue

            event_type = data.get("type", "")

            # Kritisch: Fehlerereignisse sichtbar ausgeben, sonst sieht man nur Folgefehler im Sendeloop.
            if event_type == "error":
                print("OPENAI ERROR:", json.dumps(data, ensure_ascii=False, indent=2))
                stop_event.set()
                break

            if DEBUG_EVENTS and event_type not in (
                "response.audio.delta",
                "response.output_audio.delta",
                "rate_limits.updated",
            ):
                print("OPENAI RX:", json.dumps(data, ensure_ascii=False)[:1500])

            if event_type in ("session.created", "session.updated"):
                # session.created: initiales Update senden; session.updated nur loggen.
                if event_type == "session.created":
                    send_fc_session_update(ws, gespraechspartner_ref, role_ref)

            elif event_type in ("response.audio.delta", "response.output_audio.delta"):
                delta = data.get("delta")
                if delta:
                    audio_chunk = base64.b64decode(delta)
                    audio_buffer.extend(audio_chunk)

            elif event_type == "input_audio_buffer.speech_started":
                audio_buffer.clear()

            elif event_type in ("response.audio_transcript.done", "response.output_audio_transcript.done"):
                transcript = data.get("transcript", "")
                print(f"Transkript: {transcript}")
                if not gespraechspartner_ref[0]:
                    for name in personen_info.keys():
                        if name.lower() in transcript.lower():
                            gespraechspartner_ref[0] = {
                                "name": name,
                                **personen_info[name]
                            }
                            print(f"Gesprächspartner erkannt: {gespraechspartner_ref[0]}")
                            send_fc_session_update(ws, gespraechspartner_ref, role_ref)
                            break
    except Exception as e:
        print(f"Empfangs-Thread-Fehler: {e}")
        traceback.print_exc()
        stop_event.set()
    finally:
        print("Empfangs-Thread beendet")


def send_fc_session_update(ws, gespraechspartner_ref, role_ref):
    """Session-Parameter an OpenAI senden. Aktualisiert auf Realtime-GA Shape."""
    gespraechspartner = gespraechspartner_ref[0]
    role = role_ref[0]
    extra_info = ""
    if gespraechspartner:
        extra_info = (
            f"Der Gesprächspartner heißt {gespraechspartner['name']}. "
            f"Er/Sie ist {gespraechspartner['alter']} Jahre alt, arbeitet als {gespraechspartner['beruf']} "
            f"und hat als Hobby {gespraechspartner['hobby']}. "
        )

    instructions = f"{extra_info}Du bist {role['gpt_style']}"

    session_config = {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "model": REALTIME_MODEL,
            "instructions": instructions,
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {
                        "type": "audio/pcm",
                        "rate": 24000,
                    },
                    # server_vad ist für dein Telefonprojekt besser als manuelles committen.
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.5,
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": 500,
                    },
                },
                "output": {
                    "format": {
                        "type": "audio/pcm",
                        "rate": 24000,
                    },
                    "voice": role["voice_id"],
                },
            },
            # Realtime 2 kann reasoning; niedrig halten, damit Telefonlatenz klein bleibt.
            "reasoning": {
                "effort": "low"
            },
        },
    }

    try:
        send_json(ws, session_config, "session.update")
        print("Session-Update gesendet")
    except Exception as e:
        print(f"Session-Update fehlgeschlagen: {e}")
        raise


def inject_greeting_audio(ws, wav_path):
    """Schickt WAV-Datei als Input-Audio an KI."""
    try:
        with wave.open(wav_path, "rb") as wf:
            if wf.getsampwidth() != 2:
                raise ValueError("Greeting muss PCM16 (16-bit) sein.")
            if wf.getframerate() != 24000:
                raise ValueError("Greeting muss 24000 Hz haben.")
            if wf.getnchannels() != 1:
                raise ValueError("Greeting muss mono sein.")
            if wf.getcomptype() != "NONE":
                raise ValueError("Greeting darf nicht komprimiert sein.")

            chunk_duration = 0.1
            frames_per_chunk = int(24000 * chunk_duration)

            while True:
                if ws_is_closed(ws):
                    raise websocket.WebSocketConnectionClosedException("WebSocket beim Greeting geschlossen")
                data = wf.readframes(frames_per_chunk)
                if not data:
                    break

                encoded_chunk = base64.b64encode(data).decode("ascii")
                send_json(ws, {
                    "type": "input_audio_buffer.append",
                    "audio": encoded_chunk,
                }, "greeting_audio")
                time.sleep(chunk_duration)

        send_json(ws, {"type": "input_audio_buffer.commit"}, "input_audio_buffer.commit")
        send_json(ws, {"type": "response.create"}, "response.create")
        print("Greeting-Audio an KI gesendet")

    except Exception as e:
        print(f"Konnte Greeting nicht injizieren: {e}")
        raise


def connect_to_openai(mic_queue, audio_buffer, stop_event, role, gespraechspartner, greeting=None):
    """Startet die Verbindung zu OpenAI und steuert Sende- & Empfangs-Threads."""
    ws = None
    recv_thread = None
    send_thread = None
    try:
        if not API_KEY or API_KEY == "HIER OPENAI API KEY EINTRAGEN":
            raise RuntimeError("OPENAI_API_KEY ist nicht gesetzt / noch Platzhalter in config.py")

        ws = create_connection_with_ipv4(
            WS_URL,
            header=[
                f"Authorization: Bearer {API_KEY}",
                # GA: OpenAI-Beta: realtime=v1 NICHT mehr senden.
                "OpenAI-Safety-Identifier: ki-telefon-local",
            ],
            sslopt={"cert_reqs": ssl.CERT_REQUIRED},
            timeout=10,
        )
        print(f"Mit OpenAI WebSocket verbunden: {WS_URL}")

        recv_thread = threading.Thread(
            target=receive_audio_from_websocket,
            args=(ws, audio_buffer, stop_event, gespraechspartner, role),
            daemon=True,
        )
        send_thread = threading.Thread(
            target=send_mic_audio_to_websocket,
            args=(ws, mic_queue, stop_event),
            daemon=True,
        )

        recv_thread.start()
        send_thread.start()

        # Falls Greeting gesetzt -> WAV als Input schicken. Session-Update kommt nach session.created.
        if greeting:
            # Kurz warten, damit session.created/session.update durchlaufen kann.
            time.sleep(0.8)
            if not stop_event.is_set():
                print(f"Starte Greeting (.wav) für KI: {greeting}")
                inject_greeting_audio(ws, greeting)

        while not stop_event.is_set():
            time.sleep(0.1)

        try:
            if ws and not ws_is_closed(ws):
                ws.send_close()
        except Exception:
            pass

        if recv_thread:
            recv_thread.join(timeout=2)
        if send_thread:
            send_thread.join(timeout=2)
        print("Verbindung geschlossen")

    except Exception as e:
        print(f"Verbindung fehlgeschlagen: {e}")
        traceback.print_exc()
        stop_event.set()
    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass
