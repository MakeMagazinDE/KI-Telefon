import base64
import json
import socket
import ssl
import threading
import time
import websocket
import socks
import wave

from config import OPENAI_API_KEY
from gespraechspartner import personen_info

# Proxy aktivieren
socket.socket = socks.socksocket

# WebSocket server URL
WS_URL = 'wss://api.openai.com/v1/realtime?model=gpt-realtime-mini'
API_KEY = OPENAI_API_KEY

REENGAGE_DELAY_MS = 500  # Mikrofon-Verzögerung

def create_connection_with_ipv4(*args, **kwargs):
    """WebSocket-Verbindung erzwingen über IPv4"""
    original_getaddrinfo = socket.getaddrinfo
    def getaddrinfo_ipv4(host, port, family=socket.AF_INET, *args):
        return original_getaddrinfo(host, port, socket.AF_INET, *args)
    socket.getaddrinfo = getaddrinfo_ipv4
    try:
        return websocket.create_connection(*args, **kwargs)
    finally:
        socket.getaddrinfo = original_getaddrinfo

def send_mic_audio_to_websocket(ws, mic_queue, stop_event):
    """Mikrofondaten an OpenAI WebSocket senden"""
    try:
        while not stop_event.is_set():
            if not mic_queue.empty():
                mic_chunk = mic_queue.get()
                encoded_chunk = base64.b64encode(mic_chunk).decode('utf-8')
                message = json.dumps({
                    'type': 'input_audio_buffer.append',
                    'audio': encoded_chunk
                })
                try:
                    ws.send(message)
                except Exception as e:
                    print(f'Fehler beim Senden von Mikrofon-Audio: {e}')
    except Exception as e:
        print(f'Mikrofon-Thread-Fehler: {e}')
    finally:
        print('Mikrofon-Sende-Thread beendet')

def receive_audio_from_websocket(ws, audio_buffer, stop_event, gespraechspartner_ref, role_ref):
    """Audio und Events vom OpenAI WebSocket empfangen"""
    try:
        while not stop_event.is_set():
            message = ws.recv()
            if not message:
                continue
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                print("Ungültige JSON-Nachricht empfangen")
                continue

            event_type = data.get("type", "")

            if event_type == 'session.created':
                send_fc_session_update(ws, gespraechspartner_ref, role_ref)

            elif event_type == 'response.audio.delta':
                audio_chunk = base64.b64decode(data['delta'])
                audio_buffer.extend(audio_chunk)

            elif event_type == 'input_audio_buffer.speech_started':
                audio_buffer.clear()

            elif event_type == 'response.audio_transcript.done':
                transcript = data.get("transcript", "")
                print(f'Transkript: {transcript}')
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
        print(f'Empfangs-Thread-Fehler: {e}')
    finally:
        print('Empfangs-Thread beendet')

def send_fc_session_update(ws, gespraechspartner_ref, role_ref):
    """Session-Parameter an OpenAI senden"""
    gespraechspartner = gespraechspartner_ref[0]
    role = role_ref[0]
    extra_info = ""
    if gespraechspartner:
        extra_info = (
            f"Der Gesprächspartner heißt {gespraechspartner['name']}. "
            f"Er/Sie ist {gespraechspartner['alter']} Jahre alt, arbeitet als {gespraechspartner['beruf']} "
            f"und hat als Hobby {gespraechspartner['hobby']}. "
        )

    instructions = (
        f"{extra_info}"
        f"Du bist {role['gpt_style']}"
    )

    session_config = {
        "type": "session.update",
        "session": {
            "instructions": instructions,
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.5,
                "prefix_padding_ms": 300,
                "silence_duration_ms": 500
            },
            "voice": role['voice_id'],
            "temperature": 1,
            "max_response_output_tokens": 4096,
            "modalities": ["text", "audio"],
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "input_audio_transcription": {
                "model": "whisper-1"
            }
        }
    }
    try:
        ws.send(json.dumps(session_config))
        print("Session-Update gesendet")
    except Exception as e:
        print(f"Session-Update fehlgeschlagen: {e}")

def inject_greeting_audio(ws, wav_path):
    """Schickt WAV-Datei als Input-Audio an KI"""
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

            # 100 ms Chunks bei 24kHz
            chunk_duration = 0.1
            frames_per_chunk = int(24000 * chunk_duration)
 
            while True:
                data = wf.readframes(frames_per_chunk)
                if not data:
                    break
                
                encoded_chunk = base64.b64encode(data).decode("utf-8")
                ws.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": encoded_chunk
                }))
                time.sleep(chunk_duration)  # Echtzeit simulieren

        # Audio-Buffer abschließen und Verarbeitung starten
        ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        ws.send(json.dumps({"type": "response.create"}))
        print("Greeting-Audio an KI gesendet")

    except Exception as e:
        print(f"Konnte Greeting nicht injizieren: {e}")

def connect_to_openai(mic_queue, audio_buffer, stop_event, role, gespraechspartner, greeting=None):
    """
    Startet die Verbindung zu OpenAI und steuert Sende- & Empfangs-Threads.
    greeting -> Wenn String (Pfad zu WAV) gesetzt, wird diese Datei direkt an KI geschickt.
    """
    ws = None
    try:
        ws = create_connection_with_ipv4(
            WS_URL,
            header=[
                f'Authorization: Bearer {API_KEY}',
                'OpenAI-Beta: realtime=v1'
            ],
            sslopt={"cert_reqs": ssl.CERT_NONE}
        )
        print('Mit OpenAI WebSocket verbunden')

        # Empfangs- und Sende-Threads starten
        recv_thread = threading.Thread(
            target=receive_audio_from_websocket,
            args=(ws, audio_buffer, stop_event, gespraechspartner, role)
        )
        send_thread = threading.Thread(
            target=send_mic_audio_to_websocket,
            args=(ws, mic_queue, stop_event)
        )

        recv_thread.start()
        send_thread.start()

        # Erstes Session-Update
        send_fc_session_update(ws, gespraechspartner, role)
        
        # Falls Greeting gesetzt -> WAV als Input schicken
        if greeting:
            print(f"Starte Greeting (.wav) für KI: {greeting}")
            inject_greeting_audio(ws, greeting)

        # Hauptloop
        while not stop_event.is_set():
            time.sleep(0.1)

        # Verbindung schließen
        try:
            ws.send_close()
        except:
            pass
        recv_thread.join()
        send_thread.join()
        print('Verbindung geschlossen')

    except Exception as e:
        print(f"Verbindung fehlgeschlagen: {e}")
    finally:
        if ws:
            try:
                ws.close()
            except:
                pass
