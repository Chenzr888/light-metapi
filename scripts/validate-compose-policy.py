#!/usr/bin/env python3
"""Reject Compose changes that expand the production blast radius."""

import json
import pathlib
import sys


def fail(message):
    raise SystemExit(f"FATAL: Compose policy violation: {message}")


if len(sys.argv) != 4:
    raise SystemExit("usage: validate-compose-policy.py <resolved.json> <image> <data-dir>")

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
expected_image = sys.argv[2]
expected_data = str(pathlib.Path(sys.argv[3]))
services = payload.get("services") or {}
if set(services) != {"upstream-balance"}:
    fail(f"service set must be exactly upstream-balance, got {sorted(services)}")

service = services["upstream-balance"]
checks = {
    "image": service.get("image") == expected_image,
    "container_name": service.get("container_name") == "upstream-balance",
    "user": service.get("user") == "1000:1000",
    "read_only": service.get("read_only") is True,
    "init": service.get("init") is True,
    "restart": service.get("restart") == "unless-stopped",
    "privileged": not service.get("privileged", False),
    "network_mode": service.get("network_mode") in (None, ""),
    "cap_drop": service.get("cap_drop") == ["ALL"],
    "cap_add": not service.get("cap_add"),
    "security_opt": service.get("security_opt") == ["no-new-privileges:true"],
    "pid_namespace": service.get("pid") in (None, ""),
    "ipc_namespace": service.get("ipc") in (None, ""),
    "uts_namespace": service.get("uts") in (None, ""),
    "user_namespace": service.get("userns_mode") in (None, ""),
    "cgroup_namespace": service.get("cgroup") in (None, ""),
    "devices": not service.get("devices"),
    "device_rules": not service.get("device_cgroup_rules"),
    "volumes_from": not service.get("volumes_from"),
    "runtime": service.get("runtime") in (None, "runc"),
    "gpus": not service.get("gpus"),
    "cpu_limit": service.get("cpus") == 1,
    "memory_limit": str(service.get("mem_limit")) == "536870912",
    "memory_reservation": str(service.get("mem_reservation")) == "201326592",
    "memory_swap_limit": str(service.get("memswap_limit")) == "805306368",
    "pid_limit": service.get("pids_limit") == 128,
}
for name, valid in checks.items():
    if not valid:
        fail(name)

ports = service.get("ports") or []
if len(ports) != 1 or not (
    ports[0].get("host_ip") == "127.0.0.1"
    and ports[0].get("target") == 8756
    and str(ports[0].get("published")) == "8756"
):
    fail("the only published port must be 127.0.0.1:8756:8756")

volumes = service.get("volumes") or []
if len(volumes) != 1 or not (
    volumes[0].get("type") == "bind"
    and volumes[0].get("source") == expected_data
    and volumes[0].get("target") == "/app/data"
):
    fail("the only bind mount must be the declared data directory at /app/data")
if any("docker.sock" in json.dumps(item) for item in volumes):
    fail("Docker socket mounts are forbidden")

environment = service.get("environment") or {}
if environment.get("SESSION_COOKIE_SECURE") != "1":
    fail("SESSION_COOKIE_SECURE must be 1")
if environment.get("SESSION_COOKIE_PATH") != "/upstream-balance":
    fail("SESSION_COOKIE_PATH must be /upstream-balance")

print("COMPOSE_POLICY_OK service=upstream-balance local_port=127.0.0.1:8756")
