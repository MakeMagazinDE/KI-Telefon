![GitHub Logo](http://www.heise.de/make/icons/make_logo.png)

Maker Media GmbH

***

# KI-Telefon

**In diesem Projekt habe ich einem alten „Telekom 611-2“-Tischtelefon neues Leben eingehaucht. Abheben, wählen, sprechen – genau wie früher, nur dass am anderen Ende kein Mensch, sondern eine KI den Hörer abnimmt. Ein Raspberry Pi und die API-Schnittstelle von OpenAI machen Echtzeitgespräche möglich.**

![Aufmacherbild aus dem Heft](./doc/kiTelefon_github.jpg)

Die Benötigten Dateien für das Projekt liegen in diesem GitHub-Repository.

Der vollständige Artikel zum Projekt steht in der **[Make-Ausgabe 2/26](https://www.heise.de/select/make/2026/2)**.

## Update (Mai 2026): Code an die OpenAI Realtime API (GA) angepasst
 
OpenAI hat die bisher genutzte Realtime-API-Beta zum 12. Mai 2026 abgeschaltet und aus der API entfernt. Dadurch funktionierte die ursprüngliche Version dieses Projekts nicht mehr – die Verbindung brach direkt nach dem Session-Update ab („Connection to remote host was lost“).
 
Die Datei openai_ws.py wurde auf die neue GA-Schnittstelle umgestellt. Geändert wurde:
- Der Header „OpenAI-Beta: realtime=v1“ wurde entfernt.
- Das session.update nutzt jetzt die GA-Struktur: "type": "realtime", output_modalities, Audio-Konfiguration unter audio.input / audio.output, Format als Objekt {"type": "audio/pcm", "rate": 24000} (im Input und im Output).
- Die Event-Namen wurden angepasst (response.output_audio.delta, response.output_audio_transcript.done).
- Fehler-Events von OpenAI werden jetzt im Terminal ausgegeben, was die Fehlersuche erleichtert.
 
Das verwendete Modell gpt-realtime-mini bleibt unverändert gültig.
