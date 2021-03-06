#!/usr/bin/env python
import sys
import yaml

from charmhelpers.core.hookenv import (
    Hooks,
    UnregisteredHookError,
    config,
    log,
    relation_get,
    relation_ids,
    related_units,
    status_set,
    relation_set,
    local_unit,
    open_port,
    close_port,
)

from charmhelpers.fetch import (
    apt_install,
    apt_upgrade,
    apt_update
)

from contrail_analytics_utils import (
    update_charm_status,
    CONTAINER_NAME,
)
from common_utils import (
    get_ip,
    fix_hostname,
)
from docker_utils import (
    add_docker_repo,
    apply_docker_insecure,
    docker_login,
    DOCKER_PACKAGES,
    is_container_launched,
)


PACKAGES = []

hooks = Hooks()
config = config()


@hooks.hook("install.real")
def install():
    status_set("maintenance", "Installing...")

    # TODO: try to remove this call
    fix_hostname()

    apt_upgrade(fatal=True, dist=True)
    add_docker_repo()
    apt_update(fatal=False)
    apt_install(PACKAGES + DOCKER_PACKAGES, fatal=True)

    apply_docker_insecure()
    docker_login()

    update_charm_status()
    # NOTE: do not open port until haproxy can fail
    # https://bugs.launchpad.net/charm-haproxy/+bug/1792939
    #open_port(8081, "TCP")


@hooks.hook("config-changed")
def config_changed():
    if config.changed("control-network"):
        settings = {'private-address': get_ip()}
        rnames = ("contrail-analytics", "contrail-analyticsdb",
                  "analytics-cluster", "http-services")
        for rname in rnames:
            for rid in relation_ids(rname):
                relation_set(relation_id=rid, relation_settings=settings)

    if config.changed("docker-registry"):
        apply_docker_insecure()
    if config.changed("docker-user") or config.changed("docker-password"):
        docker_login()

    update_charm_status()


def _value_changed(rel_data, rel_key, cfg_key):
    if rel_key not in rel_data:
        # data is absent in relation. it means that remote charm doesn't
        # send it due to lack of information
        return False
    value = rel_data[rel_key]
    if value is not None and value != config.get(cfg_key):
        config[cfg_key] = value
        return True
    elif value is None and config.get(cfg_key) is not None:
        config.pop(cfg_key, None)
        return True
    return False


@hooks.hook("contrail-analytics-relation-joined")
def contrail_analytics_joined():
    settings = {"private-address": get_ip()}
    relation_set(relation_settings=settings)


@hooks.hook("contrail-analytics-relation-changed")
def contrail_analytics_changed():
    data = relation_get()
    changed = False
    changed |= _value_changed(data, "api-vip", "api_vip")
    changed |= _value_changed(data, "auth-mode", "auth_mode")
    changed |= _value_changed(data, "auth-info", "auth_info")
    changed |= _value_changed(data, "orchestrator-info", "orchestrator_info")
    changed |= _value_changed(data, "ssl-enabled", "ssl_enabled")
    changed |= _value_changed(data, "rabbitmq_user", "rabbitmq_user")
    changed |= _value_changed(data, "rabbitmq_password", "rabbitmq_password")
    changed |= _value_changed(data, "rabbitmq_vhost", "rabbitmq_vhost")
    changed |= _value_changed(data, "rabbitmq_hosts", "rabbitmq_hosts")
    changed |= _value_changed(data, "configdb_cassandra_user", "configdb_cassandra_user")
    changed |= _value_changed(data, "configdb_cassandra_password", "configdb_cassandra_password")
    config.save()
    # TODO: handle changing of all values
    # TODO: set error if orchestrator is changing and container was started
    if changed:
        update_charm_status()
        _notify_proxy_services()


@hooks.hook("contrail-analytics-relation-departed")
def contrail_analytics_departed():
    units = [unit for rid in relation_ids("contrail-controller")
                  for unit in related_units(rid)]
    if not units:
        for key in ["api_vip", "auth_info", "auth_mode", "orchestrator_info",
                    "ssl_enabled", "rabbitmq_vhost",
                    "rabbitmq_user", "rabbitmq_password", "rabbitmq_hosts"]:
            config.pop(key, None)
        if is_container_launched(CONTAINER_NAME):
            status_set(
                "blocked",
                "Container is present but cloud orchestrator was disappeared."
                " Please kill container by yourself or "
                "restore cloud orchestrator.")
    config.save()
    update_charm_status()
    _notify_proxy_services()


@hooks.hook("contrail-analyticsdb-relation-joined")
def contrail_analyticsdb_joined():
    settings = {"private-address": get_ip(), 'unit-type': 'analytics'}
    relation_set(relation_settings=settings)


@hooks.hook("contrail-analyticsdb-relation-changed")
def contrail_analyticsdb_changed():
    data = relation_get()
    _value_changed(data, "db-user", "db_user")
    _value_changed(data, "db-password", "db_password")
    update_charm_status()


@hooks.hook("contrail-analyticsdb-relation-departed")
def contrail_analyticsdb_departed():
    units = [unit for rid in relation_ids("contrail-analyticsdb")
                  for unit in related_units(rid)]
    if not units:
        config.pop("db_user", None)
        config.pop("db_password", None)
    update_charm_status()


@hooks.hook("analytics-cluster-relation-joined")
def analytics_cluster_joined():
    settings = {"private-address": get_ip()}
    relation_set(relation_settings=settings)

    update_charm_status()


@hooks.hook("update-status")
def update_status():
    update_charm_status(update_config=False)


@hooks.hook("upgrade-charm")
def upgrade_charm():
    # NOTE: image can not be deleted if container is running.
    # TODO: so think about killing the container

    # clear cached version of image
    config.pop("version_with_build", None)
    config.pop("version", None)
    config.save()

    # NOTE: this hook can be fired when either resource changed or charm code
    # changed. so if code was changed then we may need to update config
    update_charm_status()


def _notify_proxy_services():
    for rid in relation_ids("http-services"):
        if related_units(rid):
            http_services_joined(rid)


def _http_services(vip):
    name = local_unit().replace("/", "-")
    addr = get_ip()
    return [{"service_name": "contrail-analytics-api",
             "service_host": str(vip),
             "service_port": 8081,
             "service_options": ["option nolinger", "balance roundrobin"],
             "servers": [[name, addr, 8081, "check inter 2000 rise 2 fall 3"]]
            }]


@hooks.hook("http-services-relation-joined")
def http_services_joined(rel_id=None):
    vip = config.get("api_vip")
    func = close_port if vip else open_port
    try:
        func(8081, "TCP")
    except Exception:
        pass
    data = list() if not vip else _http_services(str(vip))
    relation_set(relation_id=rel_id,
                 services=yaml.dump(data))


def main():
    try:
        hooks.execute(sys.argv)
    except UnregisteredHookError as e:
        log("Unknown hook {} - skipping.".format(e))


if __name__ == "__main__":
    main()
