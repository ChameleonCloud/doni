"""
SQLAlchemy models for hardware data.
"""

from os import path
from urllib import parse as urlparse

from doni.conf import CONF
from oslo_db import options as db_options
from oslo_db.sqlalchemy import models
from oslo_db.sqlalchemy import types as db_types
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, orm, schema
from sqlalchemy.ext.declarative import declarative_base

_DEFAULT_SQL_CONNECTION = "sqlite:///" + path.join("$state_path", "doni.sqlite")


db_options.set_defaults(CONF, connection=_DEFAULT_SQL_CONNECTION)


def table_args():
    engine_name = urlparse.urlparse(CONF.database.connection).scheme
    if engine_name == "mysql":
        return {"mysql_engine": CONF.database.mysql_engine, "mysql_charset": "utf8"}
    return None


class DoniBase(models.TimestampMixin, models.ModelBase):

    metadata = None

    def as_dict(self):
        d = {}
        for c in self.__table__.columns:
            d[c.name] = self[c.name]
        return d


Base = declarative_base(cls=DoniBase)


class Hardware(Base):
    __tablename__ = "hardware"
    __table_args__ = (
        schema.UniqueConstraint("uuid", name="uniq_hardware0uuid"),
        schema.UniqueConstraint("name", name="uniq_hardware0name"),
        table_args(),
    )
    id = Column(Integer, primary_key=True)
    uuid = Column(String(36))
    project_id = Column(String(255))
    hardware_type = Column(String(64))
    name = Column(String(255))
    properties = Column(db_types.JsonEncodedDict)
    workers = orm.relationship(
        "WorkerTask", cascade="all, delete", passive_deletes=True
    )


class WorkerTask(Base):
    __tablename__ = "worker_task"
    __table_args__ = (
        schema.UniqueConstraint("uuid", name="uniq_workers0uuid"),
        schema.UniqueConstraint(
            "hardware_uuid",
            "worker_type",
            name="uniq_workers0hardware_uuid0worker_type",
        ),
        table_args(),
    )
    id = Column(Integer, primary_key=True)
    uuid = Column(String(36))
    hardware_uuid = Column(String(36), ForeignKey("hardware.uuid", ondelete="cascade"))
    worker_type = Column(String(64))
    state = Column(String(15))
    state_details = Column(db_types.JsonEncodedDict)


class AvailabilityWindow(Base):
    __tablename__ = "availability_window"
    __table_args__ = (
        schema.Index("availability_window_hardware_uuid_idx", "hardware_uuid"),
        table_args(),
    )
    id = Column(Integer, primary_key=True)
    uuid = Column(String(36))
    hardware_uuid = Column(String(36), ForeignKey("hardware.uuid"))
    start = Column(DateTime(), nullable=True)
    end = Column(DateTime(), nullable=True)
