from collections import defaultdict, namedtuple
import copy
from datetime import datetime, timezone
import errno
import os
import textwrap
import time
import uuid

import html2text

from truenas_api_client import ReserveFDException

from middlewared.alert.base import (
    AlertCategory,
    alert_category_names,
    AlertClass,
    OneShotAlertClass,
    SimpleOneShotAlertClass,
    DismissableAlertClass,
    AlertLevel,
    Alert,
    AlertSource,
    ThreadedAlertSource,
    ThreadedAlertService,
    ProThreadedAlertService,
)
from middlewared.alert.base import UnavailableException, AlertService as _AlertService
from middlewared.schema import accepts, Any, Bool, Datetime, Dict, Int, List, OROperator, Patch, returns, Ref, Str
from middlewared.service import (
    ConfigService, CRUDService, Service, ValidationErrors,
    job, periodic, private,
)
from middlewared.service_exception import CallError
import middlewared.sqlalchemy as sa
from middlewared.validators import validate_schema
from middlewared.utils import bisect
from middlewared.utils.plugins import load_modules, load_classes
from middlewared.utils.python import get_middlewared_dir

POLICIES = ["IMMEDIATELY", "HOURLY", "DAILY", "NEVER"]
DEFAULT_POLICY = "IMMEDIATELY"

ALERT_SOURCES = {}
ALERT_SERVICES_FACTORIES = {}

AlertSourceLock = namedtuple("AlertSourceLock", ["source_name", "expires_at"])

SEND_ALERTS_ON_READY = False


class AlertModel(sa.Model):
    __tablename__ = 'system_alert'

    id = sa.Column(sa.Integer(), primary_key=True)
    node = sa.Column(sa.String(100))
    source = sa.Column(sa.Text())
    key = sa.Column(sa.Text())
    datetime = sa.Column(sa.DateTime())
    last_occurrence = sa.Column(sa.DateTime())
    text = sa.Column(sa.Text())
    args = sa.Column(sa.JSON(None))
    dismissed = sa.Column(sa.Boolean())
    uuid = sa.Column(sa.Text())
    klass = sa.Column(sa.Text())


class AlertSourceRunFailedAlertClass(AlertClass):
    category = AlertCategory.SYSTEM
    level = AlertLevel.CRITICAL
    title = "Alert Check Failed"
    text = "Failed to check for alert %(source_name)s: %(traceback)s"

    exclude_from_list = True


class AlertSourceRunFailedOnBackupNodeAlertClass(AlertClass):
    category = AlertCategory.SYSTEM
    level = AlertLevel.CRITICAL
    title = "Alert Check Failed (Standby Controller)"
    text = "Failed to check for alert %(source_name)s on standby controller: %(traceback)s"

    exclude_from_list = True


class AutomaticAlertFailedAlertClass(AlertClass, SimpleOneShotAlertClass):
    category = AlertCategory.SYSTEM
    level = AlertLevel.WARNING
    title = "Failed to Notify iXsystems About Alert"
    text = textwrap.dedent("""\
        Creating an automatic alert for iXsystems about system %(serial)s failed: %(error)s.
        Please contact iXsystems Support: https://www.ixsystems.com/support/

        Alert:

        %(alert)s
    """)

    exclude_from_list = True

    deleted_automatically = False


class TestAlertClass(AlertClass):
    category = AlertCategory.SYSTEM
    level = AlertLevel.CRITICAL
    title = "Test alert"

    exclude_from_list = True


class AlertPolicy:
    def __init__(self, key=lambda now: now):
        self.key = key

        self.last_key_value = None
        self.last_key_value_alerts = {}

    def receive_alerts(self, now, alerts):
        alerts = {alert.uuid: alert for alert in alerts}
        gone_alerts = []
        new_alerts = []
        key = self.key(now)
        if key != self.last_key_value:
            gone_alerts = [alert for alert in self.last_key_value_alerts.values() if alert.uuid not in alerts]
            new_alerts = [alert for alert in alerts.values() if alert.uuid not in self.last_key_value_alerts]

            self.last_key_value = key
            self.last_key_value_alerts = alerts

        return gone_alerts, new_alerts

    def delete_alert(self, alert):
        self.last_key_value_alerts.pop(alert.uuid, None)


def get_alert_level(alert, classes):
    return AlertLevel[classes.get(alert.klass.name, {}).get("level", alert.klass.level.name)]


def get_alert_policy(alert, classes):
    return classes.get(alert.klass.name, {}).get("policy", DEFAULT_POLICY)


