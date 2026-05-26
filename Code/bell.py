import RPi.GPIO as GPIO
import time

from handset import is_handset_lifted
from config import MOTORDRIVER

# Pin definitions for bell
IN1 = 17
IN2 = 27
if MOTORDRIVER == "L298N":
    ENA = 22

# Bell parameters
FREQ = 25           
SLAG_TIME = 1.5     
PAUSE_BETWEEN = 1.5 

def setup():
    """Initialize GPIO pins for the bell motor."""
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(IN1, GPIO.OUT)
    GPIO.setup(IN2, GPIO.OUT)
    if MOTORDRIVER == "L298N":
        GPIO.setup(ENA, GPIO.OUT)
        GPIO.output(ENA, GPIO.HIGH)

def bipolar_wave(duration_s, freq=FREQ):
    """
    Simulate AC for the bell.
    Returns True if the handset is lifted during the ringing.
    """
    period = 1.0 / freq
    half = period / 2.0
    end = time.time() + duration_s
    while time.time() < end:
        # Check handset state using the centralized function
        if is_handset_lifted():
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
    Rings up to `max_rings` times, checking for handset pickup instantly.
    Returns True if picked up, otherwise False.
    """
    try:
        setup()
        for i in range(max_rings):
            if bipolar_wave(SLAG_TIME):
                return True
                
            if i < max_rings - 1:
                # Non-blocking wait during the silence between rings
                start_pause = time.time()
                while time.time() - start_pause < PAUSE_BETWEEN:
                    if is_handset_lifted():
                        return True
                    time.sleep(0.05)
                    
        return False
    finally:
        if MOTORDRIVER == "L298N":
            GPIO.output(ENA, False)
        GPIO.output(IN1, False)
        GPIO.output(IN2, False)
        
