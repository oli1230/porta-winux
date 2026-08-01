PY ?= python3

.PHONY: help doctor install dev lint unit smoke test container-test \
        vm-fetch vm-drive vm-up vm-ssh vm-push vm-test vm-wipe vm-destroy

help:
	@echo "porta-winux targets:"
	@echo "  doctor          check that required/optional dependencies are present"
	@echo "  install         pip install (user site) -> 'porta-winux' on PATH"
	@echo "  dev             editable install with tui+dev extras"
	@echo "  lint            ruff check (needs 'make dev' or ruff installed)"
	@echo "  unit            pytest unit tests (pure logic, no restic needed)"
	@echo "  smoke           fast end-to-end test in a sandbox (needs restic)"
	@echo "  test            unit + smoke"
	@echo "  container-test  smoke test inside a clean Fedora 43 podman container"
	@echo "  vm-up           create Fedora 43 test VM with fake external drive"
	@echo "  vm-test         push source into VM and run e2e tests"
	@echo "  vm-ssh          shell into the VM"
	@echo "  vm-wipe         delete VM + OS disk (base image kept) for a clean retest"
	@echo "  vm-destroy      remove every VM artifact including base image"

doctor:
	@echo "== required =="
	@$(PY) -c 'import sys; v=sys.version_info; ok=v>=(3,11); \
	print(f"  python3 {v.major}.{v.minor}.{v.micro}", "OK" if ok else "TOO OLD (need >=3.11 for tomllib)"); \
	sys.exit(0 if ok else 1)'
	@command -v restic >/dev/null \
	  && echo "  restic $$(restic version | awk '{print $$2}') OK" \
	  || echo "  restic MISSING (dnf install restic) — needed for everything except 'make unit'"
	@echo "== optional =="
	@$(PY) -c 'import textual' 2>/dev/null && echo "  textual OK (browse TUI)" \
	  || echo "  textual missing — 'porta-winux browse' unavailable (make dev, or pip install '.[tui]')"
	@command -v ruff   >/dev/null && echo "  ruff OK"   || echo "  ruff missing (make lint unavailable)"
	@command -v pytest >/dev/null && echo "  pytest OK" || echo "  pytest missing (make unit unavailable)"
	@command -v podman >/dev/null && echo "  podman OK" || echo "  podman missing (make container-test unavailable)"
	@command -v virt-install >/dev/null && command -v virt-make-fs >/dev/null && command -v cloud-localds >/dev/null \
	  && echo "  libvirt tooling OK" \
	  || echo "  libvirt tooling incomplete — for vm-*: sudo dnf install libvirt virt-install guestfs-tools qemu-img cloud-utils"

install:
	$(PY) -m pip install --user .

dev:
	$(PY) -m pip install --user -e '.[tui,dev]'

lint:
	@command -v ruff >/dev/null || { echo "ruff not installed; run 'make dev' first"; exit 1; }
	ruff check porta_winux test/unit

unit:
	@command -v pytest >/dev/null || { echo "pytest not installed; run 'make dev' first"; exit 1; }
	$(PY) -m pytest test/unit/ -q

smoke:
	@command -v restic >/dev/null || { echo "restic not installed (dnf install restic)"; exit 1; }
	bash test/smoke.sh

test: unit smoke

container-test:
	podman build -t porta-winux-test -f test/container/Containerfile .
	podman run --rm porta-winux-test

vm-fetch: ;	bash test/vm/vm.sh fetch
vm-drive: ;	bash test/vm/vm.sh drive
vm-up:    ;	bash test/vm/vm.sh up
vm-ssh:   ;	bash test/vm/vm.sh ssh
vm-push:  ;	bash test/vm/vm.sh push
vm-test:  ;	bash test/vm/vm.sh test
vm-wipe:  ;	bash test/vm/vm.sh wipe
vm-destroy: ;	bash test/vm/vm.sh destroy