class AlertSerializer:
    def __init__(self, middleware):
        self.middleware = middleware

        self.initialized = False
        self.product_type = None
        self.classes = None
        self.nodes = None

    async def serialize(self, alert):
        await self._ensure_initialized()

        return dict(
            alert.__dict__,
            id=alert.uuid,
            node=self.nodes[alert.node],
            klass=alert.klass.name,
            level=self.classes.get(alert.klass.name, {}).get("level", alert.klass.level.name),
            formatted=alert.formatted,
            one_shot=issubclass(alert.klass, OneShotAlertClass) and not alert.klass.deleted_automatically
        )

    async def get_alert_class(self, alert):
        await self._ensure_initialized()
        return self.classes.get(alert.klass.name, {})

    async def should_show_alert(self, alert):
        await self._ensure_initialized()

        if self.product_type not in alert.klass.products:
            return False

        if (await self.get_alert_class(alert)).get("policy") == "NEVER":
            return False

        return True

    async def _ensure_initialized(self):
        if not self.initialized:
            self.product_type = await self.middleware.call("alert.product_type")
            self.classes = (await self.middleware.call("alertclasses.config"))["classes"]
            self.nodes = await self.middleware.call("alert.node_map")

            self.initialized = True


class AlertService(Service):
    alert_sources_errors = set()

    class Config:
        cli_namespace = "system.alert"

    def __init__(self, middleware):
        super().__init__(middleware)

        self.blocked_sources = defaultdict(set)
        self.sources_locks = {}

        self.blocked_failover_alerts_until = 0

        self.sources_run_times = defaultdict(lambda: {
            "last": [],
            "max": 0,
            "total_count": 0,
            "total_time": 0,
        })

    @private
    def load_impl(self):
        for module in load_modules(os.path.join(get_middlewared_dir(), "alert", "source")):
            for cls in load_classes(module, AlertSource, (ThreadedAlertSource,)):
                source = cls(self.middleware)
                if source.name in ALERT_SOURCES:
                    raise RuntimeError(f"Alert source {source.name} is already registered")
                ALERT_SOURCES[source.name] = source

        for module in load_modules(
            os.path.join(os.path.dirname(os.path.realpath(__file__)), os.path.pardir, "alert", "service")
        ):
            for cls in load_classes(module, _AlertService, (ThreadedAlertService, ProThreadedAlertService)):
                ALERT_SERVICES_FACTORIES[cls.name()] = cls

    @private
    async def load(self):
        await self.middleware.run_in_thread(self.load_impl)

    @private
    async def initialize(self, load=True):
        is_enterprise = await self.middleware.call("system.is_enterprise")

        self.node = "A"
        if is_enterprise:
            if await self.middleware.call("failover.node") == "B":
                self.node = "B"

        self.alerts = []
        if load:
            alerts_uuids = set()
            alerts_by_classes = defaultdict(list)
            for alert in await self.middleware.call("datastore.query", "system.alert"):
                del alert["id"]

                if alert["source"] and alert["source"] not in ALERT_SOURCES:
                    self.logger.info("Alert source %r is no longer present", alert["source"])
                    continue

                try:
                    alert["klass"] = AlertClass.class_by_name[alert["klass"]]
                except KeyError:
                    self.logger.info("Alert class %r is no longer present", alert["klass"])
                    continue

                alert["_uuid"] = alert.pop("uuid")
                alert["_source"] = alert.pop("source")
                alert["_key"] = alert.pop("key")
                alert["_text"] = alert.pop("text")

                alert = Alert(**alert)

                if alert.uuid not in alerts_uuids:
                    alerts_uuids.add(alert.uuid)
                    alerts_by_classes[alert.klass.__name__].append(alert)

            for alerts in alerts_by_classes.values():
                if isinstance(alerts[0].klass, OneShotAlertClass):
                    alerts = await alerts[0].klass.load(alerts)

                self.alerts.extend(alerts)
        else:
            await self.flush_alerts()

        self.alert_source_last_run = defaultdict(lambda: datetime.min)

        self.policies = {
            "IMMEDIATELY": AlertPolicy(),
            "HOURLY": AlertPolicy(lambda d: (d.date(), d.hour)),
            "DAILY": AlertPolicy(lambda d: (d.date())),
            "NEVER": AlertPolicy(lambda d: None),
        }
        for policy in self.policies.values():
            policy.receive_alerts(datetime.utcnow(), self.alerts)

    @private
    async def terminate(self):
        await self.flush_alerts()

    @accepts(roles=['ALERT_LIST_READ'])
    @returns(List('alert_policies', items=[Str('policy', enum=POLICIES)]))
    async def list_policies(self):
        """
        List all alert policies which indicate the frequency of the alerts.
        """
        return POLICIES

    @accepts(roles=['ALERT_LIST_READ'])
    @returns(List('categories', items=[Dict(
        'category',
        Str('id'),
        Str('title'),
        List('classes', items=[Dict(
            'category_class',
            Str('id'),
            Str('title'),
            Str('level'),
            Bool('proactive_support'),
        )])
    )]))
    async def list_categories(self):
        """
        List all types of alerts which the system can issue.
        """

        product_type = await self.middleware.call("alert.product_type")

        classes = [alert_class for alert_class in AlertClass.classes
                   if product_type in alert_class.products and not alert_class.exclude_from_list]

        return [
            {
                "id": alert_category.name,
                "title": alert_category_names[alert_category],
                "classes": sorted(
                    [
                        {
                            "id": alert_class.name,
                            "title": alert_class.title,
                            "level": alert_class.level.name,
                            "proactive_support": alert_class.proactive_support,
                        }
                        for alert_class in classes
                        if alert_class.category == alert_category
                    ],
                    key=lambda klass: klass["title"]
                )
            }
            for alert_category in AlertCategory
            if any(alert_class.category == alert_category for alert_class in classes)
        ]

    @accepts(roles=['ALERT_LIST_READ'])
    @returns(List('alerts', items=[Dict(
        'alert',
        Str('uuid'),
        Str('source'),
        Str('klass'),
        Any('args'),
        Str('node'),
        Str('key', max_length=None),
        Datetime('datetime'),
        Datetime('last_occurrence'),
        Bool('dismissed'),
        Any('mail', null=True),
        Str('text', max_length=None),
        Str('id'),
        Str('level'),
        Str('formatted', null=True, max_length=None),
        Bool('one_shot'),
        register=True,
    )]))
    async def list(self):
        """
        List all types of alerts including active/dismissed currently in the system.
        """

        as_ = AlertSerializer(self.middleware)
        classes = (await self.middleware.call("alertclasses.config"))["classes"]

        return [
            await as_.serialize(alert)
            for alert in sorted(
                self.alerts,
                key=lambda alert: (
                    -get_alert_level(alert, classes).value,
                    alert.klass.title,
                    alert.datetime,
                ),
            )
            if await as_.should_show_alert(alert)
        ]

    @private
    async def node_map(self):
        nodes = {
            'A': 'Controller A',
            'B': 'Controller B',
        }
        if await self.middleware.call('failover.licensed'):
            node = await self.middleware.call('failover.node')
            status = await self.middleware.call('failover.status')
            if status == 'MASTER':
                if node == 'A':
                    nodes = {
                        'A': 'Active Controller (A)',
                        'B': 'Standby Controller (B)',
                    }
                else:
                    nodes = {
                        'A': 'Standby Controller (A)',
                        'B': 'Active Controller (B)',
                    }
            else:
                nodes[node] = f'{status.title()} Controller ({node})'

        return nodes

    def __alert_by_uuid(self, uuid):
        try:
            return [a for a in self.alerts if a.uuid == uuid][0]
        except IndexError:
            return None

    @accepts(Str("uuid"))
    @returns()
    async def dismiss(self, uuid):
        """
        Dismiss `id` alert.
        """

        alert = self.__alert_by_uuid(uuid)
        if alert is None:
            return

        if issubclass(alert.klass, DismissableAlertClass):
            related_alerts, unrelated_alerts = bisect(lambda a: (a.node, a.klass) == (alert.node, alert.klass),
                                                      self.alerts)
            left_alerts = await alert.klass(self.middleware).dismiss(related_alerts, alert)
            for deleted_alert in related_alerts:
                if deleted_alert not in left_alerts:
                    self._delete_on_dismiss(deleted_alert)
        elif issubclass(alert.klass, OneShotAlertClass) and not alert.klass.deleted_automatically:
            self._delete_on_dismiss(alert)
        else:
            alert.dismissed = True
            await self._send_alert_changed_event(alert)

    def _delete_on_dismiss(self, alert):
        try:
            self.alerts.remove(alert)
            removed = True
        except ValueError:
            removed = False

        for policy in self.policies.values():
            policy.delete_alert(alert)

        if removed:
            self._send_alert_deleted_event(alert)

    @accepts(Str("uuid"))
    @returns()
    async def restore(self, uuid):
        """
        Restore `id` alert which had been dismissed.
        """

        alert = self.__alert_by_uuid(uuid)
        if alert is None:
            return

        alert.dismissed = False

        await self._send_alert_changed_event(alert)

    async def _send_alert_changed_event(self, alert):
        as_ = AlertSerializer(self.middleware)
        if await as_.should_show_alert(alert):
            self.middleware.send_event("alert.list", "CHANGED", id=alert.uuid, fields=await as_.serialize(alert))

    def _send_alert_deleted_event(self, alert):
        self.middleware.send_event("alert.list", "REMOVED", id=alert.uuid)

    @periodic(60)
    @private
    @job(lock="process_alerts", transient=True, lock_queue_size=1)
    async def process_alerts(self, job):
        if not await self.__should_run_or_send_alerts():
            return

        valid_alerts = copy.deepcopy(self.alerts)
        await self.__run_alerts()

        self.__expire_alerts()

        if not await self.__should_run_or_send_alerts():
            self.alerts = valid_alerts
            return

        await self.middleware.call("alert.send_alerts")

    @private
    @job(lock="process_alerts", transient=True)
    async def send_alerts(self, job):
        global SEND_ALERTS_ON_READY

        if await self.middleware.call("system.state") != "READY":
            SEND_ALERTS_ON_READY = True
            return

        product_type = await self.middleware.call("alert.product_type")
        classes = (await self.middleware.call("alertclasses.config"))["classes"]

        now = datetime.utcnow()
        for policy_name, policy in self.policies.items():
            gone_alerts, new_alerts = policy.receive_alerts(now, self.alerts)

            for alert_service_desc in await self.middleware.call("datastore.query", "system.alertservice",
                                                                 [["enabled", "=", True]]):
                service_level = AlertLevel[alert_service_desc["level"]]

                service_alerts = [
                    alert for alert in self.alerts
                    if (
                        product_type in alert.klass.products and
                        get_alert_level(alert, classes).value >= service_level.value and
                        get_alert_policy(alert, classes) != "NEVER"
                    )
                ]
                service_gone_alerts = [
                    alert for alert in gone_alerts
                    if (
                        product_type in alert.klass.products and
                        get_alert_level(alert, classes).value >= service_level.value and
                        get_alert_policy(alert, classes) == policy_name
                    )
                ]
                service_new_alerts = [
                    alert for alert in new_alerts
                    if (
                        product_type in alert.klass.products and
                        get_alert_level(alert, classes).value >= service_level.value and
                        get_alert_policy(alert, classes) == policy_name
                    )
                ]
                for gone_alert in list(service_gone_alerts):
                    for new_alert in service_new_alerts:
                        if gone_alert.klass == new_alert.klass and gone_alert.key == new_alert.key:
                            service_gone_alerts.remove(gone_alert)
                            service_new_alerts.remove(new_alert)
                            break

                if not service_gone_alerts and not service_new_alerts:
                    continue

                factory = ALERT_SERVICES_FACTORIES.get(alert_service_desc["type"])
                if factory is None:
                    self.logger.error("Alert service %r does not exist", alert_service_desc["type"])
                    continue

                try:
                    alert_service = factory(self.middleware, alert_service_desc["attributes"])
                except Exception:
                    self.logger.error("Error creating alert service %r with parameters=%r",
                                      alert_service_desc["type"], alert_service_desc["attributes"], exc_info=True)
                    continue

                alerts = [alert for alert in service_alerts if not alert.dismissed]
                service_gone_alerts = [alert for alert in service_gone_alerts if not alert.dismissed]
                service_new_alerts = [alert for alert in service_new_alerts if not alert.dismissed]

                if alerts or service_gone_alerts or service_new_alerts:
                    try:
                        await alert_service.send(alerts, service_gone_alerts, service_new_alerts)
                    except Exception:
                        self.logger.error("Error in alert service %r", alert_service_desc["type"], exc_info=True)

            if policy_name == "IMMEDIATELY":
                as_ = AlertSerializer(self.middleware)
                for alert in gone_alerts:
                    if await as_.should_show_alert(alert):
                        self._send_alert_deleted_event(alert)
                for alert in new_alerts:
                    if await as_.should_show_alert(alert):
                        self.middleware.send_event(
                            "alert.list", "ADDED", id=alert.uuid, fields=await as_.serialize(alert),
                        )

                for alert in new_alerts:
                    if alert.mail:
                        await self.middleware.call("mail.send", alert.mail)

                if await self.middleware.call("system.is_enterprise"):
                    gone_proactive_support_alerts = [
                        alert
                        for alert in gone_alerts
                        if (
                            alert.klass.proactive_support and
                            (await as_.get_alert_class(alert)).get("proactive_support", True) and
                            alert.klass.proactive_support_notify_gone
                        )
                    ]
                    new_proactive_support_alerts = [
                        alert
                        for alert in new_alerts
                        if (
                            alert.klass.proactive_support and
                            (await as_.get_alert_class(alert)).get("proactive_support", True)
                        )
                    ]
                    if gone_proactive_support_alerts or new_proactive_support_alerts:
                        if await self.middleware.call("support.is_available_and_enabled"):
                            support = await self.middleware.call("support.config")

                            msg = []
                            if gone_proactive_support_alerts:
                                msg.append("The following alerts were cleared:")
                                msg += [f"* {html2text.html2text(alert.formatted)}"
                                        for alert in gone_proactive_support_alerts]
                            if new_proactive_support_alerts:
                                msg.append("The following new alerts appeared:")
                                msg += [f"* {html2text.html2text(alert.formatted)}"
                                        for alert in new_proactive_support_alerts]

                            serial = (await self.middleware.call("system.dmidecode_info"))["system-serial-number"]

                            for name, verbose_name in await self.middleware.call("support.fields"):
                                value = support[name]
                                if value:
                                    msg += ["", "{}: {}".format(verbose_name, value)]

                            msg = "\n".join(msg)

                            job = await self.middleware.call("support.new_ticket", {
                                "title": "Automatic alert (%s)" % serial,
                                "body": msg,
                                "attach_debug": False,
                                "category": "Hardware",
                                "criticality": "Loss of Functionality",
                                "environment": "Production",
                                "name": "Automatic Alert",
                                "email": "auto-support@ixsystems.com",
                                "phone": "-",
                            })
                            await job.wait()
                            if job.error:
                                await self.middleware.call("alert.oneshot_create", "AutomaticAlertFailed",
                                                           {"serial": serial, "alert": msg, "error": str(job.error)})

    def __uuid(self):
        return str(uuid.uuid4())

    async def __should_run_or_send_alerts(self):
        if await self.middleware.call('system.state') != 'READY':
            return False

        if await self.middleware.call('failover.licensed'):
            status = await self.middleware.call('failover.status')
            if status == 'BACKUP' or await self.middleware.call('failover.in_progress'):
                return False

        return True

    async def __run_alerts(self):
        master_node = "A"
        backup_node = "B"
        product_type = await self.middleware.call("alert.product_type")
        run_on_backup_node = False
        run_failover_related = False
        if product_type == "SCALE_ENTERPRISE":
            if await self.middleware.call("failover.licensed"):
                if await self.middleware.call("failover.node") == "B":
                    master_node = "B"
                    backup_node = "A"
                try:
                    remote_version = await self.middleware.call("failover.call_remote", "system.version")
                    remote_system_state = await self.middleware.call("failover.call_remote", "system.state")
                    remote_failover_status = await self.middleware.call("failover.call_remote",
                                                                        "failover.status")
                except Exception:
                    pass
                else:
                    if remote_version == await self.middleware.call("system.version"):
                        if remote_system_state == "READY" and remote_failover_status == "BACKUP":
                            run_on_backup_node = True

            run_failover_related = time.monotonic() > self.blocked_failover_alerts_until

        for k, source_lock in list(self.sources_locks.items()):
            if source_lock.expires_at <= time.monotonic():
                await self.unblock_source(k)

        for alert_source in ALERT_SOURCES.values():
            if product_type not in alert_source.products:
                continue

            if alert_source.failover_related and not run_failover_related:
                continue

            if not alert_source.schedule.should_run(datetime.utcnow(), self.alert_source_last_run[alert_source.name]):
                continue

            self.alert_source_last_run[alert_source.name] = datetime.utcnow()

            alerts_a = [alert
                        for alert in self.alerts
                        if alert.node == master_node and alert.source == alert_source.name]
            locked = False
            if self.blocked_sources[alert_source.name]:
                self.logger.debug("Not running alert source %r because it is blocked", alert_source.name)
                locked = True
            else:
                self.logger.trace("Running alert source: %r", alert_source.name)

                try:
                    alerts_a = await self.__run_source(alert_source.name)
                except UnavailableException:
                    pass
            for alert in alerts_a:
                alert.node = master_node

            alerts_b = []
            if run_on_backup_node and alert_source.run_on_backup_node:
                try:
                    alerts_b = [alert
                                for alert in self.alerts
                                if alert.node == backup_node and alert.source == alert_source.name]
                    try:
                        if not locked:
                            alerts_b = await self.middleware.call("failover.call_remote", "alert.run_source",
                                                                  [alert_source.name])

                            alerts_b = [Alert(**dict({k: v for k, v in alert.items()
                                                      if k in ["args", "datetime", "last_occurrence", "dismissed",
                                                               "mail"]},
                                                     klass=AlertClass.class_by_name[alert["klass"]],
                                                     _source=alert["source"],
                                                     _key=alert["key"]))
                                        for alert in alerts_b]
                    except CallError as e:
                        if e.errno in [errno.ECONNABORTED, errno.ECONNREFUSED, errno.ECONNRESET, errno.EHOSTDOWN,
                                       errno.ETIMEDOUT, CallError.EALERTCHECKERUNAVAILABLE]:
                            pass
                        else:
                            raise
                except ReserveFDException:
                    self.logger.debug('Failed to reserve a privileged port')
                except Exception as e:
                    alerts_b = [
                        Alert(AlertSourceRunFailedOnBackupNodeAlertClass,
                              args={
                                  "source_name": alert_source.name,
                                  "traceback": str(e),
                              },
                              _source=alert_source.name)
                    ]

            for alert in alerts_b:
                alert.node = backup_node

            for alert in alerts_a + alerts_b:
                self.__handle_alert(alert)

            self.alerts = (
                [a for a in self.alerts if a.source != alert_source.name] +
                alerts_a +
                alerts_b
            )

    def __handle_alert(self, alert):
        try:
            existing_alert = [
                a for a in self.alerts
                if (a.node, a.source, a.klass, a.key) == (alert.node, alert.source, alert.klass, alert.key)
            ][0]
        except IndexError:
            existing_alert = None

        if existing_alert is None:
            alert.uuid = self.__uuid()
        else:
            alert.uuid = existing_alert.uuid
        if existing_alert is None:
            alert.datetime = alert.datetime or datetime.utcnow()
            if alert.datetime.tzinfo is not None:
                alert.datetime = alert.datetime.astimezone(timezone.utc).replace(tzinfo=None)
        else:
            alert.datetime = existing_alert.datetime
        alert.last_occurrence = datetime.utcnow()
        if existing_alert is None:
            alert.dismissed = False
        else:
            alert.dismissed = existing_alert.dismissed

    def __expire_alerts(self):
        self.alerts = list(filter(lambda alert: not self.__should_expire_alert(alert), self.alerts))

    def __should_expire_alert(self, alert):
        if issubclass(alert.klass, OneShotAlertClass):
            if alert.klass.expires_after is not None:
                return alert.last_occurrence < datetime.utcnow() - alert.klass.expires_after

        return False

    @private
    async def sources_stats(self):
        return {
            k: {"avg": v["total_time"] / v["total_count"] if v["total_count"] != 0 else 0, **v}
            for k, v in sorted(self.sources_run_times.items(), key=lambda t: t[0])
        }

    @private
    async def run_source(self, source_name):
        try:
            return [dict(alert.__dict__, klass=alert.klass.name)
                    for alert in await self.__run_source(source_name)]
        except UnavailableException:
            raise CallError("This alert checker is unavailable", CallError.EALERTCHECKERUNAVAILABLE)

    @private
    async def block_source(self, source_name, timeout=3600):
        if source_name not in ALERT_SOURCES:
            raise CallError("Invalid alert source")

        lock = str(uuid.uuid4())
        self.blocked_sources[source_name].add(lock)
        self.sources_locks[lock] = AlertSourceLock(source_name, time.monotonic() + timeout)
        return lock

    @private
    async def unblock_source(self, lock):
        source_lock = self.sources_locks.pop(lock, None)
        if source_lock:
            self.blocked_sources[source_lock.source_name].remove(lock)

    @private
    async def block_failover_alerts(self):
        # This values come from observation from support of how long a M-series boot can take.
        self.blocked_failover_alerts_until = time.monotonic() + 900

    async def __run_source(self, source_name):
        alert_source = ALERT_SOURCES[source_name]

        start = time.monotonic()
        try:
            alerts = (await alert_source.check()) or []
        except UnavailableException:
            raise
        except Exception as e:
            if source_name not in self.alert_sources_errors:
                self.logger.error("Error checking for alert %r", alert_source.name, exc_info=True)
                self.alert_sources_errors.add(source_name)

            alerts = [
                Alert(AlertSourceRunFailedAlertClass,
                      args={
                          "source_name": alert_source.name,
                          "traceback": str(e),
                      })
            ]
        else:
            self.alert_sources_errors.discard(source_name)
            if not isinstance(alerts, list):
                alerts = [alerts]
        finally:
            run_time = time.monotonic() - start
            source_stat = self.sources_run_times[source_name]
            source_stat["last"] = source_stat["last"][-9:] + [run_time]
            source_stat["max"] = max(source_stat["max"], run_time)
            source_stat["total_count"] += 1
            source_stat["total_time"] += run_time

        keys = set()
        unique_alerts = []
        for alert in alerts:
            if alert.key in keys:
                continue

            keys.add(alert.key)
            unique_alerts.append(alert)
        alerts = unique_alerts

        for alert in alerts:
            alert.source = source_name

        return alerts

    @periodic(3600, run_on_start=False)
    @private
    async def flush_alerts(self):
        if await self.middleware.call('failover.licensed'):
            if await self.middleware.call('failover.status') == 'BACKUP':
                return

        await self.middleware.call("datastore.delete", "system.alert", [])

        for alert in self.alerts:
            d = alert.__dict__.copy()
            d["klass"] = d["klass"].name
            del d["mail"]
            await self.middleware.call("datastore.insert", "system.alert", d)

    @private
    @accepts(Str("klass"), Any("args", null=True))
    @job(lock="process_alerts", transient=True)
    async def oneshot_create(self, job, klass, args):
        """
        Creates a one-shot alert of specified `klass`, passing `args` to `klass.create` method.

        Normal alert creation logic will be applied, so if you create an alert with the same `key` as an already
        existing alert, no duplicate alert will be created.

        :param klass: one-shot alert class name (without the `AlertClass` suffix).
        :param args: `args` that will be passed to `klass.create` method.
        """

        try:
            klass = AlertClass.class_by_name[klass]
        except KeyError:
            raise CallError(f"Invalid alert class: {klass!r}")

        if not issubclass(klass, OneShotAlertClass):
            raise CallError(f"Alert class {klass!r} is not a one-shot alert class")

        alert = await klass(self.middleware).create(args)
        if alert is None:
            return

        alert.source = ""
        alert.klass = alert.klass

        alert.node = self.node

        self.__handle_alert(alert)

        self.alerts = [a for a in self.alerts if a.uuid != alert.uuid] + [alert]

        await self.middleware.call("alert.send_alerts")

    @private
    @accepts(
        OROperator(
            Str("klass"),
            List('klass', items=[Str('klassname')], default=None),
        ),
        Any("query", null=True, default=None))
    @job(lock="process_alerts", transient=True)
    async def oneshot_delete(self, job, klass, query):
        """
        Deletes one-shot alerts of specified `klass` or klasses, passing `query`
        to `klass.delete` method.

        It's not an error if no alerts matching delete `query` exist.

        :param klass: either one-shot alert class name (without the `AlertClass` suffix), or list thereof.
        :param query: `query` that will be passed to `klass.delete` method.
        """

        if isinstance(klass, list):
            klasses = klass
        else:
            klasses = [klass]

        deleted = False
        for klassname in klasses:
            try:
                klass = AlertClass.class_by_name[klassname]
            except KeyError:
                raise CallError(f"Invalid alert source: {klassname!r}")

            if not issubclass(klass, OneShotAlertClass):
                raise CallError(f"Alert class {klassname!r} is not a one-shot alert source")

            related_alerts, unrelated_alerts = bisect(lambda a: (a.node, a.klass) == (self.node, klass),
                                                      self.alerts)
            left_alerts = await klass(self.middleware).delete(related_alerts, query)
            for deleted_alert in related_alerts:
                if deleted_alert not in left_alerts:
                    self.alerts.remove(deleted_alert)
                    deleted = True

        if deleted:
            # We need to flush alerts to the database immediately after deleting oneshot alerts.
            # Some oneshot alerts can only de deleted programmatically (i.e. cloud sync oneshot alerts are deleted
            # when deleting cloud sync task). If we delete a cloud sync task and then reboot the system abruptly,
            # the alerts won't be flushed to the database and on next boot an alert for nonexisting cloud sync task
            # will appear, and it won't be deletable.
            await self.middleware.call("alert.flush_alerts")

            await self.middleware.call("alert.send_alerts")

    @private
    def alert_source_clear_run(self, name):
        alert_source = ALERT_SOURCES.get(name)
        if not alert_source:
            raise CallError(f"Alert source {name!r} not found.", errno.ENOENT)

        self.alert_source_last_run[alert_source.name] = datetime.min

    @private
    async def product_type(self):
        return await self.middleware.call("system.product_type")


