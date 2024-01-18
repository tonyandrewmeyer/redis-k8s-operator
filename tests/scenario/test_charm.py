#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

import contextlib
import grp
import pathlib
import pwd
import socket
import stat
import tempfile

# Remove this.
from unittest import mock

import ops
import ops.pebble
import pytest
import scenario
from ops.charm import RelationDepartedEvent
from ops.testing import Harness
from redis import Redis
from redis.exceptions import RedisError

from charm import RedisK8sCharm
from literals import REDIS_PORT
from tests.helpers import APPLICATION_DATA


def setUp(self):
    self._peer_relation = "redis-peers"

    self.harness = Harness(RedisK8sCharm)
    self.addCleanup(self.harness.cleanup)
    self.harness.set_can_connect("redis", True)
    self.harness.set_can_connect("sentinel", True)
    self.harness.begin()
    self.harness.add_relation(self._peer_relation, self.harness.charm.app.name)


class MockRedis:
    def __init__(self, mock_info):
        self.mock_info = mock_info

    def info(self, key: str):
        return self.mock_info[key]


# SCENARIO-NOTE: the charm does `model.resources.fetch(...)` and this results
# in a Scenario NotImplementedError, although it's avoided by mocking out the
# redis client.
def test_on_update_status_success_leader(monkeypatch):
    @contextlib.contextmanager
    def redis_client(hostname="localhost"):
        yield MockRedis(mock_info={"server": {"redis_version": "6.0.11"}})

    monkeypatch.setattr(RedisK8sCharm, "_redis_client", redis_client)
    ctx = scenario.Context(RedisK8sCharm)
    relation = scenario.PeerRelation(
        endpoint="redis-peers",
    )
    state = scenario.State(relations=[relation], leader=True)
    out = ctx.run(scenario.Event("update-status"), state=state)
    assert out.unit_status == ops.ActiveStatus()
    assert out.app_status == ops.ActiveStatus()
    # SCENARIO-NOTE: this isn't mentioned in the docs, only the history.
    assert out.workload_version == "6.0.11"


def test_on_update_status_failure_leader(monkeypatch):
    @contextlib.contextmanager
    def redis_client(hostname="localhost"):
        raise RedisError("Error connecting to redis")

    monkeypatch.setattr(RedisK8sCharm, "_redis_client", redis_client)
    ctx = scenario.Context(RedisK8sCharm)
    # SCENARIO-NOTE: the Harness test avoids needing to set up the peer relation
    # because the mocking is done at a level where some of the charm work is
    # skipped. However, a Harness test at the same level would probably still
    # need to create it.
    relation = scenario.PeerRelation(
        endpoint="redis-peers",
    )
    state = scenario.State(relations=[relation], leader=True)
    out = ctx.run(scenario.Event("update-status"), state=state)
    assert out.unit_status == ops.WaitingStatus("Waiting for Redis...")
    assert out.app_status == ops.WaitingStatus("Waiting for Redis...")
    # SCENARIO-NOTE: In the Harness tests, this is None. I haven't checked
    # which is really ought to be, although the empty string seems more correct.
    # This is perhaps Harness being able to distinguish between 'was never set'
    # and 'was set to the empty string', but that's not meaningful for Juju or
    # Scenario.
    assert out.workload_version == ""


def test_on_update_status_success_not_leader(monkeypatch):
    @contextlib.contextmanager
    def redis_client(hostname="localhost"):
        yield MockRedis(mock_info={"server": {"redis_version": "6.0.11"}})

    monkeypatch.setattr(RedisK8sCharm, "_redis_client", redis_client)
    ctx = scenario.Context(RedisK8sCharm)
    relation = scenario.PeerRelation(
        endpoint="redis-peers",
    )
    state = scenario.State(relations=[relation], leader=False)
    out = ctx.run(scenario.Event("update-status"), state=state)
    assert out.unit_status == ops.ActiveStatus()
    # SCENARIO-NOTE: with Harness, the test has to set the leader status to
    # true to validate that the app status is unknown. However, given that the
    # unit is not the leader and only the leader can set the app status, perhaps
    # this check is not really required?
    assert out.app_status == ops.UnknownStatus()
    assert out.workload_version == "6.0.11"


