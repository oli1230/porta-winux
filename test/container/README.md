# test/container/ — containerized smoke test

`Containerfile` builds a clean Fedora 43 userspace with restic + python3,
copies the repo in, and runs `test/smoke.sh`. This catches missing
dependencies and Fedora-specific behavior in about a minute, without a VM.

Run: `make container-test` (needs podman: `sudo dnf install podman`).

Background:
- podman (daemonless Docker-compatible containers): https://podman.io/docs
- Containerfile syntax is Dockerfile syntax:
  https://docs.docker.com/reference/dockerfile/
Containers share the host kernel, so this tier can't test SELinux
relabeling, FUSE mounts, or drive auto-detection — that's what the VM tier
is for.
