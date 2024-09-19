import os
import shutil
import hashlib

from enum import Enum
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import NamedTuple
from PyQt5.QtCore import QObject, pyqtSignal

from . import __version__, eventloop
from .network import RequestManager
from .properties import ObservableProperties, Property
from .util import ZipFile, client_logger as log


class UpdateState(Enum):
    disabled = 0
    unknown = 1
    checking = 2
    available = 3
    latest = 4
    downloading = 5
    installing = 6
    failed = 7
    restart_required = 8


class UpdatePackage(NamedTuple):
    version: str
    url: str
    sha256: str


class AutoUpdate(QObject, ObservableProperties):

    default_api_url = os.getenv("INTERSTICE_URL", "https://api.interstice.cloud")

    state = Property(UpdateState.disabled)
    latest_version = Property("")
    error = Property("")

    state_changed = pyqtSignal(UpdateState)
    latest_version_changed = pyqtSignal(str)
    error_changed = pyqtSignal(str)

    def __init__(
        self,
        enabled: bool = True,
        plugin_dir: Path | None = None,
        current_version: str | None = None,
        api_url: str | None = None,
    ):
        super().__init__()
        self._is_enabled = enabled
        self.plugin_dir = plugin_dir or Path(__file__).parent.parent
        self.current_version = current_version or __version__
        self.api_url = api_url or self.default_api_url
        self._package: UpdatePackage | None = None
        self._temp_dir: TemporaryDirectory | None = None
        self._request_manager: RequestManager | None = None

        if self.is_enabled:
            self.check()

    def check(self):
        return eventloop.run(
            self._handle_errors(self._check, "Failed to check for new plugin version")
        )

    async def _check(self):
        if self.state in [UpdateState.disabled, UpdateState.restart_required]:
            return

        self.state = UpdateState.checking
        result = await self._net.get(f"{self.api_url}/plugin/latest?version={self.current_version}")
        self.latest_version = result.get("version")
        if not self.latest_version:
            self.state = UpdateState.failed
            self.error = "Failed to retrieve plugin update information"
        elif self.latest_version == self.current_version:
            self.state = UpdateState.latest
        elif "url" not in result or "sha256" not in result:
            self.state = UpdateState.failed
            self.error = "Plugin update package is incomplete"
        else:
            self._package = UpdatePackage(
                version=self.latest_version,
                url=result["url"],
                sha256=result["sha256"],
            )
            self.state = UpdateState.available

    def run(self):
        return eventloop.run(self._handle_errors(self._run, "Failed to update plugin"))

    async def _run(self):
        assert self.latest_version and self._package

        self._temp_dir = TemporaryDirectory()
        archive_path = Path(self._temp_dir.name) / f"krita_ai_diffusion-{self.latest_version}.zip"
        log.info(f"Downloading plugin update {self._package.url}")
        self.state = UpdateState.downloading
        archive_data = await self._net.download(self._package.url)

        sha256 = hashlib.sha256(archive_data).hexdigest()
        if sha256 != self._package.sha256:
            log.error(f"Update package hash mismatch: {sha256} != {self._package.sha256}")
            raise RuntimeError(f"Downloaded plugin package is corrupted or incomplete")

        archive_path.write_bytes(archive_data)
        source_dir = Path(self._temp_dir.name) / f"krita_ai_diffusion-{self.latest_version}"
        log.info(f"Extracting plugin archive into {source_dir}")
        self.state = UpdateState.installing
        with ZipFile(archive_path) as zip_file:
            zip_file.extractall(source_dir)

        log.info(f"Installing new plugin version to {self.plugin_dir}")
        shutil.copytree(source_dir, self.plugin_dir, dirs_exist_ok=True)
        self.current_version = self.latest_version
        self.state = UpdateState.restart_required

    @property
    def is_enabled(self):
        return self._is_enabled

    @is_enabled.setter
    def is_enabled(self, value: bool):
        self._is_enabled = value
        if value:
            self.state = UpdateState.unknown
        else:
            self.state = UpdateState.disabled

    @property
    def is_available(self):
        return self.latest_version is not None and self.latest_version != self.current_version

    @property
    def _net(self):
        if self._request_manager is None:
            self._request_manager = RequestManager()
        return self._request_manager

    async def _handle_errors(self, func, message: str):
        try:
            return await func()
        except Exception as e:
            log.exception(e)
            self.error = f"{message}: {e}"
            self.state = UpdateState.failed
            return None