def test_on_update_status_failure_not_leader(monkeypatch):
    @contextlib.contextmanager
    def redis_client(hostname="localhost"):
        raise RedisError("Error connecting to redis")

    monkeypatch.setattr(RedisK8sCharm, "_redis_client", redis_client)
    ctx = scenario.Context(RedisK8sCharm)
    relation = scenario.PeerRelation(
        endpoint="redis-peers",
    )
    state = scenario.State(relations=[relation], leader=False)
    out = ctx.run(scenario.Event("update-status"), state=state)
    assert out.unit_status == ops.WaitingStatus("Waiting for Redis...")
    # SCENARIO-NOTE: with Harness, the test has to set the leader status to
    # true to validate that the app status is unknown. However, given that the
    # unit is not the leader and only the leader can set the app status, perhaps
    # this check is not really required?
    assert out.app_status == ops.UnknownStatus()
    assert out.workload_version == ""


def test_config_changed_when_unit_is_leader_status_success(monkeypatch):
    @contextlib.contextmanager
    def redis_client(hostname="localhost"):
        yield MockRedis(mock_info={"server": {"redis_version": "6.0.11"}})

    monkeypatch.setattr(RedisK8sCharm, "_redis_client", redis_client)
    # SCENARIO-NOTE: Trying to use `planned_units` gives a NotImplementedError.
    monkeypatch.setattr(ops.Application, "planned_units", lambda _: 1)
    ctx = scenario.Context(RedisK8sCharm)
    password = "test-password"
    # This seems like too much internal knowledge, but the property is odd
    # (see the note in charm.py).
    unit_pod_hostname = socket.getfqdn()
    relation = scenario.PeerRelation(
        endpoint="redis-peers",
        local_app_data={"redis-password": password, "leader-host": unit_pod_hostname},
    )
    redis_container = scenario.Container("redis", can_connect=True)
    sentinel_container = scenario.Container("sentinel", can_connect=True)
    state = scenario.State(
        relations=[relation],
        containers=[redis_container, sentinel_container],
        leader=True,
        config={"enable-tls": False},
    )
    out = ctx.run(scenario.Event("config-changed"), state=state)
    extra_flags = [
        f"--requirepass {password}",
        "--bind 0.0.0.0",
        f"--masterauth {password}",
        f"--replica-announce-ip {unit_pod_hostname}",
    ]
    expected_plan = {
        "services": {
            "redis": {
                "override": "replace",
                "summary": "Redis service",
                "command": f"redis-server {' '.join(extra_flags)}",
                "user": "redis",
                "group": "redis",
                "startup": "enabled",
            }
        },
    }
    found_plan = out.containers[0].plan.to_dict()
    assert found_plan == expected_plan
    # SCENARIO-NOTE: it seems like it would be more natural to have `containers.service_info`
    # that had `ops.pebble.ServiceInfo` objects, to align with the `get_service` that you'd use in
    # the charm itself.
    assert out.containers[0].service_status["redis"] == ops.pebble.ServiceStatus.ACTIVE
    assert out.unit_status == ops.ActiveStatus()
    assert out.app_status == ops.ActiveStatus()
    assert out.workload_version == "6.0.11"


