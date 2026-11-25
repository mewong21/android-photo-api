from flask import Flask, request, jsonify, redirect, render_template_string, url_for
from datetime import datetime, timedelta
import json
import logging
import os
import re
from threading import Lock

app = Flask(__name__)

UPLOAD_FOLDER = "photos"
DATA_FOLDER = os.environ.get("PHOTO_API_DATA_FOLDER", "data")
STATE_FILE = os.path.join(DATA_FOLDER, "api_state.json")
API_KEY = os.environ["PHOTO_API_KEY"]
remote_interval_minutes = 5
remote_retention_days = 7
remote_settings_lock = Lock()
DEFAULT_DEVICE_ID = "unregistered"
remote_devices = {}
activity_log = []
battery_history = []
activity_lock = Lock()
ACTIVITY_RETENTION = timedelta(days=7)

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(DATA_FOLDER, exist_ok=True)
logger = logging.getLogger(__name__)


def _parse_datetime(value, fallback):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return fallback


def load_state():
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as state_file:
            saved = json.load(state_file)
        now = datetime.now()
        with remote_settings_lock:
            for device_id, saved_device in saved.get("devices", {}).items():
                if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", device_id):
                    continue
                device = dict(saved_device)
                device.setdefault("enabled", True)
                device["last_seen"] = _parse_datetime(saved_device.get("last_seen"), now)
                remote_devices[device_id] = device
        with activity_lock:
            for activity in saved.get("activity", []):
                activity["time_sent_at"] = _parse_datetime(activity.get("time_sent_at"), now)
                activity_log.append(activity)
            for reading in saved.get("battery_history", []):
                reading["time_sent_at"] = _parse_datetime(reading.get("time_sent_at"), now)
                battery_history.append(reading)
        clear_expired_records()
    except (OSError, json.JSONDecodeError, TypeError) as error:
        logger.error("Unable to load persisted API state from %s: %s", STATE_FILE, error)


def _state_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _state_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_state_value(item) for item in value]
    return value


def save_state():
    with remote_settings_lock:
        devices = _state_value(remote_devices)
    with activity_lock:
        state = {
            "devices": devices,
            "activity": _state_value(activity_log),
            "battery_history": _state_value(battery_history),
        }
    temporary_file = f"{STATE_FILE}.tmp"
    try:
        with open(temporary_file, "w", encoding="utf-8") as state_file:
            json.dump(state, state_file, indent=2)
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(temporary_file, STATE_FILE)
    except OSError as error:
        logger.error("Unable to persist API state to %s: %s", STATE_FILE, error)


def clear_expired_records():
    cutoff = datetime.now() - ACTIVITY_RETENTION
    with activity_lock:
        activity_log[:] = [
            activity for activity in activity_log
            if activity["time_sent_at"] >= cutoff
        ]
        battery_history[:] = [
            reading for reading in battery_history
            if reading["time_sent_at"] >= cutoff
        ]


load_state()


def get_device_identity():
    device_id = request.headers.get("X-Device-ID", "").strip()
    if not device_id or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", device_id):
        device_id = DEFAULT_DEVICE_ID
    device_name = request.headers.get("X-Device-Name", "").strip()
    return device_id, device_name[:80] or device_id


def register_device(device_id, device_name):
    now = datetime.now()
    with remote_settings_lock:
        device = remote_devices.setdefault(device_id, {
            "interval_minutes": remote_interval_minutes,
            "retention_days": remote_retention_days,
            "enabled": True,
            "name": device_name,
            "last_seen": now,
        })
        device["name"] = device_name
        device["last_seen"] = now
        settings = {
            "interval_minutes": device["interval_minutes"],
            "retention_days": device["retention_days"],
            "enabled": device.get("enabled", True),
            "battery": device.get("battery"),
        }
    save_state()
    return settings


@app.before_request
def clear_expired_activity():
    clear_expired_records()
    save_state()


