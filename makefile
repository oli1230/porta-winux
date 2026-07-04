SHELL := /bin/bash
.ONESHELL:
.SHELLFLAGS := -eu -o pipefail -c

DNF_PACKAGES := borgbackup borgmatic ansible rsync python3-pyyaml python3-jinja2
GALAXY_COLLECTION := community.general

.PHONY: check install-deps check-collection install-collection setup

## Report which dnf packages are missing. Never installs anything.
check:
	@echo "Checking dnf packages..."
	@missing=""
	for pkg in $(DNF_PACKAGES); do
		if rpm -q "$$pkg" >/dev/null 2>&1; then
			echo "  [ok]      $$pkg"
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

## Install only the currently-missing packages, and only after you confirm.
## Already-installed packages are never touched, upgraded, or reinstalled.
install-deps:
	@missing=""
	for pkg in $(DNF_PACKAGES); do
		rpm -q "$$pkg" >/dev/null 2>&1 || missing="$$missing $$pkg"
	done
	if [ -z "$$missing" ]; then
		echo "Nothing to install — all packages already present."
		exit 0
	fi
	echo "About to run: sudo dnf install -y$$missing"
	read -r -p "Proceed? [y/N] " ans
	if [[ "$$ans" =~ ^[Yy]$$ ]]; then
		sudo dnf install -y $$missing
	else
		echo "Aborted. Nothing was installed."
	fi

## Report whether the required ansible-galaxy collection is present.
check-collection:
	@if ansible-galaxy collection list 2>/dev/null | grep -q "^$(GALAXY_COLLECTION)"; then
		echo "[ok]      $(GALAXY_COLLECTION)"
	else
		echo "[missing] $(GALAXY_COLLECTION)"
		echo "Run 'make install-collection' to install it."
	fi

## Install the ansible-galaxy collection, only after confirmation.
install-collection:
	@if ansible-galaxy collection list 2>/dev/null | grep -q "^$(GALAXY_COLLECTION)"; then
		echo "Already installed. Nothing to do."
	else
		read -r -p "Install ansible-galaxy collection $(GALAXY_COLLECTION)? [y/N] " ans
		if [[ "$$ans" =~ ^[Yy]$$ ]]; then
			ansible-galaxy collection install $(GALAXY_COLLECTION)
		else
			echo "Aborted."
		fi
	fi

## Guided pass: shows you everything missing, then asks before each install step.
setup: check install-deps check-collection install-collection
	@echo "Setup complete."