def test_config_changed_when_unit_is_leader_status_failure(monkeypatch):
    @contextlib.contextmanager
    def redis_client(hostname="localhost"):
        raise RedisError("Error connecting to redis")

    monkeypatch.setattr(RedisK8sCharm, "_redis_client", redis_client)
    ctx = scenario.Context(RedisK8sCharm)
    password = "test-password"
    # This seems like too much internal knowledge, but the property is odd
    # (see the note in charm.py).
    unit_pod_hostname = socket.getfqdn()
    relation = scenario.PeerRelation(
        endpoint="redis-peers",
        local_app_data={"redis-password": password, "leader-host": unit_pod_hostname},
    )
    redis_container = scenario.Container("redis", can_connect=True)
    sentinel_container = scenario.Container("sentinel", can_connect=True)
    state = scenario.State(
        relations=[relation], containers=[redis_container, sentinel_container], leader=True
    )
    out = ctx.run(scenario.Event("config-changed"), state=state)
    extra_flags = [
        f"--requirepass {password}",
        "--bind 0.0.0.0",
        f"--masterauth {password}",
        f"--replica-announce-ip {unit_pod_hostname}",
    ]
    expected_plan = {
        "services": {
            "redis": {
                "override": "replace",
                "summary": "Redis service",
                "command": f"redis-server {' '.join(extra_flags)}",
                "user": "redis",
                "group": "redis",
                "startup": "enabled",
            }
        },
    }
    found_plan = out.containers[0].plan.to_dict()
    assert found_plan == expected_plan
    # SCENARIO-NOTE: it seems like it would be more natural to have `containers.service_info`
    # that had `ops.pebble.ServiceInfo` objects, to align with the `get_service` that you'd use in
    # the charm itself.
    assert out.containers[0].service_status["redis"] == ops.pebble.ServiceStatus.ACTIVE
    assert out.unit_status == ops.WaitingStatus("Waiting for Redis...")
    assert out.app_status == ops.WaitingStatus("Waiting for Redis...")
    assert out.workload_version == ""


def test_config_changed_pebble_error():
    ctx = scenario.Context(RedisK8sCharm)
    relation = scenario.PeerRelation(
        endpoint="redis-peers",
    )
    redis_container = scenario.Container("redis", can_connect=False)
    sentinel_container = scenario.Container("sentinel", can_connect=False)
    state = scenario.State(
        relations=[relation], containers=[redis_container, sentinel_container], leader=True
    )
    out = ctx.run(scenario.Event("config-changed"), state=state)
    # SCENARIO-NOTE: the Harness test checks that a restart hasn't been done,
    # but it seems like just making sure there is still no plan is sufficient.
    found_plan = out.containers[0].plan.to_dict()
    assert found_plan == {}
    assert out.unit_status == ops.WaitingStatus("Waiting for Pebble in sentinel container")
    assert out.app_status == ops.UnknownStatus()
    assert out.workload_version == ""
    # SCENARIO-NOTE: the Harness test has a note here that there should be a
    # check that the event is deferred. I assume with Harness this would
    # probably be done by mocking the event.defer() method and checking it was
    # called once. The Scenario support does seem nicer.
    assert len(out.deferred) == 1


def test_config_changed_when_unit_is_leader_and_service_is_running(monkeypatch):
    @contextlib.contextmanager
    def redis_client(hostname="localhost"):
        yield MockRedis(mock_info={"server": {"redis_version": "6.0.11"}})

    monkeypatch.setattr(RedisK8sCharm, "_redis_client", redis_client)

    ctx = scenario.Context(RedisK8sCharm)
    password = "test-password"
    # This seems like too much internal knowledge, but the property is odd
    # (see the note in charm.py).
    unit_pod_hostname = socket.getfqdn()
    relation = scenario.PeerRelation(
        endpoint="redis-peers",
        local_app_data={"redis-password": password, "leader-host": unit_pod_hostname},
    )
    # SCENARIO-NOTE: the readme doesn't seem to have anything on setting up
    # service state. Also, the check for a restart here is basically using the
    # same functionality as Harness (Scenario uses the ops.testing mock Pebble
    # client). It seems like both could have a better way of verifying that a
    # restart was done, for the situation where the status is active and then
    # there's a restart and it's active again.
    redis_container = scenario.Container(
        "redis", can_connect=True, service_status={"redis": ops.pebble.ServiceStatus.INACTIVE}
    )
    sentinel_container = scenario.Container("sentinel", can_connect=True)
    state = scenario.State(
        relations=[relation], containers=[redis_container, sentinel_container], leader=True
    )
    out = ctx.run(scenario.Event("config-changed"), state=state)
    assert out.unit_status == ops.ActiveStatus()
    assert out.app_status == ops.ActiveStatus()
    assert out.workload_version == "6.0.11"
    assert out.containers[0].service_status["redis"] == ops.pebble.ServiceStatus.ACTIVE