class AlertServiceModel(sa.Model):
    __tablename__ = 'system_alertservice'

    id = sa.Column(sa.Integer(), primary_key=True)
    name = sa.Column(sa.String(120))
    type = sa.Column(sa.String(20))
    attributes = sa.Column(sa.JSON())
    enabled = sa.Column(sa.Boolean())
    level = sa.Column(sa.String(20))


class AlertServiceService(CRUDService):
    class Config:
        datastore = "system.alertservice"
        datastore_extend = "alertservice._extend"
        datastore_order_by = ["name"]
        cli_namespace = "system.alert.service"

    ENTRY = Patch(
        'alert_service_create', 'alertservice_entry',
        ('add', Int('id')),
        ('add', Str('type__title')),
    )

    @accepts()
    @returns(List('alert_service_types', items=[Dict(
        'alert_service_type',
        Str('name', required=True),
        Str('title', required=True),
    )]))
    async def list_types(self):
        """
        List all types of supported Alert services which can be configured with the system.
        """
        return [
            {
                "name": name,
                "title": factory.title,
            }
            for name, factory in sorted(ALERT_SERVICES_FACTORIES.items(), key=lambda i: i[1].title.lower())
        ]

    @private
    async def _extend(self, service):
        try:
            service["type__title"] = ALERT_SERVICES_FACTORIES[service["type"]].title
        except KeyError:
            service["type__title"] = "<Unknown>"

        return service

    @private
    async def _compress(self, service):
        service.pop("type__title")

        return service

    @private
    async def _validate(self, service, schema_name):
        verrors = ValidationErrors()

        factory = ALERT_SERVICES_FACTORIES.get(service["type"])
        if factory is None:
            verrors.add(f"{schema_name}.type", "This field has invalid value")
            raise verrors

        verrors.add_child(f"{schema_name}.attributes",
                          validate_schema(list(factory.schema.attrs.values()), service["attributes"]))

        verrors.check()

    @accepts(Dict(
        "alert_service_create",
        Str("name", required=True, empty=False),
        Str("type", required=True),
        Dict("attributes", required=True, additional_attrs=True),
        Str("level", required=True, enum=list(AlertLevel.__members__)),
        Bool("enabled", default=True),
        register=True,
    ))
    async def do_create(self, data):
        """
        Create an Alert Service of specified `type`.

        If `enabled`, it sends alerts to the configured `type` of Alert Service.

        .. examples(websocket)::

          Create an Alert Service of Mail `type`

            :::javascript
            {
                "id": "6841f242-840a-11e6-a437-00e04d680384",
                "msg": "method",
                "method": "alertservice.create",
                "params": [{
                    "name": "Test Email Alert",
                    "enabled": true,
                    "type": "Mail",
                    "attributes": {
                        "email": "dev@ixsystems.com"
                    },
                    "settings": {
                        "VolumeVersion": "HOURLY"
                    }
                }]
            }
        """
        await self._validate(data, "alert_service_create")

        data["id"] = await self.middleware.call("datastore.insert", self._config.datastore, data)

        await self._extend(data)

        return await self.get_instance(data["id"])

    @accepts(Int("id"), Patch(
        "alert_service_create",
        "alert_service_update",
        ("attr", {"update": True}),
    ))
    async def do_update(self, id_, data):
        """
        Update Alert Service of `id`.
        """
        old = await self.middleware.call("datastore.query", self._config.datastore, [("id", "=", id_)],
                                         {"extend": self._config.datastore_extend,
                                          "get": True})

        new = old.copy()
        new.update(data)

        await self._validate(new, "alert_service_update")

        await self._compress(new)

        await self.middleware.call("datastore.update", self._config.datastore, id_, new)

        return await self.get_instance(id_)

    @accepts(Int("id"))
    async def do_delete(self, id_):
        """
        Delete Alert Service of `id`.
        """
        return await self.middleware.call("datastore.delete", self._config.datastore, id_)

    @accepts(
        Ref('alert_service_create')
    )
    @returns(Bool('successful_test', description='Is `true` if test is successful'))
    async def test(self, data):
        """
        Send a test alert using `type` of Alert Service.

        .. examples(websocket)::

          Send a test alert using Alert Service of Mail `type`.

            :::javascript
            {
                "id": "6841f242-840a-11e6-a437-00e04d680384",
                "msg": "method",
                "method": "alertservice.test",
                "params": [{
                    "name": "Test Email Alert",
                    "enabled": true,
                    "type": "Mail",
                    "attributes": {
                        "email": "dev@ixsystems.com"
                    },
                    "settings": {}
                }]
            }
        """
        await self._validate(data, "alert_service_test")

        factory = ALERT_SERVICES_FACTORIES.get(data["type"])
        if factory is None:
            self.logger.error("Alert service %r does not exist", data["type"])
            return False

        try:
            alert_service = factory(self.middleware, data["attributes"])
        except Exception:
            self.logger.error("Error creating alert service %r with parameters=%r",
                              data["type"], data["attributes"], exc_info=True)
            return False

        master_node = "A"
        if await self.middleware.call("failover.licensed"):
            master_node = await self.middleware.call("failover.node")

        test_alert = Alert(
            TestAlertClass,
            node=master_node,
            datetime=datetime.utcnow(),
            last_occurrence=datetime.utcnow(),
            _uuid=str(uuid.uuid4()),
        )

        try:
            await alert_service.send([test_alert], [], [test_alert])
        except Exception:
            self.logger.error("Error in alert service %r", data["type"], exc_info=True)
            return False

        return True


