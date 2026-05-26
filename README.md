
# KI-Telefon (fork)

Ein Fork des ursprünglichen Projektcodes zum **KI-Telefon** der Maker Media GmbH (vollständiger Artikel in der **[Make-Ausgabe 2/26](https://www.heise.de/select/make/2026/2)**).

Der Code dieses forks ist gegenüber dem Ursprungscode stark überarbeitet und erweitert, um sich exakt wie die Telekom-Infrastruktur der 80er Jahre zu verhalten, während im Hintergrund moderne KI-Technologie in Form der OpenAI Realtime API V2 arbeitet.

---

## Was ist neu?

Das System ahmt nun physikalische und logische Abläufe eines echten Wählscheibentelefons (z. B. FeTAp 611-2 oder 791-1) realistisch nach:

* **Instant Hardware-Mute:** Wird der Gabelumschalter gedrückt, wird der Audio-Stream der KI *sofort* hart stummgeschaltet – genau wie bei der Stromunterbrechung einer echten Hörmuschel. Das langsame "Ausfaden" oder Weiterreden der KI beim Auflegen entfällt.
* **Unterbrechbarer Freiton:** Der simulierte Amtston reagiert in Echtzeit. Legt man während des Wählens oder Klingelns auf, bricht der Ton augenblicklich ab.
* **Echte Zwangstrennung:** Bricht die Verbindung zur KI ab, ertönt das Besetztzeichen. Das System blockiert danach logisch die Leitung, bis der Nutzer den Hörer physisch auflegt. Ein unnatürlicher, sofortiger Neustart des Amtstons wird verhindert.
* **Agiles Polling (0.5s Debounce):** Das System reagiert auch auf schnelles Auflegen und Neu-Abheben, filtert aber Wackelkontakte alter Kupferfedern sicher heraus.
* **Anrufer:** Das Programm ruft nun nur dann an und klingelt, wenn der Hörer auf der Gabel liegt. (Zustand wurde vorher nicht beachtet)

## Modifikationen an der Hardware

* **Telefon:** Neben dem FeTAp 611-2 eignet sich auch das FeTAp 791-1 für dieses Projekt; Wählscheibe und Klingel werden bei beiden identisch an Pi, Motordriver und Step-Up-Konverter angeschlossen
* **Gabelschalter:** Unterschiedliche Platinenlayouts erschweren u.U. die Kontaktpunkte des original Gabelschalters zu ermitteln. Ich habe daher einen kleinen Mikroschalter mit Hebel unter die Gabel geklebt mit etwas Heißkleber. Der Mikroschalter wird identisch am Pi angeschlossen, so dass im Code keine Änderung notwendig ist
* **Motordriver:** Der im Make-Artikel verwendete L298N benötigt durch seinen großen Kühlkörper zu viel Platz im Telefon, statt dessen kann man auch den Mosfet-basierten und viel kleineren DRV8871 verwenden. Dieser wird identisch angesteuert, kommt aber ohne ENA aus. In der config.py wird der verwendete Driver eingestellt (Default: L298N, alternativ: DRV8871), im Code wird der ENA-Pin nur definiert und angesprochen, wenn L298N als Motor-Driver ausgewählt ist.

## Architektur- & Stabilitäts-Upgrades

Die Software wurde für den dauerhaften, wartungsfreien *Headless*-Betrieb (ohne Monitor) auf einem Raspberry Pi optimiert.

* **VAD-Noise-Floor Fix (WAV entfernt):** Die alte Logik, das Gespräch durch das Einspeisen einer `greeting.wav` zu starten, zerstörte bei analogen Telefonen oft die Voice-Activity-Detection (VAD) von OpenAI. Gespräche werden nun sauber über ein unsichtbares Text-Event (`conversation.item.create`) initiiert.
* **Thread-Sicherheit:** Einführung strenger WebSocket-Locks gegen Race-Conditions.
* **API V2 & Environment Variables:** Migration auf das aktuelle `gpt-realtime-2` Modell. API-Keys können nun sicher über System-Umgebungsvariablen (`OPENAI_API_KEY`) statt im Klartext geladen werden.
* **Saubere Terminierung:** Echtes `GPIO.cleanup()` und sofortiger Prozess-Exit beim Wählen der Ziffer `0` (Shutdown-Sequenz).
* **Shutdown-Sequenz:** spielt zur Erkennung eine Tonfolge ab, statt des 425Hz Amtstons.

## Neue Entwickler-Tools

#### 1. Interaktives, standalone Hardware-Testprogramm (`Code/telefon_hw_test.py`)
Ein komplett neues Standalone-Skript, um die Hardware beim Zusammenbau interaktiv zu debuggen – ohne direkt API-Kosten bei OpenAI zu erzeugen:
- **Lautsprecher/Hörer:** 425Hz-Amtston-Generator
- **Mikrofon:** Live-Pegelanzeige (VU-Meter) im Terminal + Playback-Test
- **Gabel:** Echtzeit-Statusänderungen des Hook-Switches
- **Wählscheibe:** Präziser Impulszähler und Ziffern-Dekoder
- **Klingel:** H-Brücken-Stresstest (25 Hz Wechselstrom)

*Tipp: Untermenüs können direkt aufgerufen werden, z.B.: `python3 telefon_hw_test.py --test rotary`*

#### 2. Headless Logging (`Code/logging_setup.py`)
Ein unsichtbarer TeeStream-Logger. Alle Konsolenausgaben und kritischen C-Level-Abstürze (Segfaults von PyAudio) werden automatisch in `/home/username/ki-telefon.log` geschrieben. 
Ideal zur Fehleranalyse, wenn das Telefon headless betrieben wird.

## Überarbeitete Persönlichkeiten (`Code/roles.py`)

Die Prompts der Gesprächspartner wurden stark erweitert, um lebendigere und realistischere Dialoge zu erzwingen. 
Aus dem einfachen "Pierre" wurde beispielsweise der auskunftsfreudige französische Koch **Jean-Luc**. 
Auch Dialekte (Schwäbisch, Berlinerisch, Hamburgerisch) wurden nachgeschärft.

## Durchgeführte Änderungen im Detail

Gegenüber dem ursprünglichen Code aus dem Make-Magazin wurden neben vielen kleinen Bugfixes, Korrekturen und Anpassungen die folgenden größeren Veränderungen vorgenommen:

* **VAD-Noise-Floor Fix (WAV entfernt):** Die Datei `greeting.wav` und alle zugehörigen Importe (`wave`) wurden restlos entfernt. Das Gespräch wird nun über ein unsichtbares WebSocket-Text-Event (`conversation.item.create` mit dem Text "Hallo, wer ist da?") initiiert. Dies verhindert, dass das analoge Rauschen der Leitung die automatische Stille-Erkennung (VAD) des OpenAI-Servers dekalibriert.
* **Thread-Sicherheit (Race Conditions):** Einführung eines `threading.Lock()` (`WS_SEND_LOCK`) um alle `ws.send()`-Aufrufe. Verhindert asynchrone Abstürze und *Silent Connection Drops*, wenn der Receive-Thread (Session-Updates) und der Send-Thread (Mikrofon-Audio) gleichzeitig auf den WebSocket zugreifen wollen.
* **Instant-Mute & Interruptible Audio:** Die Audiowiedergabe stoppt beim Drücken des Gabelschalters nun auf die Millisekunde genau (Ersatz der Audio-Chunks durch Nullen), auch während der 500ms-Software-Entprellung. Blockierende `time.sleep()`-Pausen beim Freiton wurden durch agile Polling-Schleifen ersetzt, sodass der Ton beim Abheben/Auflegen sofort stoppt.
* **Leitungs-Zwangstrennung:** Nach einem Verbindungsabbruch (Besetztzeichen) springt das System nicht mehr automatisch in den Wählmodus zurück. Der Code erzwingt nun ein physisches Auflegen des Hörers, um die Leitung wieder freizugeben.
* **GPIO- & Prozess-Sicherheit:** Doppelte Pin-Initialisierungen (Runtime Warnings) in der Wählscheiben-Logik wurden entfernt. Die Shutdown-Sequenz (Ziffer 0) beendet den Python-Prozess nun sofort sauber per `sys.exit(0)`. Ein zentraler `try...finally`-Block sorgt für ein zuverlässiges `GPIO.cleanup()` bei Programmende.
* **API-Compliance & Security:** Bereinigung ungültiger Payload-Parameter (`"type": "realtime"`) im Session-Update. Der `OPENAI_API_KEY` wird nun primär aus den sicheren System-Umgebungsvariablen geladen (Fallback via `config.py` bleibt für Tests erhalten).
* **Konsistentes & pyhsikalisch korrektes Verhalten:** Es kann jetzt nur noch klingeln, wenn der Hörer vorher auf der Gabel liegt. Die Anwahl einer Nummer kann durch Auflegen des Höreres abgebrochen werden. Der Status des Gabelschalters wird getracked und überwacht.
  
### Quickstart

1. Abhängigkeiten installieren: `pip install -r Code/requirements.txt`
2. OpenAI API-Key als Umgebungsvariable setzen: `export OPENAI_API_KEY="sk-dein-key"` (Alternativ Fallback in `Code/config.py` eintragen).
3. Hardware testen: `python3 Code/telefon_hw_test.py`
4. KI-Telefon starten: `python3 Code/main.py`

---

### Viel Spaß mit dem erweiterten Code & KI-Telefon!
