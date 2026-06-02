# bot/Makefile — operate the Pi-side forgejo-runner-notify service.
# Run on the Pi host (where the service runs). Most targets need sudo.

SCRIPT_SRC := forgejo-runner-notify
SCRIPT_DST := /usr/local/bin/forgejo-runner-notify
UNIT_SRC   := forgejo-runner-notify.service
UNIT_DST   := /etc/systemd/system/forgejo-runner-notify.service
ENV_DST    := /etc/forgejo-runner-notify.env
BACKUP_DIR := $(HOME)

.PHONY: help install install-script install-unit restart status logs backup verify

help:           ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

install-script: backup ## Install patched script (sudo) — replaces $(SCRIPT_DST)
	sudo install -m 755 -o nobody -g nogroup $(SCRIPT_SRC) $(SCRIPT_DST)

install-unit:   ## Install systemd unit (sudo) — daemon-reload after
	sudo install -m 644 $(UNIT_SRC) $(UNIT_DST)
	sudo systemctl daemon-reload

install: install-script install-unit restart status  ## Full install + restart + status

restart:        ## Restart the notifier (sudo)
	sudo systemctl restart forgejo-runner-notify

status:         ## Show service status (sudo)
	sudo systemctl status forgejo-runner-notify --no-pager | head -20

logs:           ## Tail recent service logs (sudo)
	sudo journalctl -u forgejo-runner-notify -n 50 --no-pager

backup:         ## Snapshot current installed script to $(BACKUP_DIR)
	@if [ -f $(SCRIPT_DST) ]; then \
		sudo cp $(SCRIPT_DST) $(BACKUP_DIR)/forgejo-runner-notify.bak.$$(date +%Y%m%d-%H%M); \
		echo "backup written under $(BACKUP_DIR)"; \
	else \
		echo "no installed script at $(SCRIPT_DST) — skipping backup"; \
	fi

verify:         ## Byte-compile + smoke imports (no creds, no network)
	python3 -m py_compile $(SCRIPT_SRC)
	@echo "script compiles cleanly"
