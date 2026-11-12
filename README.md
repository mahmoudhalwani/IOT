# Smart Doorbell + Lock: door-unit software

Built so far: face pipeline, encrypted people database, access schedules, the
lock and motion sensor, the main door loop, the access log and the resident
website. Remote access from outside the home network, video clips and phone
alerts come next.

## Set up on the Raspberry Pi

```bash
sudo apt install -y python3-venv
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-pi.txt
./scripts/download_models.sh
```

On a laptop, skip `requirements-pi.txt`; the lock is then simulated and printed
on screen.

## Wiring (Raspberry Pi 4)

| Part | Wire | Goes to |
|---|---|---|
| Servo | signal (orange/yellow) | GPIO18, physical pin 12 |
| Servo | + (red) | **separate 5 V supply**, not the Pi's 5 V pin |
| Servo | - (brown/black) | supply ground **and** a Pi ground pin (e.g. pin 6) |
| Motion sensor | OUT | GPIO17, physical pin 11 |
| Motion sensor | GND | Pi ground (e.g. pin 9) |
| Motion sensor | VCC | PIR (HC-SR501): Pi 5 V, pin 2. IR obstacle module (FC-51): Pi 3.3 V, pin 1 |

The Pi's pins only tolerate 3.3 V. A PIR module's output is 3.3 V even when it
is powered from 5 V. An IR obstacle module outputs whatever it is powered with,
so power it from 3.3 V. For an IR obstacle module also set
`DOORBELL_MOTION_ACTIVE_HIGH=false` (its output goes low when it sees something).
With no sensor, set `DOORBELL_MOTION_PIN=-1` and the camera is used as the trigger.

Find the servo angles for your lock, then put them in `doorbell/config.py`
(`servo_locked_angle`, `servo_unlocked_angle`):

```bash
python -m doorbell servo 0
python -m doorbell servo 90
```

## Run the door

```bash
python -m doorbell run
python -m doorbell log
```

`run` locks the door when it starts and when it stops. A recognised person
unlocks it for 5 seconds (`relock_seconds`). To start it at boot, see
`scripts/doorbell.service`.

## Use

```bash
python -m doorbell add "Alaa" --consent --camera          # enrol with the door camera
python -m doorbell add "Sara" --consent sara1.jpg sara2.jpg
python -m doorbell add "Cleaner" --guest --consent --days mon,thu --hours 09:00-12:00 --until 2026-12-31T23:59 c.jpg
python -m doorbell list
python -m doorbell access 3 --hours 08:00-18:00           # or --always
python -m doorbell remove 3
python -m doorbell watch                                  # live decisions from the camera
```

## Website

Create an account (the password is typed hidden), then start the door:

```bash
python -m doorbell user add alaa
python -m doorbell run
```

Open http://127.0.0.1:8000 in a browser on the same machine. On a laptop, set
`DOORBELL_WEB_DEV=true` first so the sign-in cookie works over plain http;
never set it on the real door.

Optional second step at sign-in (authenticator app code):

```bash
python -m doorbell user totp alaa
```

Security measures: Argon2id password hashes; random session tokens stored only
as hashes; HttpOnly, Secure, SameSite=Strict cookies; CSRF token and same-origin
check on every change; lock commands signed, time-limited and single-use;
account pause after repeated wrong passwords; strict Content-Security-Policy;
enrolment photos processed in memory and never saved; no route can edit or
delete the access log.

### Demo page

A static demo of the pages, with a pretend door that runs in the browser, is
published from the `gh-pages` branch: https://mahmoudhalwani.github.io/IOT/
It is for showing the interface only and controls nothing. Rebuild it with
`python tools/build_demo.py <output dir>`.

## How a decision is made

1. YuNet finds the largest face; it must be at least 90 px wide and fully in frame.
2. Two MiniFASNet models judge whether it is a live face or a photo/screen. Both
   must say "real" with a mean probability of 0.80 or more.
3. Only then SFace compares the face with the enrolled templates.
4. The door is granted after 5 consecutive frames that are live and the same
   person, and only inside that person's allowed time.

Thresholds are in `doorbell/config.py` and can be overridden with `DOORBELL_*`
environment variables.

## Models

| File | Source | Licence |
|---|---|---|
| `face_detection_yunet_2023mar.onnx` | OpenCV Zoo | MIT |
| `face_recognition_sface_2021dec.onnx` (downloaded) | OpenCV Zoo | Apache-2.0 |
| `minifasnet_*.onnx` | minivision-ai/Silent-Face-Anti-Spoofing, converted with `tools/export_antispoof.py` | Apache-2.0 |

## Tests

```bash
pip install pytest && python -m pytest tests
```
