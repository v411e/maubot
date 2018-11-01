# maubot - A plugin-based Matrix bot system.
# Copyright (C) 2018 Tulir Asokan
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
from typing import Dict, List, Optional
from sqlalchemy.orm import Session
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml import YAML
from asyncio import AbstractEventLoop
import logging
import io

from mautrix.util.config import BaseProxyConfig, RecursiveDict
from mautrix.types import UserID

from .db import DBPlugin
from .config import Config
from .client import Client
from .loader import PluginLoader
from .plugin_base import Plugin

log = logging.getLogger("maubot.plugin")

yaml = YAML()
yaml.indent(4)


class PluginInstance:
    db: Session = None
    mb_config: Config = None
    loop: AbstractEventLoop = None
    cache: Dict[str, 'PluginInstance'] = {}
    plugin_directories: List[str] = []

    log: logging.Logger
    loader: PluginLoader
    client: Client
    plugin: Plugin
    config: BaseProxyConfig
    running: bool

    def __init__(self, db_instance: DBPlugin):
        self.db_instance = db_instance
        self.log = logging.getLogger(f"maubot.plugin.{self.id}")
        self.config = None
        self.running = False
        self.cache[self.id] = self

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "enabled": self.enabled,
            "running": self.running,
            "primary_user": self.primary_user,
        }

    def load(self) -> None:
        try:
            self.loader = PluginLoader.find(self.type)
        except KeyError:
            self.log.error(f"Failed to find loader for type {self.type}")
            self.enabled = False
            return
        self.client = Client.get(self.primary_user)
        if not self.client:
            self.log.error(f"Failed to get client for user {self.primary_user}")
            self.enabled = False
            return
        self.log.debug("Plugin instance dependencies loaded")
        self.loader.references.add(self)
        self.client.references.add(self)

    def delete(self) -> None:
        self.loader.references.remove(self)
        self.db.delete(self.db_instance)
        # TODO delete plugin db

    def load_config(self) -> CommentedMap:
        return yaml.load(self.db_instance.config)

    def save_config(self, data: RecursiveDict[CommentedMap]) -> None:
        buf = io.StringIO()
        yaml.dump(data, buf)
        self.db_instance.config = buf.getvalue()

    async def start(self) -> None:
        if self.running:
            self.log.warning("Ignoring start() call to already started plugin")
            return
        elif not self.enabled:
            self.log.warning("Plugin disabled, not starting.")
            return
        cls = await self.loader.load()
        config_class = cls.get_config_class()
        if config_class:
            try:
                base = await self.loader.read_file("base-config.yaml")
                base_file = RecursiveDict(yaml.load(base.decode("utf-8")), CommentedMap)
            except (FileNotFoundError, KeyError):
                base_file = None
            self.config = config_class(self.load_config, lambda: base_file, self.save_config)
        self.plugin = cls(self.client.client, self.loop, self.client.http_client, self.id,
                          self.log, self.config, self.mb_config["plugin_directories.db"])
        try:
            await self.plugin.start()
        except Exception:
            self.log.exception("Failed to start instance")
            self.enabled = False
            return
        self.running = True
        self.log.info(f"Started instance of {self.loader.id} v{self.loader.version} "
                      f"with user {self.client.id}")

    async def stop(self) -> None:
        if not self.running:
            self.log.warning("Ignoring stop() call to non-running plugin")
            return
        self.log.debug("Stopping plugin instance...")
        self.running = False
        try:
            await self.plugin.stop()
        except Exception:
            self.log.exception("Failed to stop instance")
        self.plugin = None

    @classmethod
    def get(cls, instance_id: str, db_instance: Optional[DBPlugin] = None
            ) -> Optional['PluginInstance']:
        try:
            return cls.cache[instance_id]
        except KeyError:
            db_instance = db_instance or DBPlugin.query.get(instance_id)
            if not db_instance:
                return None
            return PluginInstance(db_instance)

    @classmethod
    def all(cls) -> List['PluginInstance']:
        return [cls.get(plugin.id, plugin) for plugin in DBPlugin.query.all()]

    # region Properties

    @property
    def id(self) -> str:
        return self.db_instance.id

    @id.setter
    def id(self, value: str) -> None:
        self.db_instance.id = value

    @property
    def type(self) -> str:
        return self.db_instance.type

    @property
    def enabled(self) -> bool:
        return self.db_instance.enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self.db_instance.enabled = value

    @property
    def primary_user(self) -> UserID:
        return self.db_instance.primary_user

    @primary_user.setter
    def primary_user(self, value: UserID) -> None:
        self.db_instance.primary_user = value

    # endregion


def init(db: Session, config: Config, loop: AbstractEventLoop):
    PluginInstance.db = db
    PluginInstance.mb_config = config
    PluginInstance.loop = loop
