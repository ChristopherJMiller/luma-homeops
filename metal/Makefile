env ?= "prod"

default: boot

~/.ssh/id_ed25519:
	ssh-keygen -t ed25519 -f "$@"

boot: ~/.ssh/id_ed25519
	ansible-playbook \
		--inventory inventories/${env}.yaml \
		boot.yaml

install: ~/.ssh/id_ed25519
	ansible-playbook \
		--inventory inventories/${env}.yaml \
		install.yaml

k3s: ~/.ssh/id_ed25519
	ansible-playbook \
		--inventory inventories/${env}.yaml \
		k3s.yaml
