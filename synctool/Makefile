PY ?= python3

.PHONY: help install dev lint smoke container-test \
        vm-fetch vm-drive vm-up vm-ssh vm-push vm-test vm-wipe vm-destroy

help:
	@echo "synctool targets:"
	@echo "  install         pip install (user site)"
	@echo "  dev             editable install with tui+dev extras"
	@echo "  lint            ruff check"
	@echo "  smoke           fast end-to-end test in a sandbox (needs restic)"
	@echo "  container-test  same, inside a Fedora 43 podman container"
	@echo "  vm-up           create Fedora 43 test VM with fake external drive"
	@echo "  vm-test         push source into VM and run e2e tests"
	@echo "  vm-ssh          shell into the VM"
	@echo "  vm-wipe         delete VM + OS disk (base image kept) for a clean retest"
	@echo "  vm-destroy      remove every VM artifact including base image"

install:
	$(PY) -m pip install --user .

dev:
	$(PY) -m pip install --user -e '.[tui,dev]'

lint:
	$(PY) -m ruff check src

smoke:
	bash test/smoke.sh

container-test:
	podman build -t synctool-test -f test/container/Containerfile .
	podman run --rm synctool-test

vm-fetch: ;	bash test/vm/vm.sh fetch
vm-drive: ;	bash test/vm/vm.sh drive
vm-up:    ;	bash test/vm/vm.sh up
vm-ssh:   ;	bash test/vm/vm.sh ssh
vm-push:  ;	bash test/vm/vm.sh push
vm-test:  ;	bash test/vm/vm.sh test
vm-wipe:  ;	bash test/vm/vm.sh wipe
vm-destroy: ;	bash test/vm/vm.sh destroy