@app.route("/")
def home():
    with activity_lock:
        activities = list(reversed(activity_log))
    with remote_settings_lock:
        devices = [
            {
                "id": device_id,
                "name": device["name"],
                "interval_minutes": device["interval_minutes"],
                "retention_days": device["retention_days"],
                "enabled": device.get("enabled", True),
                "last_seen": device["last_seen"].strftime("%Y-%m-%d %H:%M:%S"),
                "battery": device.get("battery"),
            }
            for device_id, device in remote_devices.items()
        ]

    return render_template_string("""
        <!doctype html>
        <html lang="en">
        <head>
            <meta charset="utf-8">
            <meta http-equiv="refresh" content="10">
            <title>Photo API Activity</title>
            <style>
                :root {
                    color-scheme: light;
                    --background: #f4f7fb;
                    --surface: #ffffff;
                    --text: #172033;
                    --muted: #687386;
                    --border: #e2e8f0;
                    --primary: #2563eb;
                    --primary-dark: #1d4ed8;
                    --success: #15803d;
                    --danger: #b91c1c;
                    --shadow: 0 12px 30px rgba(30, 41, 59, 0.08);
                }

                * { box-sizing: border-box; }

                body {
                    background: var(--background);
                    color: var(--text);
                    font-family: Inter, ui-sans-serif, system-ui, -apple-system,
                        BlinkMacSystemFont, "Segoe UI", sans-serif;
                    margin: 0;
                }

                .page {
                    margin: 0 auto;
                    max-width: 1100px;
                    padding: 2rem 1.25rem 3rem;
                }

                header {
                    align-items: center;
                    display: flex;
                    justify-content: space-between;
                    margin-bottom: 2rem;
                }

                .eyebrow {
                    color: var(--primary);
                    font-size: 0.78rem;
                    font-weight: 700;
                    letter-spacing: 0.12em;
                    margin: 0 0 0.4rem;
                    text-transform: uppercase;
                }

                h1, h2, p { margin-top: 0; }
                h1 { font-size: clamp(1.8rem, 4vw, 2.5rem); margin-bottom: 0.45rem; }
                h2 { font-size: 1.15rem; margin-bottom: 1.25rem; }
                .subtitle, .helper { color: var(--muted); }
                .subtitle { margin-bottom: 0; }

                .status {
                    align-items: center;
                    background: #dcfce7;
                    border-radius: 999px;
                    color: var(--success);
                    display: inline-flex;
                    font-size: 0.9rem;
                    font-weight: 700;
                    gap: 0.5rem;
                    padding: 0.55rem 0.85rem;
                    white-space: nowrap;
                }

                .status::before {
                    background: currentColor;
                    border-radius: 50%;
                    content: "";
                    height: 0.5rem;
                    width: 0.5rem;
                }

                .card {
                    background: var(--surface);
                    border: 1px solid var(--border);
                    border-radius: 1rem;
                    box-shadow: var(--shadow);
                    padding: 1.5rem;
                }

                .settings {
                    margin-bottom: 1.5rem;
                }

                form {
                    align-items: start;
                    display: grid;
                    gap: 1rem;
                    grid-template-columns: minmax(150px, 0.7fr) minmax(190px, 1fr) auto;
                }

                label {
                    color: var(--muted);
                    display: block;
                    font-size: 0.85rem;
                    font-weight: 700;
                    margin-bottom: 0.45rem;
                }

                input, select {
                    background: var(--surface);
                    border: 1px solid #cbd5e1;
                    border-radius: 0.55rem;
                    color: var(--text);
                    font: inherit;
                    min-height: 2.75rem;
                    padding: 0.6rem 0.75rem;
                    width: 100%;
                }

                input:focus, select:focus {
                    border-color: var(--primary);
                    outline: 3px solid rgba(37, 99, 235, 0.15);
                }

                button {
                    background: var(--primary);
                    border: 0;
                    border-radius: 0.55rem;
                    color: white;
                    cursor: pointer;
                    font: inherit;
                    font-weight: 700;
                    min-height: 2.75rem;
                    padding: 0.6rem 1rem;
                    transition: background 0.15s ease;
                    white-space: nowrap;
                }

                button:hover { background: var(--primary-dark); }
                form > button { margin-top: 1.65rem; }

                .activity-header {
                    align-items: baseline;
                    display: flex;
                    justify-content: space-between;
                }

                .helper { font-size: 0.85rem; }

                .table-wrap {
                    overflow-x: auto;
                }

                table {
                    border-collapse: collapse;
                    margin-top: 0.5rem;
                    min-width: 600px;
                    width: 100%;
                }

                th, td {
                    border-bottom: 1px solid var(--border);
                    padding: 0.9rem 0.75rem;
                    text-align: left;
                }

                th {
                    color: var(--muted);
                    font-size: 0.75rem;
                    letter-spacing: 0.06em;
                    text-transform: uppercase;
                }

                td { font-size: 0.95rem; }
                tbody tr:last-child td { border-bottom: 0; }
                .success { color: var(--success); font-weight: 700; }
                .failed { color: var(--danger); font-weight: 700; }

                .empty {
                    color: var(--muted);
                    margin: 0;
                    padding: 1rem 0 0.25rem;
                }

                .device {
                    border-top: 1px solid var(--border);
                    margin-top: 1.25rem;
                    padding-top: 1.25rem;
                }

                .device:first-of-type {
                    border-top: 0;
                    margin-top: 0;
                    padding-top: 0;
                }

                .device-header {
                    align-items: baseline;
                    display: flex;
                    gap: 0.75rem;
                    justify-content: space-between;
                    margin-bottom: 0.9rem;
                }

                .device-header h3 {
                    margin: 0;
                }

                code {
                    color: var(--muted);
                    font-size: 0.8rem;
                }

                @media (max-width: 700px) {
                    .page { padding: 1.25rem 0.9rem 2rem; }
                    header { align-items: flex-start; flex-direction: column; gap: 1rem; }
                    form { grid-template-columns: 1fr; }
                    form > button { margin-top: 0; width: 100%; }
                    .card { padding: 1.1rem; }
                    .activity-header { align-items: flex-start; flex-direction: column; gap: 0.35rem; }
                }
            </style>
        </head>
        <body>
            <main class="page">
                <header>
                    <div>
                        <p class="eyebrow">Photo Sender</p>
                        <h1>API dashboard</h1>
                        <p class="subtitle">Manage your phones and monitor photo uploads.</p>
                    </div>
                    <div class="status">Server online</div>
                </header>

                <section class="card settings">
                    <h2>Phone settings</h2>
                    {% if devices %}
                    {% for device in devices %}
                    <div class="device">
                        <div class="device-header">
                            <h3>{{ device.name }}</h3>
                            <span class="helper">Last seen {{ device.last_seen }}</span>
                        </div>
                        <p class="helper"><code>{{ device.id }}</code></p>
                        {% if device.battery %}
                        <p class="helper">
                            Battery:
                            {{ device.battery.level_percent if device.battery.level_percent is not none else "?" }}%
                            {% if device.battery.voltage_mv is not none %}at {{ device.battery.voltage_mv }} mV{% endif %}
                            {% if device.battery.current_ma is not none %}, {{ device.battery.current_ma }} mA{% endif %}
                            {% if device.battery.temperature_c is not none %}, {{ device.battery.temperature_c }} C{% endif %}
                            (updated {{ device.battery.time_sent }})
                        </p>
                        {% else %}
                        <p class="helper">No battery reading received yet.</p>
                        {% endif %}
                    <form method="post" action="/settings">
                        <input type="hidden" name="device_id" value="{{ device.id }}">
                        <div>
                            <label for="interval_{{ device.id }}">Photo interval</label>
                            <input id="interval_{{ device.id }}" name="interval_minutes" type="number"
                                   min="1" value="{{ device.interval_minutes }}" required>
                            <p class="helper">Minutes between photos</p>
                        </div>
                        <div>
                            <label>
                                <input name="enabled" type="checkbox" {% if device.enabled %}checked{% endif %}>
                                Photo service enabled
                            </label>
                            <p class="helper">Clear this box to stop scheduled photo capture on this phone.</p>
                        </div>
                        <div>
                            <label for="retention_days">Delete local photos</label>
                            <select id="retention_days" name="retention_days">
                                <option value="0" {% if device.retention_days == 0 %}selected{% endif %}>Right after upload</option>
                                <option value="1" {% if device.retention_days == 1 %}selected{% endif %}>After 1 day</option>
                                <option value="3" {% if device.retention_days == 3 %}selected{% endif %}>After 3 days</option>
                                <option value="7" {% if device.retention_days == 7 %}selected{% endif %}>After 7 days</option>
                                <option value="30" {% if device.retention_days == 30 %}selected{% endif %}>After 30 days</option>
                                <option value="-1" {% if device.retention_days == -1 %}selected{% endif %}>Never</option>
                            </select>
                            <p class="helper">Controls photos stored on the phone</p>
                        </div>
                        <button type="submit">Save settings</button>
                    </form>
                    </div>
                    {% endfor %}
                    {% else %}
                    <p class="empty">No phones connected yet. Start the app on a phone to register it.</p>
                    {% endif %}
                </section>

                <section class="card">
                    <div class="activity-header">
                        <h2>Recent activity</h2>
                        <span class="helper">Refreshes every 10 seconds</span>
                    </div>
                    {% if activities %}
                    <div class="table-wrap">
                        <table>
                            <thead>
                                <tr>
                                    <th>Phone</th>
                                    <th>Photo file name</th>
                                    <th>Time sent</th>
                                    <th>Status</th>
                                </tr>
                            </thead>
                            <tbody>
                                {% for activity in activities %}
                                <tr>
                                    <td>{{ activity.device_id }}</td>
                                    <td>{{ activity.filename }}</td>
                                    <td>{{ activity.time_sent }}</td>
                                    <td class="{{ 'success' if activity.success else 'failed' }}">
                                        {{ activity.status }}
                                    </td>
                                </tr>
                                {% endfor %}
                            </tbody>
                        </table>
                    </div>
                    {% else %}
                    <p class="empty">No upload activity received yet.</p>
                    {% endif %}
                </section>

                <section class="card">
                    <div class="activity-header">
                        <h2>Battery history</h2>
                        <span class="helper">Last 7 days</span>
                    </div>
                    {% if battery_history %}
                    <div class="table-wrap">
                        <table>
                            <thead>
                                <tr>
                                    <th>Phone</th>
                                    <th>Time</th>
                                    <th>Level</th>
                                    <th>Voltage</th>
                                    <th>Current</th>
                                    <th>Temperature</th>
                                </tr>
                            </thead>
                            <tbody>
                                {% for reading in battery_history %}
                                <tr>
                                    <td>{{ reading.device_id }}</td>
                                    <td>{{ reading.time_sent }}</td>
                                    <td>{{ reading.level_percent if reading.level_percent is not none else "-" }}%</td>
                                    <td>{{ reading.voltage_mv if reading.voltage_mv is not none else "-" }} mV</td>
                                    <td>{{ reading.current_ma if reading.current_ma is not none else "-" }} mA</td>
                                    <td>{{ reading.temperature_c if reading.temperature_c is not none else "-" }} C</td>
                                </tr>
                                {% endfor %}
                            </tbody>
                        </table>
                    </div>
                    {% else %}
                    <p class="empty">No battery readings received yet.</p>
                    {% endif %}
                </section>
            </main>
        </body>
        </html>
    """, activities=activities, devices=devices, battery_history=list(reversed(battery_history[-100:])))


