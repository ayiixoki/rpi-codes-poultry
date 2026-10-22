#!/usr/bin/env python3
"""PoultryCare camera service: MJPEG over HTTP, started only while someone watches."""

import hmac
import io
import ipaddress
import json
import re
import signal
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from libcamera import Transform
from picamera2 import Picamera2
from picamera2.encoders import MJPEGEncoder, Quality
from picamera2.outputs import FileOutput

# ---------------- SETTINGS ----------------
PORT = 8000
ACCESS_KEY = "Byy2sdOi4LuiNeoHBlwHmg"
RESOLUTION = (640, 480)
FPS = 10
QUALITY = Quality.MEDIUM
HFLIP = False
VFLIP = False
MAX_VIEWERS = 3
IDLE_STOP_SECONDS = 10
FIREBASE_DB_URL = "https://poultrycare-f816d-default-rtdb.asia-southeast1.firebasedatabase.app"
# ------------------------------------------


class CameraStreamer:
    """Owns the camera. Starts on the first viewer, stops when idle."""

    class _Output(io.BufferedIOBase):
        def __init__(self, owner):
            super().__init__()
            self.owner = owner

        def write(self, buf):
            self.owner._push(bytes(buf))
            return len(buf)

    def __init__(self):
        self._lock = threading.Lock()
        self._cond = threading.Condition()
        self._picam = None
        self._clients = 0
        self._last_release = time.time()
        self._started_at = 0.0
        self._frame = None
        self._frame_id = 0
        threading.Thread(target=self._idle_watchdog, daemon=True).start()

    def _push(self, data):
        with self._cond:
            self._frame = data
            self._frame_id += 1
            self._cond.notify_all()

    def _start(self):
        picam = Picamera2()
        try:
            frame_us = int(1_000_000 / FPS)
            config = picam.create_video_configuration(
                main={"size": RESOLUTION},
                controls={"FrameDurationLimits": (frame_us, frame_us)},
                transform=Transform(hflip=HFLIP, vflip=VFLIP),
            )
            picam.configure(config)
            picam.start_recording(
                MJPEGEncoder(), FileOutput(self._Output(self)), quality=QUALITY
            )
        except Exception:
            picam.close()
            raise
        self._picam = picam
        self._started_at = time.time()
        print("[camera] started", flush=True)

    def _stop(self):
        picam, self._picam = self._picam, None
        if picam is None:
            return
        try:
            picam.stop_recording()
        except Exception:
            pass
        try:
            picam.close()
        except Exception:
            pass
        print("[camera] stopped", flush=True)

    def acquire(self):
        with self._lock:
            if self._picam is None:
                self._start()
            self._clients += 1

    def release(self):
        with self._lock:
            self._clients = max(0, self._clients - 1)
            self._last_release = time.time()

    def wait_frame(self, last_id, timeout=5.0):
        with self._cond:
            if not self._cond.wait_for(lambda: self._frame_id != last_id, timeout):
                return None, last_id
            return self._frame, self._frame_id

    def restart(self):
        with self._lock:
            if self._picam is None or time.time() - self._started_at < 5:
                return
            self._stop()
            try:
                self._start()
            except Exception as e:
                print(f"[camera] restart failed: {e}", flush=True)

    def _idle_watchdog(self):
        while True:
            time.sleep(2)
            with self._lock:
                if (
                    self._picam is not None
                    and self._clients == 0
                    and time.time() - self._last_release > IDLE_STOP_SECONDS
                ):
                    self._stop()

    def shutdown(self):
        with self._lock:
            self._stop()


streamer = CameraStreamer()
viewer_slots = threading.BoundedSemaphore(MAX_VIEWERS)


class StreamHandler(BaseHTTPRequestHandler):
    timeout = 10

    def log_message(self, fmt, *args):
        pass

    def _reply(self, code, text):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        key = parse_qs(url.query).get("key", [""])[0]
        if not hmac.compare_digest(key.encode(), ACCESS_KEY.encode()):
            return self._reply(401, "Unauthorized")
        if url.path != "/stream":
            return self._reply(404, "Not found")
        if not viewer_slots.acquire(blocking=False):
            return self._reply(503, "Too many viewers")
        try:
            self._stream()
        finally:
            viewer_slots.release()

    def _stream(self):
        try:
            streamer.acquire()
        except Exception as e:
            print(f"[camera] cannot start: {e}", flush=True)
            return self._reply(503, "Camera unavailable")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=FRAME")
            self.send_header("Cache-Control", "no-cache, private")
            self.send_header("Pragma", "no-cache")
            self.end_headers()
            last_id = -1
            while True:
                frame, last_id = streamer.wait_frame(last_id)
                if frame is None:
                    streamer.restart()
                    break
                self.wfile.write(
                    b"--FRAME\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(frame)).encode()
                    + b"\r\n\r\n"
                )
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
        except OSError:
            pass
        finally:
            streamer.release()


# ---------------- Firebase address publishing + remote tunnel ----------------

TUNNEL_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
tunnel_url = ""


def get_lan_ip():
    try:
        items = subprocess.check_output(["hostname", "-I"], text=True, timeout=3).split()
    except Exception:
        return ""
    for item in items:
        try:
            ip = ipaddress.ip_address(item)
        except ValueError:
            continue
        if ip.version == 4 and ip.is_private and not ip.is_loopback and not ip.is_link_local:
            return item
    return ""


def tunnel_loop():
    global tunnel_url
    while True:
        try:
            proc = subprocess.Popen(
                ["cloudflared", "tunnel", "--no-autoupdate", "--url", f"http://localhost:{PORT}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except FileNotFoundError:
            print("[camera] cloudflared not installed - remote access disabled", flush=True)
            return
        for line in proc.stdout:
            match = TUNNEL_URL_RE.search(line)
            if match:
                tunnel_url = match.group(0)
                print(f"[camera] tunnel address: {tunnel_url}", flush=True)
        proc.wait()
        tunnel_url = ""
        print("[camera] tunnel stopped, restarting in 10 s", flush=True)
        time.sleep(10)


def firebase_patch(path, data):
    req = urllib.request.Request(
        f"{FIREBASE_DB_URL}/{path}.json",
        data=json.dumps(data).encode(),
        method="PATCH",
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=8).read()


def publisher_loop():
    last = None
    while True:
        delay = 5
        current = (get_lan_ip(), tunnel_url)
        lan, remote = current
        if current != last and (lan or remote):
            try:
                firebase_patch("camera", {
                    "lan_url": f"http://{lan}:{PORT}/stream?key={ACCESS_KEY}" if lan else "",
                    "remote_url": f"{remote}/stream?key={ACCESS_KEY}" if remote else "",
                    "updated_at": {".sv": "timestamp"},
                })
                last = current
                print(f"[camera] published address: {current}", flush=True)
            except Exception as e:
                print(f"[camera] Firebase publish failed (will retry): {e}", flush=True)
                delay = 30
        time.sleep(delay)


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), StreamHandler)
    threading.Thread(target=tunnel_loop, daemon=True).start()
    threading.Thread(target=publisher_loop, daemon=True).start()
    signal.signal(
        signal.SIGTERM,
        lambda *_: threading.Thread(target=server.shutdown, daemon=True).start(),
    )
    print(f"[camera] listening on port {PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        streamer.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