class AlertClassesModel(sa.Model):
    __tablename__ = 'system_alertclasses'

    id = sa.Column(sa.Integer(), primary_key=True)
    classes = sa.Column(sa.JSON())


class AlertClassesService(ConfigService):
    class Config:
        datastore = "system.alertclasses"
        cli_namespace = "system.alert.class"

    ENTRY = Dict(
        "alertclasses_entry",
        Int("id"),
        Dict("classes", additional_attrs=True),
    )

    async def do_update(self, data):
        """
        Update default Alert settings.

        .. examples(rest)::

        Set ClassName's level to LEVEL and policy to POLICY. Reset settings for other alert classes.

        {
            "classes": {
                "ClassName": {
                    "level": "LEVEL",
                    "policy": "POLICY",
                }
            }
        }
        """
        old = await self.config()

        new = old.copy()
        new.update(data)

        verrors = ValidationErrors()

        for k, v in new["classes"].items():
            if k not in AlertClass.class_by_name:
                verrors.add(f"alert_class_update.classes.{k}", "This alert class does not exist")

            verrors.add_child(
                f"alert_class_update.classes.{k}",
                validate_schema([
                    Str("level", enum=list(AlertLevel.__members__)),
                    Str("policy", enum=POLICIES),
                    Bool("proactive_support"),
                ], v),
            )

            if "proactive_support" in v and not AlertClass.class_by_name[k].proactive_support:
                verrors.add(
                    f"alert_class_update.classes.{k}.proactive_support",
                    "Proactive support is not supported by this alert class",
                )

        verrors.check()

        await self.middleware.call("datastore.update", self._config.datastore, old["id"], new)

        return await self.config()


async def _event_system(middleware, event_type, args):
    if SEND_ALERTS_ON_READY:
        await middleware.call("alert.send_alerts")


async def setup(middleware):
    middleware.event_register("alert.list", "Sent on alert changes.", roles=["ALERT_LIST_READ"])

    await middleware.call("alert.load")
    await middleware.call("alert.initialize")

    middleware.event_subscribe("system.ready", _event_system)
