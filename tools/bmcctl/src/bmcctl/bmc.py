"""One BMC: Redfish (basic auth) plus the AMI web API (session) for remote media.

Redfish on this firmware (1.90) covers power, thermal, BIOS settings and boot
override, but its VirtualMedia collection stays empty, so remote media goes
through the same JSON API the BMC's own web UI calls (/api/settings/media/...).
The BMC certificates are self-signed; TLS is not verified.
"""
from __future__ import annotations

import base64
import gzip
import http.cookiejar
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from bmcctl.errors import BmcctlError

CD = 1                # AMI media type: CD/DVD (4 is hard disk)
NOTHING_REDIRECTING = 16416   # "Error while getting Media Info": the empty answer

_TLS = ssl.create_default_context()
_TLS.check_hostname = False
_TLS.verify_mode = ssl.CERT_NONE


def load_creds(path: Path) -> tuple[str, str]:
    env = {}
    try:
        text = path.read_text()
    except OSError as e:
        raise BmcctlError(f"{path}: {e.strerror}") from None
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.lstrip().startswith("#"):
            env[key.strip()] = value.strip().strip("'\"")
    if "BMC_USER" not in env or "BMC_PASS" not in env:
        raise BmcctlError(f"{path} lacks BMC_USER/BMC_PASS (is git-crypt unlocked?)")
    return env["BMC_USER"], env["BMC_PASS"]


def _decode(raw: bytes):
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode(errors="replace")


