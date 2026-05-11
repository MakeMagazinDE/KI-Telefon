import RPi.GPIO as GPIO
import time

# Pin-Definitionen Klingel
IN1 = 17
IN2 = 27
ENA = 22

# Pin für "Abgehoben"-Erkennung (Test mit Jumper auf GND)
PIN_ANSWER = 5  # GPIO-Pin an den Jumper kommt

# Klingel-Parameter
FREQ = 25           # 25 Hz Wechsel
SLAG_TIME = 1.5     # Dauer eines Schlages in Sekunden
PAUSE_BETWEEN = 1.5 # Pause zwischen Schlägen in Sekunden

def setup():
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(IN1, GPIO.OUT)
    GPIO.setup(IN2, GPIO.OUT)
    GPIO.setup(ENA, GPIO.OUT)
    # Eingang mit Pull-Up → Standard HIGH, Jumper an GND → LOW
    GPIO.setup(PIN_ANSWER, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.output(ENA, GPIO.HIGH)

def bipolar_wave(duration_s, freq=FREQ):
    """
    Simuliert Wechselstrom für die Klingel.
    Gibt True zurück, wenn während des Schlagens abgehoben wurde.
    """
    period = 1.0 / freq
    half = period / 2.0
    end = time.time() + duration_s
    while time.time() < end:
        if GPIO.input(PIN_ANSWER) == GPIO.LOW:  # LOW = Jumper an GND
            return True
        GPIO.output(IN1, True)
        GPIO.output(IN2, False)
        time.sleep(half)
        GPIO.output(IN1, False)
        GPIO.output(IN2, True)
        time.sleep(half)
    GPIO.output(IN1, False)
    GPIO.output(IN2, False)
    return False

def ring_until_answer(max_rings=5):
    """
    Klingelt bis zu `max_rings` Mal, prüft währenddessen auf Abheben.
    Gibt True zurück, wenn abgehoben wurde, sonst False.
    """
    try:
        setup()
        for i in range(max_rings):
            if bipolar_wave(SLAG_TIME):
                return True
            time.sleep(PAUSE_BETWEEN)
            # Prüfen auch zwischen den Schlägen
            if GPIO.input(PIN_ANSWER) == GPIO.LOW:
                return True
        return False
    finally:
        GPIO.output(ENA, False)
        GPIO.output(IN1, False)
        GPIO.output(IN2, False)
        # Würde auch GPIO 5/26 wieder zurücksetzen, daher auskommentiert:
        # GPIO.cleanup()