def record_activity(filename, status, success, device_id=None):
    sent_at = datetime.now()
    with activity_lock:
        activity_log.append({
            "filename": filename,
            "time_sent": sent_at.strftime("%Y-%m-%d %H:%M:%S"),
            "time_sent_at": sent_at,
            "status": status,
            "success": success,
            "device_id": device_id or DEFAULT_DEVICE_ID,
        })
    save_state()


def record_battery(device_id):
    def parse_number(header, parser):
        value = request.headers.get(header)
        if value is None:
            return None
        try:
            return parser(value)
        except ValueError:
            return None

    reading = {
        "device_id": device_id,
        "time_sent": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "time_sent_at": datetime.now(),
        "voltage_mv": parse_number("X-Battery-Voltage-Mv", int),
        "level_percent": parse_number("X-Battery-Level-Percent", int),
        "current_ma": parse_number("X-Battery-Current-Ma", int),
        "temperature_c": parse_number("X-Battery-Temperature-C", float),
    }
    if all(reading[key] is None for key in ("voltage_mv", "level_percent", "current_ma", "temperature_c")):
        return
    with activity_lock:
        battery_history.append(reading)
    with remote_settings_lock:
        device = remote_devices.get(device_id)
        if device is not None:
            device["battery"] = reading
    save_state()


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "online"
    })


