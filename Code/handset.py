# handset.py
import RPi.GPIO as GPIO
import time

# Pin-Definition für Gabelumschalter
HANDSET_PIN = 5
_HANDSET_SETUP_DONE = False


def safe_setmode():
    if GPIO.getmode() is None:
        GPIO.setmode(GPIO.BCM)


def setup():
    """Initialize handset GPIO once."""
    global _HANDSET_SETUP_DONE
    safe_setmode()
    if not _HANDSET_SETUP_DONE:
        GPIO.setup(HANDSET_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        _HANDSET_SETUP_DONE = True


def is_handset_lifted():
    """
    Gibt TRUE zurück, wenn der Hörer abgehoben ist.
    """
    setup()
    return GPIO.input(HANDSET_PIN) == GPIO.LOW


if __name__ == "__main__":
    # Simpler lokaler Test
    setup()
    print("Hörer-Test gestartet. Abbruch mit Strg+C")
    try:
        while True:
            state = "Abgehoben" if is_handset_lifted() else "Aufgelegt"
            print(f"Status: {state}", end="\r")
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\nTest beendet.")
