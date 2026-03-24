# handset.py
import RPi.GPIO as GPIO
import time

# Pin-Definition für Gabelumschalter
HANDSET_PIN = 5

def safe_setmode():
    if GPIO.getmode() is None:
         GPIO.setmode(GPIO.BCM)

def setup():
    safe_setmode()
    #GPIO.setmode(GPIO.BCM)
    GPIO.setup(HANDSET_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)

def is_handset_lifted():
    """
    Gibt TRUE zurück, wenn der Hörer abgehoben ist.
    """
    setup()
    safe_setmode()
    value = GPIO.input(HANDSET_PIN)
    return GPIO.input(HANDSET_PIN) == GPIO.LOW

def wait_for_handset_lift(timeout=None):
    """
    Blockiert, bis der Hörer abgehoben wird.
    Optional: Timeout in Sekunden.
    """
    #setup()
    start = time.time()
    while True:
        if is_handset_lifted():
            return True
        if timeout and (time.time() - start) > timeout:
            return False
        time.sleep(0.05)

def wait_for_handset_hangup():
    """
    Blockiert, bis der Hörer wieder aufgelegt wird.
    """
    while True:
        if not is_handset_lifted():
            return
        time.sleep(0.05)

if __name__ == "__main__":
    # Testcode
    setup()
    print("Warte auf Abheben...")
    wait_for_handset_lift()
    print("Hörer abgehoben!")
    print("Warte auf Auflegen...")
    wait_for_handset_hangup()
    print("Hörer aufgelegt.")