def test_password_on_leader_elected():
    ctx = scenario.Context(RedisK8sCharm)
    relation = scenario.PeerRelation(
        endpoint="redis-peers",
    )
    # SCENARIO-TEST: this feels a bit odd: I have to have the state be
    # leader==True, but the event is leader-elected. It feels like the event
    # should make the output state have leader=True, and going into the event
    # the state would be that leader==False. I guess from the Charm perspective
    # by the time the event handler is called, the Juju state already has the
    # leader value changed.
    state = scenario.State(relations=[relation], leader=True)
    out = ctx.run(scenario.Event("leader-elected"), state=state)
    admin_password = out.relations[0].local_app_data["redis-password"]
    assert admin_password

    # Trigger a new leader election and check that the password is still the same.
    out = ctx.run(scenario.Event("leader-elected"), state=out)
    assert out.relations[0].local_app_data["redis-password"] == admin_password


def test_on_relation_changed_status_when_unit_is_leader():
    # This seems like too much internal knowledge, but the property is odd
    # (see the note in charm.py).
    unit_pod_hostname = socket.getfqdn()
    leader_ip = socket.gethostbyname(unit_pod_hostname)

    # Given
    ctx = scenario.Context(RedisK8sCharm)
    relation = scenario.PeerRelation(
        endpoint="redis-peers", local_app_data={"leader-host": unit_pod_hostname}
    )
    wordpress_relation = scenario.Relation(
        endpoint="redis",
        remote_app_name="wordpress",
    )
    state = scenario.State(relations=[relation, wordpress_relation], leader=True)

    # When
    out = ctx.run(wordpress_relation.changed_event(), state=state)

    # Then
    databag = out.relations[1].local_unit_data
    assert databag["hostname"] == leader_ip
    assert databag["port"] == "6379"


def test_pebble_layer_on_relation_created():
    ctx = scenario.Context(RedisK8sCharm)
    # This seems like too much internal knowledge, but the property is odd
    # (see the note in charm.py).
    unit_pod_hostname = socket.getfqdn()
    relation = scenario.PeerRelation(
        endpoint="redis-peers", local_app_data={"leader-host": unit_pod_hostname}
    )
    wordpress_relation = scenario.Relation(
        endpoint="redis",
        remote_app_name="wordpress",
    )
    redis_container = scenario.Container("redis", can_connect=True)
    state = scenario.State(
        relations=[relation, wordpress_relation], containers=[redis_container], leader=True
    )
    out = ctx.run(wordpress_relation.created_event(), state=state)

    # Check that the resulting plan does not have a password
    extra_flags = [
        "--bind 0.0.0.0",
        f"--replica-announce-ip {unit_pod_hostname}",
        "--protected-mode no",
    ]
    expected_plan = {
        "services": {
            "redis": {
                "override": "replace",
                "summary": "Redis service",
                "command": f"redis-server {' '.join(extra_flags)}",
                "user": "redis",
                "group": "redis",
                "startup": "enabled",
            }
        },
    }
    found_plan = out.containers[0].plan.to_dict()
    assert found_plan == expected_plan


@pytest.fixture()
def resources():
    with tempfile.TemporaryDirectory() as resource_folder:
        resource_folder = pathlib.Path(resource_folder)
        cert_file_name = resource_folder / "cert-file"
        with open(cert_file_name, "w") as cert_file:
            cert_file.write("cert")
        key_file_name = resource_folder / "key-file"
        with open(key_file_name, "w") as key_file:
            key_file.write("key")
        ca_cert_file_name = resource_folder / "ca-cert-file"
        with open(ca_cert_file_name, "w") as ca_cert_file:
            ca_cert_file.write("ca-cert")
        yield {
            "cert-file": cert_file_name,
            "key-file": key_file_name,
            "ca-cert-file": ca_cert_file_name,
        }


