PY ?= python3
USER_BIN := $(shell $(PY) -m site --user-base 2>/dev/null)/bin

.PHONY: help doctor path install dev lint unit smoke pkg-smoke pkg-live test container-test \
        vm-fetch vm-drive vm-up vm-ssh vm-push vm-test vm-wipe vm-destroy

help:
	@echo "porta-winux targets:"
	@echo "  doctor          check that required/optional dependencies are present"
	@echo "  install         pip install (user site); warns if the bin dir isn't on PATH"
	@echo "  path            add the pip user bin dir to your shell PATH (edits your shell rc)"
	@echo "  dev             editable install with tui+dev extras"
	@echo "  lint            ruff check (needs 'make dev' or ruff installed)"
	@echo "  unit            pytest unit tests (pure logic, no restic needed)"
	@echo "  smoke           fast end-to-end test in a sandbox (needs restic)"
	@echo "  pkg-smoke       full pkg scan/adopt/apply flow vs FAKE package managers"
	@echo "  pkg-live        READ-ONLY pkg scans against this machine's real dnf/flatpak/rpm"
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
	@case ":$$PATH:" in \
	  *:"$(USER_BIN)":*) echo "  PATH includes $(USER_BIN) OK" ;; \
	  *) echo "  PATH missing $(USER_BIN) — installed 'porta-winux' won't be found (make path)" ;; \
	esac
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
	@case ":$$PATH:" in \
	  *:"$(USER_BIN)":*) echo "porta-winux installed; '$(USER_BIN)' is on your PATH" ;; \
	  *) echo ""; \
	     echo "NOTE: porta-winux was installed to $(USER_BIN),"; \
	     echo "      but that directory is NOT on your PATH, so the command"; \
	     echo "      won't be found. Fix it with:  make path"; \
	     echo "      (or run it as: $(USER_BIN)/porta-winux)" ;; \
	esac

dev:
	$(PY) -m pip install --user -e '.[tui,dev]'

# Adds the pip user bin dir to PATH via your shell's rc file. Explicitly
# invoking this target is the consent; it prints exactly what it changed,
# and running it again is a no-op.
path:
	@BIN="$(USER_BIN)"; \
	case ":$$PATH:" in *:"$$BIN":*) \
	  echo "'$$BIN' is already on your PATH — nothing to do"; exit 0;; esac; \
	LINE="export PATH=\"\$$HOME/.local/bin:\$$PATH\""; \
	case "$$BIN" in "$$HOME"/.local/bin) ;; \
	  *) LINE="export PATH=\"$$BIN:\$$PATH\"";; esac; \
	case "$${SHELL##*/}" in \
	  fish) \
	    echo "fish shell detected — run this once instead:"; \
	    echo "  fish_add_path $$BIN"; exit 0;; \
	  zsh)  RC="$$HOME/.zshrc";; \
	  *)    RC="$$HOME/.bashrc";; \
	esac; \
	if [ -f "$$RC" ] && grep -qF "$$LINE" "$$RC"; then \
	  echo "$$RC already contains the PATH line; open a NEW terminal (or: source $$RC)"; \
	else \
	  printf '\n# added by porta-winux (make path)\n%s\n' "$$LINE" >> "$$RC"; \
	  echo "appended to $$RC:"; echo "  $$LINE"; \
	  echo "takes effect in NEW terminals; for this one, run:  source $$RC"; \
	fi

lint:
	@command -v ruff >/dev/null || { echo "ruff not installed; run 'make dev' first"; exit 1; }
	ruff check porta_winux test/unit

unit:
	@command -v pytest >/dev/null || { echo "pytest not installed; run 'make dev' first"; exit 1; }
	$(PY) -m pytest test/unit/ -q

smoke:
	@command -v restic >/dev/null || { echo "restic not installed (dnf install restic)"; exit 1; }
	bash test/smoke.sh

pkg-smoke:
	bash test/pkg-smoke.sh

pkg-live:
	bash test/pkg-live.sh

test: unit smoke pkg-smoke

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
