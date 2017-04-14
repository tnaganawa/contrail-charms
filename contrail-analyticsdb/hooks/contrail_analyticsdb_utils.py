import functools
import os
import pwd
import shutil
import socket
from socket import gaierror, gethostbyname, gethostname, inet_aton
import struct
from subprocess import (
    CalledProcessError,
    check_call,
    check_output
)
from time import sleep, time

import apt_pkg
import yaml
import platform

try:
  import netaddr
  import netifaces
except ImportError:
  pass

from charmhelpers.core.hookenv import (
    config,
    log,
    related_units,
    relation_get,
    relation_ids,
    relation_type,
    remote_unit,
    unit_get
)

from charmhelpers.core.host import service_restart, service_start

from charmhelpers.core.templating import render

apt_pkg.init()
config = config()


def dpkg_version(pkg):
    try:
        return check_output(["docker",
                              "exec",
                              "contrail-analyticsdb",
                              "dpkg-query",
                              "-f", "${Version}\\n", "-W", pkg]).rstrip()
    except CalledProcessError:
        return None


def fix_hostname():
    hostname = gethostname()
    try:
        gethostbyname(hostname)
    except gaierror:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 53))  # connecting to a UDP address doesn't send packets
        local_ip_address = s.getsockname()[0]
        check_call(["sed", "-E", "-i", "-e",
                    "/127.0.0.1[[:blank:]]+/a \\\n"+ local_ip_address+" " + hostname,
                    "/etc/hosts"])


def controller_ctx():
    """Get the ipaddres of all contrail control nodes"""
    controller_ip_list = [ gethostbyname(relation_get("private-address", unit, rid))
                           for rid in relation_ids("contrail-control")
                           for unit in related_units(rid) ]
    controller_ip_list = sorted(controller_ip_list, key=lambda ip: struct.unpack("!L", inet_aton(ip))[0])
    return { "controller_servers": controller_ip_list }


def analytics_ctx():
    """Get the ipaddres of all analytics nodes"""
    analytics_ip_list = [ gethostbyname(relation_get("private-address", unit, rid))
                            for rid in relation_ids("contrail-analytics")
                            for unit in related_units(rid) ]
    analytics_ip_list = sorted(analytics_ip_list, key=lambda ip: struct.unpack("!L", inet_aton(ip))[0])
    return { "analytics_servers": analytics_ip_list }


def analyticsdb_ctx():
    """Get the ipaddres of all analyticsdb nodes"""
    analyticsdb_ip_list = [ gethostbyname(relation_get("private-address", unit, rid))
                            for rid in relation_ids("analyticsdb-cluster")
                            for unit in related_units(rid) ]
    # add it's own ip address
    analyticsdb_ip_list.append(gethostbyname(unit_get("private-address")))
    analyticsdb_ip_list = sorted(analyticsdb_ip_list, key=lambda ip: struct.unpack("!L", inet_aton(ip))[0])
    return { "analyticsdb_servers": analyticsdb_ip_list }


def lb_ctx():
    lb_vip = None
    for rid in relation_ids("contrail-lb"):
        for unit in related_units(rid):
            lb_vip = gethostbyname(relation_get("private-address", unit, rid))
    return { "lb_vip": lb_vip}


def identity_admin_ctx():
    ctxs = [ { "keystone_ip": gethostbyname(hostname),
               "keystone_public_port": relation_get("service_port", unit, rid),
               "keystone_admin_user": relation_get("service_username", unit, rid),
               "keystone_admin_password": relation_get("service_password", unit, rid),
               "keystone_admin_tenant": relation_get("service_tenant_name", unit, rid),
               "keystone_auth_protocol": relation_get("service_protocol", unit, rid) }
             for rid in relation_ids("identity-admin")
             for unit, hostname in
             ((unit, relation_get("service_hostname", unit, rid)) for unit in related_units(rid))
             if hostname ]
    return ctxs[0] if ctxs else {}


def is_already_launched():
    cmd = 'docker ps | grep contrail-analyticsdb'
    try:
        output =  check_output(cmd, shell=True)
        return True
    except CalledProcessError:
        return False


def apply_config():
   cmd = '/usr/bin/docker exec contrail-analyticsdb contrailctl config sync -c analyticsdb'
   check_call(cmd, shell=True)


def launch_docker_image():
    image_id = None
    orchestrator = config.get("cloud_orchestrator")
    output =  check_output(["docker",
                            "images",
                            ])
    output = output.decode().split('\n')[:-1]
    for line in output:
        if "contrail-analyticsdb" in line.split()[0]:
            image_id = line.split()[2].strip()
    if not image_id:
        log("contrail-analyticsdb docker image is not available")
        return
    dist = platform.linux_distribution()[2].strip()
    cmd = "/usr/bin/docker "+ \
          "run "+ \
          "--net=host "+ \
          "--cap-add=AUDIT_WRITE "+ \
          "--privileged "+ \
          "--env='CLOUD_ORCHESTRATOR=%s' "%(orchestrator)+ \
          "--volume=/etc/contrailctl:/etc/contrailctl "+ \
          "--name=contrail-analyticsdb "
    if dist == "trusty":
        cmd = cmd + "--pid=host "
    cmd = cmd +"-itd "+ image_id
    check_call(cmd, shell=True)


def write_analyticsdb_config():
    ctx = {}
    ctx.update({"cloud_orchestrator": config.get("cloud_orchestrator")})
    ctx.update(controller_ctx())
    ctx.update(analytics_ctx())
    ctx.update(analyticsdb_ctx())
    ctx.update(lb_ctx())
    ctx.update(identity_admin_ctx())
    render("analyticsdb.conf", "/etc/contrailctl/analyticsdb.conf", ctx)
    if config.get("control-ready") and config.get("lb-ready") \
      and config.get("identity-admin-ready") and config.get("analytics-ready"):
        if is_already_launched():
            apply_config()
        else:
            launch_docker_image()