def test_attach_resource(resources):
    ctx = scenario.Context(RedisK8sCharm)
    relation = scenario.PeerRelation(
        endpoint="redis-peers",
    )
    redis_container = scenario.Container("redis", can_connect=True)
    state = scenario.State(
        leader=True, relations=[relation], containers=[redis_container], resources=resources
    )
    out = ctx.run(scenario.Event("upgrade-charm"), state=state)
    container_root_fs = out.containers[0].get_filesystem(ctx)
    cert_file = container_root_fs / "var/lib/redis" / "cert-file"
    assert cert_file.read_text() == "cert"
    # Permission 0600, user and group == "redis"
    assert stat.S_IMODE(cert_file.stat().st_mode) == stat.S_IRUSR | stat.S_IWUSR
    # SCENARIO-NOTE: Scenario doesn't set the user and group (and probably can't
    # since this user and group might not exist locally, only on the container),
    # but also provides no way to check that this was done.
    #    assert cert_file.stat().st_uid == pwd.getpwnam("redis").pw_uid
    #    assert cert_file.stat().st_gid == grp.getgrnam("redis").gr_gid
    key_file = container_root_fs / "var/lib/redis" / "key-file"
    assert key_file.read_text() == "key"
    assert stat.S_IMODE(key_file.stat().st_mode) == stat.S_IRUSR | stat.S_IWUSR
    ca_cert_file = container_root_fs / "var/lib/redis" / "ca-cert-file"
    assert ca_cert_file.read_text() == "ca-cert"
    assert stat.S_IMODE(ca_cert_file.stat().st_mode) == stat.S_IRUSR | stat.S_IWUSR


def test_blocked_on_enable_tls_with_no_certificates(monkeypatch):
    @contextlib.contextmanager
    def redis_client(hostname="localhost"):
        yield MockRedis(mock_info={"server": {"redis_version": "6.0.11"}})

    monkeypatch.setattr(RedisK8sCharm, "_redis_client", redis_client)
    ctx = scenario.Context(RedisK8sCharm)
    relation = scenario.PeerRelation(
        endpoint="redis-peers", local_app_data={"leader-host": "host", "redis-password": "pass"}
    )
    redis_container = scenario.Container("redis", can_connect=True)
    sentinel_container = scenario.Container("sentinel", can_connect=True)
    # SCENARIO-NOTE: Scenario doesn't seem to have a way to have a state where
    # there are missing resources. If there is a `fetch()` call to a resource
    # then it causes a `RuntimeError` of an inconsistent state, rather than
    # the `NameError` that you get if this happens with Juju/ops.
    monkeypatch.setattr(RedisK8sCharm, "_retrieve_resource", lambda *_: None)
    state = scenario.State(
        relations=[relation],
        containers=[redis_container, sentinel_container],
        config={"enable-tls": True},
    )
    out = ctx.run(scenario.Event("config-changed"), state=state)
    assert out.unit_status == ops.BlockedStatus("Not enough certificates found")


def test_active_on_enable_tls_with_certificates(monkeypatch, resources):
    @contextlib.contextmanager
    def redis_client(hostname="localhost"):
        yield MockRedis(mock_info={"server": {"redis_version": "6.0.11"}})

    monkeypatch.setattr(RedisK8sCharm, "_redis_client", redis_client)

    ctx = scenario.Context(RedisK8sCharm)
    password = "test-password"
    # This seems like too much internal knowledge, but the property is odd
    # (see the note in charm.py).
    unit_pod_hostname = socket.getfqdn()
    relation = scenario.PeerRelation(
        endpoint="redis-peers",
        local_app_data={"redis-password": password, "leader-host": unit_pod_hostname},
    )
    redis_container = scenario.Container("redis", can_connect=True)
    sentinel_container = scenario.Container("sentinel", can_connect=True)
    state = scenario.State(
        leader=True,
        relations=[relation],
        containers=[redis_container, sentinel_container],
        config={"enable-tls": True},
        resources=resources,
    )
    out = ctx.run(scenario.Event("upgrade-charm"), state=state)
    out = ctx.run(scenario.Event("config-changed"), state=out)

    assert out.unit_status == ops.ActiveStatus()
    assert out.app_status == ops.ActiveStatus()
    assert out.workload_version == "6.0.11"
    assert out.containers[0].service_status["redis"] == ops.pebble.ServiceStatus.ACTIVE
    container_root_fs = out.containers[0].get_filesystem(ctx)
    cert_file = container_root_fs / "var/lib/redis" / "cert-file"
    assert cert_file.read_text() == "cert"
    assert stat.S_IMODE(cert_file.stat().st_mode) == stat.S_IRUSR | stat.S_IWUSR
    key_file = container_root_fs / "var/lib/redis" / "key-file"
    assert key_file.read_text() == "key"
    assert stat.S_IMODE(key_file.stat().st_mode) == stat.S_IRUSR | stat.S_IWUSR
    ca_cert_file = container_root_fs / "var/lib/redis" / "ca-cert-file"
    assert ca_cert_file.read_text() == "ca-cert"
    assert stat.S_IMODE(ca_cert_file.stat().st_mode) == stat.S_IRUSR | stat.S_IWUSR
    extra_flags = [
        f"--requirepass {password}",
        "--bind 0.0.0.0",
        f"--masterauth {password}",
        f"--replica-announce-ip {unit_pod_hostname}",
        "--tls-port 6379",
        "--port 0",
        "--tls-auth-clients optional",
        "--tls-cert-file /var/lib/redis/redis.crt",
        "--tls-key-file /var/lib/redis/redis.key",
        "--tls-ca-cert-file /var/lib/redis/ca.crt",
    ]
    expected_plan = {
        "services": {
            "redis": {
                "override": "replace",
                "summary": "Redis service",
                "command": f"redis-server {' '.join(extra_flags)}",
                "user": "redis",
                "group": "redis",
                "startup": "enabled",
            }
        },
    }
    found_plan = out.containers[0].plan.to_dict()
    assert found_plan == expected_plan


