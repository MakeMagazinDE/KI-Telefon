import base64
import json
import queue
import socket
import ssl
import threading
import time
import traceback
import websocket

from config import OPENAI_API_KEY
from gespraechspartner import personen_info

REALTIME_MODEL = "gpt-realtime-2"
WS_URL = f"wss://api.openai.com/v1/realtime?model={REALTIME_MODEL}"
API_KEY = OPENAI_API_KEY

# Keep normal logs concise. Enable temporarily if raw Realtime events are needed again.
DEBUG_OPENAI_EVENTS = False
INPUT_TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe"

# Server-VAD tuning for the telephone handset.
VAD_THRESHOLD = 0.35
VAD_PREFIX_PADDING_MS = 500
VAD_SILENCE_DURATION_MS = 650

# Concise runtime heartbeat interval
MIC_SEND_REPORT_INTERVAL_S = 5.0

# Prevent WebSocket Race Conditions between Threads
WS_SEND_LOCK = threading.Lock()


def create_connection_with_ipv4(*args, **kwargs):
    """Force WebSocket connection via IPv4."""
    original_getaddrinfo = socket.getaddrinfo

    def getaddrinfo_ipv4(host, port, family=socket.AF_INET, *args):
        return original_getaddrinfo(host, port, socket.AF_INET, *args)

    socket.getaddrinfo = getaddrinfo_ipv4
    try:
        return websocket.create_connection(*args, **kwargs)
    finally:
        socket.getaddrinfo = original_getaddrinfo


def ws_is_closed(ws):
    """Check if the socket is still connected."""
    try:
        return not bool(ws.connected)
    except Exception:
        return True


def send_json(ws, payload, label="event"):
    """Thread-safe WebSocket JSON send."""
    if ws_is_closed(ws):
        raise websocket.WebSocketConnectionClosedException(f"WebSocket bereits geschlossen vor {label}")
    
    payload_str = json.dumps(payload)
    
    # Lock prevents overlapping frame writes from recv/send threads
    with WS_SEND_LOCK:
        ws.send(payload_str)
        
    if DEBUG_OPENAI_EVENTS and payload.get("type") not in ("input_audio_buffer.append",):
        print(f"OPENAI TX {label}: {json.dumps(payload, ensure_ascii=False)[:1200]}")


def drain_mic_queue(mic_queue):
    """Drop microphone chunks recorded while session was not ready yet."""
    dropped = 0
    try:
        while True:
            mic_queue.get_nowait()
            dropped += 1
    except queue.Empty:
        pass
    if dropped:
        print(f"Mikrofon-Queue geleert: {dropped} alte Chunks verworfen")


def send_mic_audio_to_websocket(ws, mic_queue, stop_event, greeting_done):
    """Send microphone data to OpenAI WebSocket after session/initial prompt are ready."""
    try:
        while not stop_event.is_set():
            if greeting_done.wait(timeout=0.1):
                break

        if stop_event.is_set():
            return

        print("Mikrofon-Sende-Thread freigegeben")
        sent_chunks = 0
        sent_bytes = 0
        last_report = time.time()

        while not stop_event.is_set():
            if ws_is_closed(ws):
                print("WebSocket geschlossen – Mikrofon-Sende-Thread stoppt.")
                stop_event.set()
                break

            try:
                mic_chunk = mic_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            encoded_chunk = base64.b64encode(mic_chunk).decode("ascii")
            message = {
                "type": "input_audio_buffer.append",
                "audio": encoded_chunk,
            }
            try:
                send_json(ws, message, "mic_audio")
                sent_chunks += 1
                sent_bytes += len(mic_chunk)
                now = time.time()
                if now - last_report >= MIC_SEND_REPORT_INTERVAL_S:
                    last_report = now
                    print(
                        f"Mikrofon-Live-Send OK: chunks={sent_chunks} "
                        f"bytes={sent_bytes} queue={mic_queue.qsize()}"
                    )
            except Exception as e:
                print(f"Fehler beim Senden von Mikrofon-Audio: {e}")
                stop_event.set()
                break
    except Exception as e:
        print(f"Mikrofon-Thread-Fehler: {e}")
        traceback.print_exc()
    finally:
        print("Mikrofon-Sende-Thread beendet")