@app.route("/settings", methods=["POST"])
def update_settings():
    try:
        interval_minutes = int(request.form.get("interval_minutes", ""))
    except ValueError:
        return "Interval must be a whole number.", 400

    if interval_minutes < 1:
        return "Interval must be at least 1 minute.", 400

    try:
        retention_days = int(request.form.get("retention_days", ""))
    except ValueError:
        return "Retention must be a valid option.", 400

    if retention_days not in (0, 1, 3, 7, 30, -1):
        return "Retention must be one of the available options.", 400

    device_id = request.form.get("device_id", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", device_id or ""):
        return "A valid phone must be selected.", 400

    with remote_settings_lock:
        device = remote_devices.get(device_id)
        if device is None:
            return "Phone is not registered.", 404
        device["interval_minutes"] = interval_minutes
        device["retention_days"] = retention_days
        device["enabled"] = request.form.get("enabled") == "on"
    save_state()
    return redirect(url_for("home"))


@app.route("/config", methods=["GET"])
def device_config():
    if request.headers.get("X-API-Key") != API_KEY:
        return jsonify({
            "success": False,
            "error": "Unauthorized"
        }), 401

    device_id, device_name = get_device_identity()
    settings = register_device(device_id, device_name)
    return jsonify({
        "deviceId": device_id,
        "intervalMinutes": settings["interval_minutes"],
        "retentionDays": settings["retention_days"],
        "enabled": settings["enabled"],
    })


@app.route("/upload", methods=["POST"])
def upload_photo():

    # Check API key
    key = request.headers.get("X-API-Key")

    if key != API_KEY:
        record_activity("(not saved)", "Failed - unauthorized", False)
        return jsonify({
            "success": False,
            "error": "Unauthorized"
        }), 401

    device_id, device_name = get_device_identity()
    register_device(device_id, device_name)
    record_battery(device_id)

    # Get the raw image data sent by MacroDroid
    photo_data = request.get_data()

    if not photo_data:
        record_activity("(not saved)", "Failed - no photo data", False)
        return jsonify({
            "success": False,
            "error": "No photo data provided"
        }), 400

    # Determine file type from Content-Type
    content_type = request.content_type

    if content_type == "image/png":
        extension = ".png"
    elif content_type == "image/jpeg":
        extension = ".jpg"
    else:
        extension = ".jpg"

    # Create timestamp filename
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
    filename = f"{timestamp}_{device_id}{extension}"

    filepath = os.path.join(UPLOAD_FOLDER, filename)

    # Save the image
    with open(filepath, "wb") as photo:
        photo.write(photo_data)

    record_activity(filename, "Sent", True, device_id)
    return jsonify({
        "success": True,
        "filename": filename
    }), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)