def test_non_leader_unit_as_replica(monkeypatch, resources):
    @contextlib.contextmanager
    def redis_client(hostname="localhost"):
        yield MockRedis(mock_info={"server": {"redis_version": "6.0.11"}})

    monkeypatch.setattr(RedisK8sCharm, "_redis_client", redis_client)

    # SCENARIO-NOTE: the 'redis-k8s' name here comes from charm.app.name - I'm
    # not sure if there's a way to get that from Scenario - it doesn't seem to
    # be in the state.
    # Custom responses to Redis `execute_command` call
    def my_side_effect(_, value: str):
        mapping = {
            f"SENTINEL CKQUORUM redis-k8s": "OK",
            f"SENTINEL MASTER redis-k8s": [
                "ip",
                APPLICATION_DATA["leader-host"],
                "flags",
                "master",
            ],
        }
        return mapping.get(value)

    # This doesn't seem to have any impact on the output state that was being
    # tested in the Harness test, so I'm not sure what it's meant to be doing.
    monkeypatch.setattr(Redis, "execute_command", my_side_effect)

    ctx = scenario.Context(RedisK8sCharm)
    password = "test-password"
    # This seems like too much internal knowledge, but the property is odd
    # (see the note in charm.py).
    unit_pod_hostname = socket.getfqdn()
    relation = scenario.PeerRelation(
        endpoint="redis-peers",
        local_app_data={"redis-password": password, "leader-host": unit_pod_hostname},
        peers_data={1: {}},
    )
    redis_container = scenario.Container("redis", can_connect=True)
    sentinel_container = scenario.Container("sentinel", can_connect=True)
    state = scenario.State(
        leader=True,
        relations=[relation],
        containers=[redis_container, sentinel_container],
        resources=resources,
    )
    # Trigger peer_relation_joined/changed
    out = ctx.run(relation.joined_event(), state=state)
    # Simulate an update to the application databag made by the leader unit
    relation = relation.replace(local_app_data=APPLICATION_DATA)
    out = out.replace(relations=[relation])
    out = ctx.run(relation.changed_event(), state=out)
    # A pebble ready event will set the non-leader unit with the correct information
    out = out.replace(leader=False)
    out = ctx.run(redis_container.pebble_ready_event(), state=out)

    assert out.unit_status == ops.ActiveStatus()
    extra_flags = [
        f"--requirepass {APPLICATION_DATA['redis-password']}",
        "--bind 0.0.0.0",
        f"--masterauth {APPLICATION_DATA['redis-password']}",
        f"--replica-announce-ip {unit_pod_hostname}",
        f"--replicaof {APPLICATION_DATA['leader-host']} {REDIS_PORT}",
    ]
    expected_plan = {
        "services": {
            "redis": {
                "override": "replace",
                "summary": "Redis service",
                "command": f"redis-server {' '.join(extra_flags)}",
                "user": "redis",
                "group": "redis",
                "startup": "enabled",
            }
        },
    }
    found_plan = out.containers[0].plan.to_dict()
    assert found_plan == expected_plan