def update_detected_person_from_transcript(transcript, ws, gespraechspartner_ref, role_ref):
    """Detect a configured conversation partner name in user input transcription."""
    if not transcript or gespraechspartner_ref[0]:
        return

    transcript_lower = transcript.lower()
    for name, info in personen_info.items():
        if name.lower() in transcript_lower:
            gespraechspartner_ref[0] = {
                "name": name,
                **info,
            }
            print(f"Gesprächspartner erkannt: {gespraechspartner_ref[0]}")
            send_session_update(ws, gespraechspartner_ref, role_ref, initial=False)
            break


def receive_audio_from_websocket(ws, audio_buffer, stop_event, gespraechspartner_ref, role_ref, session_ready, audio_lock):
    """Receive audio and events from OpenAI WebSocket."""
    try:
        while not stop_event.is_set():
            try:
                message = ws.recv()
            except (websocket.WebSocketTimeoutException, socket.timeout, TimeoutError):
                continue
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

            if event_type == "error":
                print("OPENAI ERROR:", json.dumps(data, ensure_ascii=False, indent=2))
                stop_event.set()
                break

            if DEBUG_OPENAI_EVENTS and event_type not in (
                "response.audio.delta",
                "response.output_audio.delta",
                "response.output_audio_transcript.delta",
                "conversation.item.input_audio_transcription.delta",
                "rate_limits.updated",
            ):
                print("OPENAI RX:", json.dumps(data, ensure_ascii=False)[:1500])

            if event_type == "session.created":
                send_session_update(ws, gespraechspartner_ref, role_ref, initial=True)

            elif event_type == "session.updated":
                session_ready.set()

            elif event_type in ("response.audio.delta", "response.output_audio.delta"):
                delta = data.get("delta")
                if delta:
                    audio_chunk = base64.b64decode(delta)
                    with audio_lock:
                        audio_buffer.extend(audio_chunk)

            elif event_type == "input_audio_buffer.speech_started":
                print("OpenAI VAD: Sprache erkannt")
                with audio_lock:
                    audio_buffer.clear()

            elif event_type == "input_audio_buffer.speech_stopped":
                print("OpenAI VAD: Sprache beendet")

            elif event_type == "input_audio_buffer.committed":
                print("OpenAI VAD: User-Turn committed")

            elif event_type == "conversation.item.input_audio_transcription.completed":
                transcript = data.get("transcript", "")
                print(f"Benutzer-Transkript: {transcript}")
                update_detected_person_from_transcript(transcript, ws, gespraechspartner_ref, role_ref)

            elif event_type == "conversation.item.input_audio_transcription.failed":
                print("Input-Transkription fehlgeschlagen:", json.dumps(data, ensure_ascii=False)[:1500])

            elif event_type == "response.done":
                status = data.get("response", {}).get("status", "?")
                reason = (data.get("response", {}).get("status_details") or {}).get("reason")
                if reason:
                    print(f"OpenAI Response abgeschlossen: {status} ({reason})")
                else:
                    print(f"OpenAI Response abgeschlossen: {status}")

            elif event_type in ("response.audio_transcript.done", "response.output_audio_transcript.done"):
                transcript = data.get("transcript", "")
                print(f"KI-Transkript: {transcript}")

    except Exception as e:
        print(f"Empfangs-Thread-Fehler: {e}")
        traceback.print_exc()
        stop_event.set()
    finally:
        print("Empfangs-Thread beendet")


def build_instructions(gespraechspartner_ref, role_ref):
    """Build the current system instructions for the selected role/person context."""
    gespraechspartner = gespraechspartner_ref[0]
    role = role_ref[0]
    extra_info = ""
    if gespraechspartner:
        extra_info = (
            f"Der Gesprächspartner heißt {gespraechspartner['name']}. "
            f"Er/Sie ist {gespraechspartner['alter']} Jahre alt, arbeitet als {gespraechspartner['beruf']} "
            f"und hat als Hobby {gespraechspartner['hobby']}. "
        )

    return f"{extra_info}Du bist {role['gpt_style']}"


