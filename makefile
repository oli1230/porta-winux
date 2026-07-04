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

## Install (or update, if the repo's copy has changed) the systemd unit that
## drive_handler.sh runs under. Shows a diff and asks before touching
## anything under /etc, and reloads systemd so the change takes effect
## immediately rather than needing a reboot.
install-service:
	@if [ ! -f $(SERVICE_SRC) ]; then \
		echo "ERROR: $(SERVICE_SRC) not found (run make from the repo root)"; exit 1; \
	fi
	@if [ -f $(SERVICE_DEST) ] && diff -q $(SERVICE_SRC) $(SERVICE_DEST) >/dev/null 2>&1; then \
		echo "[ok] $(SERVICE_DEST) already up to date."; \
	else \
		if [ -f $(SERVICE_DEST) ]; then \
			echo "Installed unit differs from the repo version:"; \
			diff -u $(SERVICE_DEST) $(SERVICE_SRC) || true; \
			read -r -p "Update installed service with this version? [y/N] " ans || ans="n"; \
		else \
			echo "Service is not installed yet. Will install:"; \
			cat $(SERVICE_SRC); \
			read -r -p "Install $(SERVICE_DEST)? [y/N] " ans || ans="n"; \
		fi; \
		if [[ "$$ans" =~ ^[Yy]$$ ]]; then \
			sudo cp $(SERVICE_SRC) $(SERVICE_DEST); \
			sudo systemctl daemon-reload; \
			echo "Installed and reloaded."; \
		else \
			echo "Aborted. Nothing changed."; \
		fi; \
	fi

## Detect the currently-mounted backup drive's filesystem UUID and offer to
## write it into manifest.yaml's drive.expected_uuid (asks first; a plain
## text substitution, so your comments in the file are preserved).
detect-uuid:
	@drive_label="$$(python3 -c "import yaml; print(yaml.safe_load(open('$(MANIFEST)'))['drive']['label'])")"; \
	run_as_user="$${USER:-$$(whoami)}"; \
	mount_path="/run/media/$$run_as_user/$$drive_label"; \
	if ! mountpoint -q "$$mount_path" 2>/dev/null; then \
		echo "ERROR: $$mount_path is not mounted. Plug in '$$drive_label' and try again."; exit 1; \
	fi; \
	detected="$$(findmnt -no UUID "$$mount_path")"; \
	current="$$(python3 -c "import yaml; print(yaml.safe_load(open('$(MANIFEST)'))['drive'].get('expected_uuid',''))")"; \
	echo "Detected UUID for '$$drive_label': $$detected"; \
	if [ "$$detected" = "$$current" ]; then \
		echo "manifest.yaml already has this UUID recorded. Nothing to do."; \
	else \
		if [ -n "$$current" ]; then \
			echo "manifest.yaml currently has a DIFFERENT UUID recorded: $$current"; \
		fi; \
		read -r -p "Write $$detected into $(MANIFEST) as drive.expected_uuid? [y/N] " ans || ans="n"; \
		if [[ "$$ans" =~ ^[Yy]$$ ]]; then \
			python3 -c "import re; p='$(MANIFEST)'; text=open(p).read(); new_text=re.sub(r'(?m)^(\s*expected_uuid:).*\$$', r'\1 \"$$detected\"', text, count=1); open(p,'w').write(new_text)"; \
			echo "Updated $(MANIFEST)."; \
		else \
			echo "Aborted."; \
		fi; \
	fi

## Render udev/99-backup-drive.rules with the UUID from manifest.yaml, then
## install (or update) it under /etc/udev/rules.d, asking first either way.
install-udev-rule:
	@uuid="$$(python3 -c "import yaml; print(yaml.safe_load(open('$(MANIFEST)'))['drive'].get('expected_uuid',''))")"; \
	if [ -z "$$uuid" ]; then \
		echo "ERROR: $(MANIFEST) has no drive.expected_uuid set yet."; \
		echo "Run 'make detect-uuid' first (with the drive plugged in)."; \
		exit 1; \
	fi; \
	mkdir -p $(STATE_DIR); \
	sed "s/REPLACE-WITH-YOUR-UUID/$$uuid/" $(UDEV_SRC) > $(UDEV_RENDERED); \
	if [ -f $(UDEV_DEST) ] && diff -q $(UDEV_RENDERED) $(UDEV_DEST) >/dev/null 2>&1; then \
		echo "[ok] $(UDEV_DEST) already up to date."; \
	else \
		if [ -f $(UDEV_DEST) ]; then \
			echo "Installed udev rule differs from the rendered version:"; \
			diff -u $(UDEV_DEST) $(UDEV_RENDERED) || true; \
			read -r -p "Update installed rule with this version? [y/N] " ans || ans="n"; \
		else \
			echo "Will install:"; \
			cat $(UDEV_RENDERED); \
			read -r -p "Install $(UDEV_DEST)? [y/N] " ans || ans="n"; \
		fi; \
		if [[ "$$ans" =~ ^[Yy]$$ ]]; then \
			sudo cp $(UDEV_RENDERED) $(UDEV_DEST); \
			sudo udevadm control --reload; \
			echo "Installed and reloaded."; \
		else \
			echo "Aborted. Nothing changed."; \
		fi; \
	fi

## One-time-per-host: borg init the repo on the drive, but only if one
## doesn't already exist there (never re-inits over an existing repo).
borg-init:
	@drive_label="$$(python3 -c "import yaml; print(yaml.safe_load(open('$(MANIFEST)'))['drive']['label'])")"; \
	uuid="$$(python3 -c "import yaml; print(yaml.safe_load(open('$(MANIFEST)'))['drive'].get('expected_uuid',''))")"; \
	timeout_s="$$(python3 -c "import yaml; print(yaml.safe_load(open('$(MANIFEST)'))['drive'].get('mount_timeout',30))")"; \
	repo_root="$$(python3 -c "import yaml; print(yaml.safe_load(open('$(MANIFEST)'))['borg']['repo_root'])")"; \
	check_args=(--label "$$drive_label" --timeout "$$timeout_s"); \
	[ -n "$$uuid" ] && check_args+=(--uuid "$$uuid"); \
	mount_point="$$(scripts/check_drive_mounted.sh "$${check_args[@]}")" || { echo "ERROR: drive not verified mounted"; exit 1; }; \
	host="$$(hostname -s)"; \
	repo_path="$$mount_point/$${repo_root#/}/$$host.borgrepo"; \
	if [ -f "$$repo_path/config" ]; then \
		echo "[ok] borg repo already initialized at $$repo_path"; \
	else \
		echo "No borg repo found at $$repo_path yet."; \
		read -r -p "Run 'borg init --encryption=repokey-blake2' there now? [y/N] " ans || ans="n"; \
		if [[ "$$ans" =~ ^[Yy]$$ ]]; then \
			mkdir -p "$$repo_path"; \
			borg init --encryption=repokey-blake2 "$$repo_path"; \
		else \
			echo "Aborted."; \
		fi; \
	fi