class Bmc:
    def __init__(self, host: str, user: str, password: str, timeout: int = 30):
        self.host = host
        self.timeout = timeout
        self._user, self._password = user, password
        self._basic = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self._plain = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_TLS))
        self._web = None
        self._csrf = None

    # -- transport -------------------------------------------------------
    def _call(self, opener, method, path, body=None, headers=None, form=False):
        hdrs = dict(headers or {})
        data = None
        if body is not None:
            if form:
                data = urllib.parse.urlencode(body).encode()
                hdrs["Content-Type"] = "application/x-www-form-urlencoded"
            else:
                data = json.dumps(body).encode()
                hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(f"https://{self.host}{path}", data=data, method=method, headers=hdrs)
        try:
            with opener.open(req, timeout=self.timeout) as r:
                return r.status, r.headers, _decode(r.read())
        except urllib.error.HTTPError as e:
            return e.code, e.headers, _decode(e.read())
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise BmcctlError(f"{self.host}: {getattr(e, 'reason', e)}") from None

    # -- Redfish ---------------------------------------------------------
    def redfish(self, method, path, body=None, headers=None):
        return self._call(self._plain, method, path, body, {"Authorization": self._basic, **(headers or {})})

    def rf_get(self, path):
        status, hdrs, data = self.redfish("GET", path)
        if status != 200:
            raise BmcctlError(f"{self.host}: GET {path} -> HTTP {status}: {_message(data)}")
        return data, hdrs

    def rf_patch(self, path, body):
        """PATCH with the If-Match the AMI Redfish service insists on."""
        data, hdrs = self.rf_get(path)
        etag = hdrs.get("ETag") or (data or {}).get("@odata.etag")
        status, _, resp = self.redfish("PATCH", path, body, {"If-Match": etag} if etag else None)
        if status not in (200, 202, 204):
            raise BmcctlError(f"{self.host}: PATCH {path} -> HTTP {status}: {_message(resp)}")
        return resp

    def power_state(self) -> str:
        return self.rf_get("/redfish/v1/Systems/Self")[0].get("PowerState", "?")

    def reset(self, kind: str):
        """ComputerSystem.Reset: On, ForceRestart, ForceOff or GracefulShutdown."""
        status, _, data = self.redfish("POST", "/redfish/v1/Systems/Self/Actions/ComputerSystem.Reset",
                                       {"ResetType": kind})
        if status not in (200, 202, 204):
            raise BmcctlError(f"{self.host}: reset {kind} -> HTTP {status}: {_message(data)}")

    def boot_override(self) -> tuple[dict, dict]:
        """(override in effect, override staged in the FutureState object for the next boot)."""
        now = self.rf_get("/redfish/v1/Systems/Self")[0].get("Boot", {})
        staged = self.rf_get("/redfish/v1/Systems/Self/SD")[0].get("Boot", {})
        return now, staged

    def set_boot_override(self, target: str):
        """One-time UEFI boot to `target` (Cd, Hdd), or clear the override (None).

        This firmware rejects boot properties on Systems/Self ("moved to FutureState
        URI"); they are written to Systems/Self/SD and take effect at the next boot.
        """
        boot = {"BootSourceOverrideEnabled": "Disabled", "BootSourceOverrideTarget": "None"}
        if target != "None":
            boot = {"BootSourceOverrideEnabled": "Once", "BootSourceOverrideTarget": target,
                    "BootSourceOverrideMode": "UEFI"}
        return self.rf_patch("/redfish/v1/Systems/Self/SD", {"Boot": boot})

    def bios(self) -> tuple[dict, dict]:
        """(current attributes, attributes staged for the next POST)."""
        current = self.rf_get("/redfish/v1/Systems/Self/Bios")[0].get("Attributes", {})
        staged = self.rf_get("/redfish/v1/Systems/Self/Bios/SD")[0].get("Attributes", {})
        return current, staged

    def stage_bios(self, attrs: dict):
        return self.rf_patch("/redfish/v1/Systems/Self/Bios/SD", {"Attributes": attrs})

    def thermal(self) -> dict:
        data = self.rf_get("/redfish/v1/Chassis/Self/Thermal")[0]
        out = {t.get("Name"): t.get("ReadingCelsius") for t in data.get("Temperatures", [])}
        out.update({f.get("Name") or f.get("FanName"): f.get("Reading") for f in data.get("Fans", [])})
        return out

    # -- AMI web API (remote media) --------------------------------------
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.logout()

    def _login(self):
        jar = http.cookiejar.CookieJar()
        self._web = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_TLS),
                                                urllib.request.HTTPCookieProcessor(jar))
        status, _, data = self._call(self._web, "POST", "/api/session",
                                     {"username": self._user, "password": self._password}, form=True)
        # While the BMC is still starting it can answer 200 with "CSRFToken": null.
        if status != 200 or not isinstance(data, dict) or not data.get("CSRFToken"):
            self._web = None
            raise BmcctlError(f"{self.host}: web login failed (HTTP {status}; BMC still starting?)")
        self._csrf = data["CSRFToken"]

    def logout(self):
        """Always release the web session: the BMC has a small, fixed number of slots."""
        if self._web is not None:
            try:
                self._call(self._web, "DELETE", "/api/session", headers={"X-CSRFTOKEN": self._csrf})
            finally:
                self._web = self._csrf = None

    def web(self, method, path, body=None):
        if self._web is None:
            self._login()
        status, _, data = self._call(self._web, method, path, body, {"X-CSRFTOKEN": self._csrf})
        if status != 200:
            raise BmcctlError(f"{self.host}: {method} {path} -> HTTP {status}: {_message(data)}")
        return data

    def media_settings(self) -> dict:
        return self.web("GET", "/api/settings/media/general")

    def remote_images(self) -> list:
        """Configured images that are not redirecting; a started image moves to active_redirections()."""
        return self.web("GET", "/api/settings/media/remote/images") or []

    def active_redirections(self) -> list:
        """Media the BMC is redirecting now. With none, this firmware answers 500 code 16416."""
        if self._web is None:
            self._login()
        status, _, data = self._call(self._web, "GET", "/api/settings/media/active_redirections",
                                     headers={"X-CSRFTOKEN": self._csrf})
        if status == 500 and isinstance(data, dict) and data.get("code") == NOTHING_REDIRECTING:
            return []
        if status != 200:
            raise BmcctlError(f"{self.host}: GET active_redirections -> HTTP {status}: {_message(data)}")
        return data or []

    def _put_media(self, settings: dict):
        # The UI round-trips the retry settings under hd_* names; without them the BMC
        # saves the rest and answers 500 "Error in setting Remote Media Misc Configuration".
        settings["hd_remote_retry_count"] = settings.get("rmedia_retry_count", 3)
        settings["hd_remote_retry_interval"] = settings.get("rmedia_retry_interval", 15)
        return self.web("PUT", "/api/settings/media/general", settings)

    def set_rmedia(self, enable: bool, seconds: int = 60):
        """Turn remote media on or off with Redfish's EnableRMedia.

        Not through the web API's PUT: after turning it off and on that way on
        2026-10-27, top's BMC stopped listing the configured image until a BMC reset.
        """
        want = 1 if enable else 0
        if self.media_settings().get("remote_media_support") == want:
            return
        status, _, data = self.redfish("POST", "/redfish/v1/Managers/Self/Actions/Oem/AMIVirtualMedia.EnableRMedia",
                                       {"RMediaState": "Enable" if enable else "Disable"})
        if status not in (200, 202, 204, 400) or (status == 400 and "initiated" not in _message(data)):
            raise BmcctlError(f"{self.host}: EnableRMedia -> HTTP {status}: {_message(data)}")
        deadline = time.monotonic() + seconds
        while self.media_settings().get("remote_media_support") != want:
            if time.monotonic() > deadline:
                raise BmcctlError(f"{self.host}: remote media did not turn {'on' if enable else 'off'} in {seconds}s")
            time.sleep(3)

    def configure_http_cd(self, url: str):
        """Point the BMC's remote CD at an http(s) image URL (the UI's 'HTTP' share type)."""
        settings = self.media_settings()
        settings.update({
            "mount_cd": 1,
            "cd_remote_share_type": "http",
            "cd_remote_server_address": urllib.parse.urlsplit(url).hostname or "",
            "cd_remote_source_path": url,
            "cd_remote_domain_name": "",
            "cd_remote_user_name": "",
            "cd_remote_password": "",
        })
        return self._put_media(settings)

    def start_cd(self, image: dict):
        return self.web("POST", "/api/settings/media/remote/start-media", {
            "image_name": image["image_name"], "image_type": CD,
            "image_redirection": 1, "image_index": image.get("image_index", 0)})

    def stop_cd(self, active: dict):
        """Stop one entry of active_redirections()."""
        return self.web("POST", "/api/settings/media/remote/stop-media", {
            "image_name": active["image_name"], "image_type": active.get("media_type", CD),
            "image_index": active.get("media_index", 0)})

    def wait_images(self, name: str, matcher, seconds: int = 60) -> dict:
        """Wait for the BMC to list `name` after configure_http_cd (about 8s).

        Don't call refresh_images() for an http source: it empties the list (the
        UI's "refresh" re-scans NFS/CIFS shares; with an http URL it finds nothing).
        """
        deadline = time.monotonic() + seconds
        while True:
            for image in self.remote_images():
                if matcher(image, name):
                    return image
            if time.monotonic() > deadline:
                raise BmcctlError(f"{self.host}: the BMC did not list {name} within {seconds}s "
                                  f"(saw: {[i.get('image_name') for i in self.remote_images()]})")
            time.sleep(3)


def _message(data) -> str:
    if isinstance(data, dict):
        err = data.get("error", data)
        if isinstance(err, dict):
            info = err.get("@Message.ExtendedInfo") or []
            msgs = [i.get("Message") for i in info if isinstance(i, dict)]
            return "; ".join(m for m in msgs if m) or str(err.get("message") or err)[:300]
    return str(data)[:300]
