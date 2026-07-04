SHELL := /bin/bash
.ONESHELL:
.SHELLFLAGS := -eu -o pipefail -c

DNF_PACKAGES := borgbackup borgmatic ansible rsync python3-pyyaml python3-jinja2
GALAXY_COLLECTION := community.general

STATE_DIR := .porta-winux-state
DNF_BASELINE := $(STATE_DIR)/dnf-preexisting.txt
COLLECTION_BASELINE := $(STATE_DIR)/collection-preexisting.txt
MANIFEST := manifest/manifest.yaml
SERVICE_SRC := systemd/borg-drive-handler@.service
SERVICE_DEST := /etc/systemd/system/borg-drive-handler@.service
UDEV_SRC := udev/99-backup-drive.rules
UDEV_DEST := /etc/udev/rules.d/99-backup-drive.rules
UDEV_RENDERED := $(STATE_DIR)/99-backup-drive.rendered.rules

.PHONY: check install-deps check-collection install-collection setup revert \
        install-service detect-uuid install-udev-rule borg-init

## Snapshot which packages are ALREADY installed (the baseline revert will
## protect), then report what's missing. Never installs anything.
check:
	@mkdir -p $(STATE_DIR)
	@: > $(DNF_BASELINE)
	@echo "Checking dnf packages..."
	@missing=""
	for pkg in $(DNF_PACKAGES); do
		if rpm -q "$$pkg" >/dev/null 2>&1; then
			echo "  [ok]      $$pkg"
			echo "$$pkg" >> $(DNF_BASELINE)
		else
			echo "  [missing] $$pkg"
			missing="$$missing $$pkg"
		fi
	done
	if [ -z "$$missing" ]; then
		echo "All dnf packages already present. Nothing to do."
	else
		echo ""
		echo "Missing:$$missing"
		echo "Run 'make install-deps' to install ONLY these packages."
	fi
	@echo "(baseline snapshot written to $(DNF_BASELINE) for 'make revert')"

## Install only the currently-missing packages, and only after you confirm.
## Depends on 'check' so the baseline snapshot is always fresh right before
## anything is actually installed.
install-deps: check
	@missing=""
	for pkg in $(DNF_PACKAGES); do
		grep -qx "$$pkg" $(DNF_BASELINE) 2>/dev/null || missing="$$missing $$pkg"
	done
	if [ -z "$$missing" ]; then
		echo "Nothing to install — all packages already present."
		exit 0
	fi
	echo "About to run: sudo dnf install -y$$missing"
	read -r -p "Proceed? [y/N] " ans || ans="n"
	if [[ "$$ans" =~ ^[Yy]$$ ]]; then
		sudo dnf install -y $$missing
	else
		echo "Aborted. Nothing was installed."
	fi

## Snapshot whether the collection is already present, then report status.
check-collection:
	@mkdir -p $(STATE_DIR)
	@if ansible-galaxy collection list 2>/dev/null | grep -q "^$(GALAXY_COLLECTION)"; then
		echo "[ok]      $(GALAXY_COLLECTION)"
		echo "present" > $(COLLECTION_BASELINE)
	else
		echo "[missing] $(GALAXY_COLLECTION)"
		echo "absent" > $(COLLECTION_BASELINE)
		echo "Run 'make install-collection' to install it."
	fi

## Install the ansible-galaxy collection, only after confirmation.
install-collection: check-collection
	@if [ "$$(cat $(COLLECTION_BASELINE) 2>/dev/null)" = "present" ]; then
		echo "Already installed. Nothing to do."
	else
		read -r -p "Install ansible-galaxy collection $(GALAXY_COLLECTION)? [y/N] " ans || ans="n"
		if [[ "$$ans" =~ ^[Yy]$$ ]]; then
			ansible-galaxy collection install $(GALAXY_COLLECTION)
		else
			echo "Aborted."
		fi
	fi

## Guided pass: shows you everything missing, then asks before each install step.
setup: check install-deps check-collection install-collection
	@echo "Setup complete."

## Uninstall ONLY what was installed by this Makefile since the last
## 'make check' / 'make check-collection' baseline. Packages that were
## already on the system before that snapshot are never touched.
revert:
	@if [ ! -f $(DNF_BASELINE) ]; then
		echo "No dnf baseline found at $(DNF_BASELINE)."
		echo "Nothing to revert — 'make check' (or install-deps) must have run first."
	else
		to_remove=""
		for pkg in $(DNF_PACKAGES); do
			if rpm -q "$$pkg" >/dev/null 2>&1 && ! grep -qx "$$pkg" $(DNF_BASELINE); then
				to_remove="$$to_remove $$pkg"
			fi
		done
		if [ -z "$$to_remove" ]; then
			echo "Nothing to revert — no packages beyond the baseline are currently installed."
		else
			echo "These were NOT present at the last 'make check' and are installed now:"
			echo "  $$to_remove"
			read -r -p "Remove them with 'sudo dnf remove'? [y/N] " ans || ans="n"
			if [[ "$$ans" =~ ^[Yy]$$ ]]; then
				sudo dnf remove -y $$to_remove
			else
				echo "Aborted. Nothing was removed."
			fi
		fi
	fi
	@if [ -f $(COLLECTION_BASELINE) ] && [ "$$(cat $(COLLECTION_BASELINE))" = "absent" ]; then
		if ansible-galaxy collection list 2>/dev/null | grep -q "^$(GALAXY_COLLECTION)"; then
			echo ""
			echo "$(GALAXY_COLLECTION) was absent at the last check-collection and is installed now."
			echo "NOTE: ansible-galaxy has no official 'collection remove' command as of this"
			echo "writing — this deletes the collection's directory directly."
			read -r -p "Delete it from ~/.ansible/collections? [y/N] " ans || ans="n"
			if [[ "$$ans" =~ ^[Yy]$$ ]]; then
				rm -rf "$$HOME/.ansible/collections/ansible_collections/community/general"
			else
				echo "Aborted. Nothing was removed."
			fi
		fi
	fi