def test_application_data_update_after_failover(monkeypatch, resources):
    # Custom responses to Redis `execute_command` call
    def my_side_effect(_, value: str):
        mapping = {
            f"SENTINEL CKQUORUM redis-k8s": "OK",
            f"SENTINEL MASTER redis-k8s": [
                "ip",
                "different-leader",
                "flags",
                "s_down",
            ],
        }
        return mapping.get(value)

    monkeypatch.setattr(Redis, "execute_command", my_side_effect)

    ctx = scenario.Context(RedisK8sCharm)
    relation = scenario.PeerRelation(
        endpoint="redis-peers",
        peers_data={1: {}},
    )
    redis_container = scenario.Container("redis", can_connect=True)
    sentinel_container = scenario.Container("sentinel", can_connect=True)
    state = scenario.State(
        leader=True,
        relations=[relation],
        resources=resources,
        containers=[redis_container, sentinel_container],
    )
    # Trigger peer_relation_joined/changed
    out = ctx.run(relation.joined_event(), state=state)
    # Simulate an update to the application databag made by the leader unit
    relation = relation.replace(local_app_data=APPLICATION_DATA)
    out = out.replace(relations=[relation])
    out = ctx.run(relation.changed_event(), state=out)

    # NOTE: On config changed, charm will be updated with APPLICATION_DATA. But a
    # call to `execute_command(SENTINEL MASTER redis-k8s)` will return `different_leader`
    # when checking the master, simulating that sentinel triggered failover in between
    # charm events.
    assert out.relations[0].local_app_data["leader-host"] == "different-leader"

    # Reset the application data to the initial state
    relation = relation.replace(local_app_data=APPLICATION_DATA)
    out = out.replace(relations=[relation])

    # Now check that a pod reschedule will also result in updated information
    out = ctx.run(scenario.Event("upgrade-charm"), state=out)

    assert out.relations[0].local_app_data["leader-host"] == "different-leader"


def test_forced_failover_when_unit_departed_is_master(monkeypatch, resources):
    ctx = scenario.Context(RedisK8sCharm)
    relation = scenario.PeerRelation(
        endpoint="redis-peers",
        local_app_data=APPLICATION_DATA,
    )
    redis_container = scenario.Container("redis", can_connect=True)
    sentinel_container = scenario.Container("sentinel", can_connect=True)
    state = scenario.State(
        leader=True,
        relations=[relation],
        containers=[redis_container, sentinel_container],
        model=scenario.Model("testing-redis-k8s"),
    )

    # Custom responses to Redis `execute_command` call
    called_reset = False

    def my_side_effect(_, value: str):
        nonlocal called_reset
        if value == "SENTINEL RESET redis-k8s":
            called_reset = True
            return
        k8s_hostname = f"redis-k8s-1.redis-k8s-endpoints.{state.model.name}.svc.cluster.local"
        mapping = {
            f"SENTINEL CKQUORUM redis-k8s": "OK",
            f"SENTINEL MASTER redis-k8s": [
                "ip",
                k8s_hostname,
                "flags",
                "master",
            ],
        }
        return mapping.get(value)

    monkeypatch.setattr(Redis, "execute_command", my_side_effect)

    # Simulate an update to the application databag made by the leader unit
    out = ctx.run(relation.changed_event(), state=state)
    # Add and remove a unit that sentinel will simulate as current master
    relation = relation.replace(peers_data={1: {}})
    out = out.replace(relations=[relation])
    out = ctx.run(relation.joined_event(), state=out)
    relation = relation.replace(peers_data={})
    out = out.replace(relations=[relation])
    # SCENARIO-NOTE: it's actually a peer unit that's departing here, but this
    # seemed like the only way to get event.unit properly set up.
    ctx.run(relation.departed_event(remote_unit_id=1), state=out)
    assert called_reset