def send_session_update(ws, gespraechspartner_ref, role_ref, initial=False):
    """Send session parameters to OpenAI cleanly without invalid dictionary keys."""
    role = role_ref[0]
    session = {
        "type": "realtime",
        "instructions": build_instructions(gespraechspartner_ref, role_ref),
    }

    if initial:
        session.update({
            "model": REALTIME_MODEL,
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {
                        "type": "audio/pcm",
                        "rate": 24000,
                    },
                    "noise_reduction": {
                        "type": "near_field",
                    },
                    "transcription": {
                        "model": INPUT_TRANSCRIPTION_MODEL,
                        "language": "de",
                        "prompt": "Deutsches Telefongespräch. Achte besonders auf Eigennamen und Vornamen.",
                    },
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": VAD_THRESHOLD,
                        "prefix_padding_ms": VAD_PREFIX_PADDING_MS,
                        "silence_duration_ms": VAD_SILENCE_DURATION_MS,
                        "create_response": True,
                        "interrupt_response": True,
                    },
                },
                "output": {
                    "format": {
                        "type": "audio/pcm",
                        "rate": 24000,
                    },
                    "voice": role["voice_id"],
                    "speed": role.get("speed", 1.0),
                },
            },
            "reasoning": {
                "effort": "low",
            },
        })

    session_config = {
        "type": "session.update",
        "session": session,
    }

    try:
        send_json(ws, session_config, "session.update" if initial else "session.update.instructions")
        print("Session-Update gesendet" if initial else "Session-Instructions aktualisiert")
    except Exception as e:
        print(f"Session-Update fehlgeschlagen: {e}")
        raise


def inject_initial_prompt(ws):
    """Send the initial greeting phrase as a text event instead of a WAV stream."""
    try:
        send_json(ws, {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Hallo, wer ist da?"
                    }
                ]
            }
        }, "greeting_text")

        send_json(ws, {
            "type": "response.create"
        }, "greeting_trigger")

        print("Greeting als Text-Event gesendet (löst das VAD-Noise-Floor-Problem)")
    except Exception as e:
        print(f"Konnte Text-Greeting nicht injizieren: {e}")
        raise


def connect_to_openai(mic_queue, audio_buffer, stop_event, role, gespraechspartner, send_initial_prompt=False, audio_lock=None):
    """Start connection to OpenAI and handle send/receive threads."""
    ws = None
    recv_thread = None
    send_thread = None

    if audio_lock is None:
        audio_lock = threading.Lock()

    session_ready = threading.Event()
    greeting_done = threading.Event()

    try:
        if not API_KEY or API_KEY == "HIER OPENAI API KEY EINTRAGEN":
            raise RuntimeError("OPENAI_API_KEY ist nicht gesetzt / noch Platzhalter in config.py")

        ws = create_connection_with_ipv4(
            WS_URL,
            header=[
                f"Authorization: Bearer {API_KEY}",
                "OpenAI-Safety-Identifier: ki-telefon-local",
            ],
            sslopt={"cert_reqs": ssl.CERT_REQUIRED},
            timeout=10,
        )
        ws.settimeout(1.0)

        print(f"Mit OpenAI WebSocket verbunden: {WS_URL}")

        recv_thread = threading.Thread(
            target=receive_audio_from_websocket,
            args=(ws, audio_buffer, stop_event, gespraechspartner, role, session_ready, audio_lock),
            daemon=True,
        )
        send_thread = threading.Thread(
            target=send_mic_audio_to_websocket,
            args=(ws, mic_queue, stop_event, greeting_done),
            daemon=True,
        )

        recv_thread.start()
        send_thread.start()

        if not session_ready.wait(timeout=5.0):
            print("Timeout: Keine Bestätigung für session.updated vom Server erhalten.")
            stop_event.set()
            return

        if send_initial_prompt and not stop_event.is_set():
            print("Starte Text-Greeting für KI...")
            inject_initial_prompt(ws)

        drain_mic_queue(mic_queue)
        greeting_done.set()

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
        greeting_done.set()
        if ws:
            try:
                ws.close()
            except Exception:
                pass
                
